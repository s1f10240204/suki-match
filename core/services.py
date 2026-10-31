"""マッチングのドメインロジック。ビューとWebSocketの両方から使う。"""
import json
from datetime import timedelta
from math import asin, ceil, cos, radians, sin, sqrt

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.db.models.functions import ExtractHour
from django.urls import reverse
from django.utils import timezone

from . import images, line_api, moderation
from .geocoding import area_label_for
from .models import (ActivityBase, Application, HelpRequest, Match, Message, MessageRead, MessageReaction,
                     Notification, NotificationSetting, PointTransaction, PushSubscription, Report, Review, Sanction, User,
                     MissionClaim, UserBlock)

RADIUS_METERS = 500
HELPER_POINTS = 10
POINTS_PER_MINUTE = 1  # 依頼ポイントは所要時間1分あたり1pt(依頼者→ヘルパーへ渡る)
REVIEW_BONUS_POINTS = 2
CREDIT_LIMIT = 50           # ツケ(マイナス残高)で依頼できる下限
REFERRAL_REWARD_POINTS = 30  # 招待した友達が参加条件を満たしたら紹介者へ
LOGIN_BONUS_POINTS = 2       # 1日1回のログインボーナス
LOCATION_FRESH_HOURS = 24      # これより古い現在地は通知対象にしない
RELABEL_DISTANCE_M = 200       # これ以上移動したらエリア名を取り直す

CANCEL_GRACE_SECONDS = 60
PENALTY_WINDOW_DAYS = 30
# 猶予後キャンセルの件数(直近30日) → (内容, 期間)。4件目以降は PENALTY_MAX
PENALTY_LADDER = {
    1: (Sanction.Level.WARNING, timedelta(days=PENALTY_WINDOW_DAYS)),
    2: (Sanction.Level.NO_ACCEPT, timedelta(hours=24)),
    3: (Sanction.Level.NO_ACTIVITY, timedelta(hours=72)),
}
PENALTY_MAX = (Sanction.Level.NO_ACTIVITY, timedelta(days=7))
SEVERITY = {Sanction.Level.WARNING: 0, Sanction.Level.NO_ACCEPT: 1,
            Sanction.Level.NO_ACTIVITY: 2, Sanction.Level.BANNED: 3}

# 不正検知: 一定回数以上マッチした上でキャンセル率が閾値を超えたら受付不可
ANOMALY_MIN_MATCHES = 5
ANOMALY_CANCEL_RATE = 0.5

REPORT_WINDOW_DAYS = 30
REPORT_AUTO_SANCTION_REPORTERS = 3
REPORT_SANCTION_DAYS = 7

ONLINE_KEY = 'online:{}'


class ServiceError(Exception):
    """利用者にそのまま表示できるエラー。"""


# ---------- 位置 ----------

def distance_m(lat1, lon1, lat2, lon2):
    """2地点間の距離(m)。ハーバーサイン公式。"""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def distance_label(meters):
    """正確な距離は出さず段階表示にする(複数地点からの測量で住所を特定されないように)。"""
    for limit, label in ((200, '200m以内'), (500, '500m以内'), (1000, '1km以内')):
        if meters <= limit:
            return label
    return '1km以上'


def public_distance(user, help_request):
    """閲覧者から依頼(丸めた公開座標)までの距離(m)。"""
    return distance_m(user.latitude, user.longitude, help_request.public_latitude, help_request.public_longitude)


def bounding_box(lat, lng, meters):
    dlat = meters / 111320
    dlng = meters / (111320 * max(cos(radians(lat)), 0.01))
    return (lat - dlat, lat + dlat), (lng - dlng, lng + dlng)


def update_user_location(user, lat, lng):
    moved = not user.has_location or distance_m(user.latitude, user.longitude, lat, lng) > RELABEL_DISTANCE_M
    user.latitude, user.longitude = lat, lng
    user.location_updated_at = timezone.now()
    if moved or not user.area_label:
        user.area_label = area_label_for(lat, lng) or user.area_label
    user.save(update_fields=['latitude', 'longitude', 'location_updated_at', 'area_label'])


def can_view_exact_location(user, help_request):
    """正確な場所は依頼者本人と、マッチングしたヘルパーだけが見られる。"""
    if not user.is_authenticated:
        return False
    if user.pk == help_request.requester_id:
        return True
    return help_request.matches.filter(
        helper=user, status__in=[Match.Status.IN_PROGRESS, Match.Status.DONE]).exists()


# ---------- スキル・利用制限 ----------

def skill_matches(user, help_request):
    """依頼の必要スキルのどれかを持っていればOK(指定なしなら誰でも)。"""
    required = {s.resolved.pk for s in help_request.required_skills.select_related('canonical')}
    return not required or bool(required & user.resolved_skill_ids())


def strongest_sanction(user):
    sanctions = list(Sanction.objects.filter(user=user).active().exclude(level=Sanction.Level.WARNING))
    return max(sanctions, key=lambda s: SEVERITY[s.level], default=None)


def _until(sanction):
    if sanction.ends_at is None:
        return ''
    return timezone.localtime(sanction.ends_at).strftime('%m/%d %H:%M') + 'まで'


def is_suspicious(user):
    """統計的に不自然な行動(高いキャンセル率)を検知する。"""
    matches = Match.objects.filter(helper=user)
    total = matches.count()
    if total < ANOMALY_MIN_MATCHES:
        return False
    return matches.filter(status=Match.Status.CANCELED).count() / total > ANOMALY_CANCEL_RATE


def photo_exempt(user):
    """@iniad.org のメール認証が済んだアカウントは、大学で本人確認済みとして顔写真なしでも使える。"""
    from allauth.account.models import EmailAddress
    return EmailAddress.objects.filter(user=user, verified=True, email__iendswith='@iniad.org').exists()


def photo_block_reason(user):
    """顔写真の審査が済んでいないと、依頼も応募もできない。"""
    if photo_exempt(user):
        return ''
    status = user.avatar_status if user.avatar else User.PhotoStatus.NONE
    if status == User.PhotoStatus.APPROVED:
        return ''
    if status == User.PhotoStatus.PENDING:
        return '顔写真の審査中です。審査が終わると依頼・お手伝いができるようになります。'
    if status == User.PhotoStatus.REJECTED:
        reason = f'(理由: {user.avatar_reject_reason})' if user.avatar_reject_reason else ''
        return f'顔写真が承認されませんでした{reason}。別の写真を登録してください。'
    return '依頼・お手伝いには顔写真の登録が必要です。プロフィールから登録してください。'


def accept_block_reason(user):
    """お手伝いを受けられない理由。受けられるなら空文字。"""
    photo = photo_block_reason(user)
    if photo:
        return photo
    sanction = strongest_sanction(user)
    if sanction is not None:
        return f'{_until(sanction)}「{sanction.get_level_display()}」中のため、お手伝いを受けられません。'
    if is_suspicious(user):
        return 'キャンセル率が高いため、現在お手伝いを受けられません。'
    return ''


def post_block_reason(user):
    photo = photo_block_reason(user)
    if photo:
        return photo
    sanction = strongest_sanction(user)
    if sanction is not None and sanction.level in (Sanction.Level.NO_ACTIVITY, Sanction.Level.BANNED):
        return f'{_until(sanction)}「{sanction.get_level_display()}」中のため、依頼を投稿できません。'
    return ''


# ---------- 顔写真 ----------

def set_photo(user, photo):
    """images.normalize_photo 済みの顔写真を保存して審査待ちにする。"""
    old = user.avatar.name if user.avatar else ''
    user.avatar.save(photo.name, photo, save=False)
    user.avatar_status, user.avatar_reject_reason, user.avatar_reviewed_at = User.PhotoStatus.PENDING, '', None
    user.save(update_fields=['avatar', 'avatar_status', 'avatar_reject_reason', 'avatar_reviewed_at'])
    if old:
        images.delete_photo(old)


def review_photo(user, approve, reason=''):
    user.avatar_status = User.PhotoStatus.APPROVED if approve else User.PhotoStatus.REJECTED
    user.avatar_reject_reason = '' if approve else reason[:200]
    user.avatar_reviewed_at = timezone.now()
    user.save(update_fields=['avatar_status', 'avatar_reject_reason', 'avatar_reviewed_at'])
    if approve:
        images.ensure_mosaic(user.avatar.name)
        notify(user, 'photo', '顔写真が承認されました。依頼・お手伝いができるようになりました', reverse('core:profile'))
        grant_referral_reward(user)  # 招待で来た人なら、この瞬間に紹介者へボーナス
    else:
        notify(user, 'photo', '顔写真が承認されませんでした。プロフィールから別の写真を登録してください',
               reverse('core:profile_edit'))


def photo_revealed(viewer, target):
    """モザイクなしで見せてよいか: 本人・運営、またはマッチング成立済みの相手。"""
    if not viewer.is_authenticated:
        return False
    if viewer.pk == target.pk or viewer.is_staff:
        return True
    active = [Match.Status.IN_PROGRESS, Match.Status.DONE]
    return Match.objects.filter(status__in=active).filter(
        Q(helper=viewer, help_request__requester=target) | Q(helper=target, help_request__requester=viewer)
    ).exists()


# ---------- 通知 ----------

def push(group, payload):
    layer = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(group, payload)


def notify(user, kind, text, url=''):
    """通知履歴に残し、接続中ならWebSocketでも届ける。LINE・プッシュは設定に応じて送る。"""
    notification = Notification.objects.create(user=user, kind=kind, text=text[:200], url=url)
    push(f'user_{user.pk}', {'type': 'notify', 'kind': kind, 'message': notification.text, 'url': url})
    setting = notification_setting(user)
    if setting.forced_off:
        return notification  # 管理者が通知を止めている場合は外部通知を送らない
    line_api.maybe_notify(user, kind, notification.text, url)
    if kind != 'nearby_request' and setting.push_activity:
        send_web_push(user, 'suki-match', notification.text, url)
    return notification


def send_web_push(user, title, body, url=''):
    """ホーム画面に追加した PWA 等へポップアップ通知を送る。失敗しても呼び出し元に影響させない。"""
    if not settings.VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush
    except ImportError:
        return
    data = json.dumps({'title': title, 'body': body[:120], 'url': url or '/'})
    for sub in list(user.push_subscriptions.all()):
        try:
            webpush(subscription_info={'endpoint': sub.endpoint,
                                       'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
                    data=data, vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': settings.VAPID_SUBJECT}, ttl=3600)
        except Exception as exc:  # 失効した購読(404/410)は掃除する
            response = getattr(exc, 'response', None)
            if response is not None and getattr(response, 'status_code', 0) in (404, 410):
                sub.delete()


def set_online(user_id, delta):
    key = ONLINE_KEY.format(user_id)
    cache.add(key, 0, None)
    try:
        return cache.incr(key, delta)
    except ValueError:
        cache.set(key, max(delta, 0), None)
        return max(delta, 0)


def is_online(user_id):
    return (cache.get(ONLINE_KEY.format(user_id)) or 0) > 0


def notification_setting(user):
    return NotificationSetting.objects.get_or_create(user=user)[0]


def detail_url(help_request):
    return reverse('core:help_request_detail', args=[help_request.pk])

def rating_summary(user):
    """受け取ったレビューの (平均, 件数)。レビューがなければ (None, 0)。"""
    agg = user.received_reviews.aggregate(avg=Avg('rating'), count=Count('id'))
    return agg['avg'], agg['count']


def rating_map(user_ids):
    """一覧表示用に複数ユーザーの評価をまとめて集計する。{user_id: (平均, 件数)}"""
    rows = (Review.objects.filter(reviewee_id__in=list(user_ids))
            .values('reviewee_id').annotate(avg=Avg('rating'), count=Count('id')))
    return {row['reviewee_id']: (row['avg'], row['count']) for row in rows}


def is_blocked_pair(user_a_id, user_b_id):
    """どちらか一方がもう一方をブロックしていれば True(新規マッチング不可)。"""
    return UserBlock.objects.filter(
        Q(blocker_id=user_a_id, blocked_id=user_b_id)
        | Q(blocker_id=user_b_id, blocked_id=user_a_id)).exists()


def blocked_user_ids(user):
    """一覧で非表示にすべき相手(自分がブロックした人 + 自分をブロックした人)のID集合。"""
    ids = set(UserBlock.objects.filter(blocker=user).values_list('blocked_id', flat=True))
    ids |= set(UserBlock.objects.filter(blocked=user).values_list('blocker_id', flat=True))
    return ids


def block_user(blocker, blocked):
    if blocker.pk == blocked.pk:
        raise ServiceError('自分自身はブロックできません。')
    obj, _created = UserBlock.objects.get_or_create(blocker=blocker, blocked=blocked)
    return obj


def unblock_user(blocker, blocked):
    UserBlock.objects.filter(blocker=blocker, blocked=blocked).delete()




def nearby_helpers(help_request):
    """依頼の半径500m以内(現在地または活動拠点)にいて、スキル条件を満たし、受付制限のないユーザー。"""
    lat, lng = help_request.latitude, help_request.longitude
    lat_range, lng_range = bounding_box(lat, lng, RADIUS_METERS)
    fresh_since = timezone.now() - timedelta(hours=LOCATION_FRESH_HOURS)

    candidate_ids = set()
    for user in User.objects.filter(is_active=True, latitude__range=lat_range, longitude__range=lng_range,
                                    location_updated_at__gte=fresh_since):
        if distance_m(user.latitude, user.longitude, lat, lng) <= RADIUS_METERS:
            candidate_ids.add(user.pk)
    for base in ActivityBase.objects.filter(latitude__range=lat_range, longitude__range=lng_range):
        if distance_m(base.latitude, base.longitude, lat, lng) <= RADIUS_METERS:
            candidate_ids.add(base.user_id)
    candidate_ids.discard(help_request.requester_id)

    return [user for user in User.objects.filter(pk__in=candidate_ids, is_active=True,
                                                 avatar_status=User.PhotoStatus.APPROVED).order_by('pk')
            if skill_matches(user, help_request) and not accept_block_reason(user)]


def notify_nearby_helpers(help_request):
    if help_request.requester.is_shadow_banned:
        return 0  # ゴーストBan中は誰にも通知しない(本人には通常どおり見える)
    when = (f'{timezone.localtime(help_request.scheduled_for):%m/%d %H:%M}〜'
            if help_request.scheduled_for else '今すぐ')
    text = f'近くで依頼「{help_request.title}」({when}・{help_request.duration_minutes}分)'
    helpers = nearby_helpers(help_request)
    for user in helpers:
        notify(user, 'nearby_request', text, detail_url(help_request))
    return len(helpers)


# ---------- 依頼のライフサイクル ----------

def cost_for_duration(duration_minutes):
    return int(duration_minutes) * POINTS_PER_MINUTE


def record_points(user, amount, kind, help_request=None, counterparty=None, description=''):
    """ポイント増減を台帳に記録する。呼び出し前に user.points を増減後の値にしておくこと。"""
    if not amount:
        return None
    return PointTransaction.objects.create(
        user=user, amount=amount, balance_after=user.points, kind=kind,
        help_request=help_request, counterparty=counterparty, description=description)


def charge_for_request(help_request):
    """投稿時に依頼ポイントを残高から預かる。残高不足なら ServiceError。"""
    cost = cost_for_duration(help_request.duration_minutes)
    with transaction.atomic():
        existing = PointTransaction.objects.filter(
            help_request=help_request, kind=PointTransaction.Kind.ESCROW).first()
        if existing is not None:
            return -existing.amount  # 徴収済み(二重送信・再送でも二重徴収しない)
        requester = User.objects.select_for_update().get(pk=help_request.requester_id)
        if requester.points - cost < -CREDIT_LIMIT:
            raise ServiceError(
                f'ポイントが不足しています(必要 {cost}pt / 保有 {requester.points}pt)。'
                f'ツケ(最大で-{CREDIT_LIMIT}ptまで)でも足りません。'
                'ポイントページに、ポイントを貯める方法があります。')
        requester.points -= cost
        requester.save(update_fields=['points'])
        help_request.points_cost = cost
        help_request.save(update_fields=['points_cost'])
        record_points(requester, -cost, PointTransaction.Kind.ESCROW, help_request,
                      description=f'「{help_request.title}」のポイント預かり')
    return cost


def _claim_points_settlement(help_request):
    """精算・返金の権利を原子的に確保する。確保できた場合だけ True(同時実行でも一度だけ)。"""
    return HelpRequest.objects.filter(
        pk=help_request.pk, points_settled=False).update(points_settled=True) == 1


def _refund_escrow(help_request):
    """預かったポイントを依頼者へ返す(キャンセル・期限切れ用)。精算は一度だけ。"""
    if help_request.points_cost and _claim_points_settlement(help_request):
        help_request.points_settled = True
        User.objects.filter(pk=help_request.requester_id).update(
            points=F('points') + help_request.points_cost)
        help_request.points_settled = True
        balance = User.objects.values_list('points', flat=True).get(pk=help_request.requester_id)
        PointTransaction.objects.create(
            user_id=help_request.requester_id, amount=help_request.points_cost,
            balance_after=balance, kind=PointTransaction.Kind.REFUND,
            help_request=help_request,
            description=f'「{help_request.title}」の預かり分返金')


def publish_request(help_request):
    if not 1 <= help_request.headcount <= 20:
        raise ServiceError('募集人数が不正です。')
    charge_for_request(help_request)
    help_request.area_label = area_label_for(help_request.latitude, help_request.longitude)
    help_request.save(update_fields=['area_label'])
    return notify_nearby_helpers(help_request)


def _claim(help_request, helper):
    """募集人数の枠が残っていれば参加させる。呼び出し側のトランザクション内で行をロックして数える。"""
    locked = HelpRequest.objects.select_for_update().get(pk=help_request.pk)
    if locked.status not in (HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS) \
            or locked.expires_at <= timezone.now():
        raise ServiceError('この依頼はすでに締め切られました。')
    if locked.matches.filter(status=Match.Status.IN_PROGRESS, helper=helper).exists():
        raise ServiceError('すでに参加しています。')
    if locked.matches.filter(status=Match.Status.IN_PROGRESS).count() >= locked.headcount:
        raise ServiceError('募集人数に達しました。')
    match = Match.objects.create(help_request=locked, helper=helper)
    if locked.status == HelpRequest.Status.OPEN:
        locked.status = HelpRequest.Status.IN_PROGRESS
        locked.save(update_fields=['status'])
    help_request.status = locked.status
    return match


def _check_can_help(help_request, user):
    if help_request.requester_id == user.pk:
        raise ServiceError('自分の依頼は受けられません。')
    if user.is_shadow_banned:
        # ゴーストBan中は締め切られたように見せて静かに止める(マッチングさせない)
        raise ServiceError('この依頼はすでに締め切られました。')
    if is_blocked_pair(help_request.requester_id, user.pk):
        # ブロック関係がある相手とはマッチングさせない(締め切られたように見せる)
        raise ServiceError('この依頼はすでに締め切られました。')
    reason = accept_block_reason(user)
    if reason:
        raise ServiceError(reason)
    if not help_request.accepting:
        raise ServiceError('この依頼は募集を締め切りました。')


def accept_first_come(help_request, user):
    if help_request.mode != HelpRequest.Mode.FIRST_COME:
        raise ServiceError('この依頼は承認制です。応募してください。')
    _check_can_help(help_request, user)
    with transaction.atomic():
        match = _claim(help_request, user)
    notify(help_request.requester, 'matched',
           f'{user.username}さんが「{help_request.title}」を引き受けました', detail_url(help_request))
    return match


def apply(help_request, user, message=''):
    if help_request.mode != HelpRequest.Mode.APPROVAL:
        raise ServiceError('この依頼は先着順です。')
    _check_can_help(help_request, user)
    application, created = Application.objects.get_or_create(
        help_request=help_request, applicant=user, defaults={'message': message})
    if not created:
        if application.status != Application.Status.WITHDRAWN:
            raise ServiceError('すでに応募しています。')
        application.status, application.message = Application.Status.APPLIED, message
        application.save(update_fields=['status', 'message'])
    notify(help_request.requester, 'applied',
           f'「{help_request.title}」に{user.username}さんが応募しました', detail_url(help_request))
    return application


def withdraw(application, user):
    if application.applicant_id != user.pk or application.status != Application.Status.APPLIED:
        raise ServiceError('取り消せる応募がありません。')
    application.status = Application.Status.WITHDRAWN
    application.save(update_fields=['status'])


def approve(application, user):
    help_request = application.help_request
    if help_request.requester_id != user.pk:
        raise ServiceError('承認できるのは依頼者だけです。')
    if application.status != Application.Status.APPLIED:
        raise ServiceError('この応募は承認できません。')
    if application.applicant.is_shadow_banned:
        raise ServiceError('この応募は承認できません。')
    if is_blocked_pair(user.pk, application.applicant_id):
        raise ServiceError('この応募は承認できません。')
    reason = accept_block_reason(application.applicant)
    if reason:
        raise ServiceError('この応募者は現在お手伝いを受けられません。')
    with transaction.atomic():
        match = _claim(help_request, application.applicant)
        application.status = Application.Status.ACCEPTED
        application.save(update_fields=['status'])
        others = []
        if help_request.slots_left <= 0:  # 満員になったときだけ、残りの応募を見送る
            others = list(help_request.applications.filter(status=Application.Status.APPLIED).select_related('applicant'))
            help_request.applications.filter(status=Application.Status.APPLIED).update(status=Application.Status.REJECTED)
    url = detail_url(help_request)
    notify(application.applicant, 'matched', f'「{help_request.title}」の応募が承認されました', url)
    for other in others:
        notify(other.applicant, 'rejected', f'「{help_request.title}」は他の方に決まりました', url)
    return match


# ---------- 作業時間(タイマー実測 + 申告) ----------

WORK_ROUND_MINUTES = 5   # 実績時間は5分単位に切り上げる
WORK_CAP_MULTIPLIER = 2  # 自動精算の上限は投稿時間の2倍まで


def _round_up_minutes(minutes):
    return int(ceil(minutes / WORK_ROUND_MINUTES)) * WORK_ROUND_MINUTES


def measured_minutes(match, help_request, now=None):
    """タイマー実測の分数(5分切り上げ、上限は投稿時間の2倍)。未開始なら None。"""
    if not match.work_started_at:
        return None
    now = now or timezone.now()
    end = match.work_ended_at or now
    seconds = (end - match.work_started_at).total_seconds()
    if seconds <= 0:
        return None
    minutes = _round_up_minutes(seconds / 60)
    posted = help_request.duration_minutes or 0
    if posted:
        minutes = min(minutes, posted * WORK_CAP_MULTIPLIER)
    return max(WORK_ROUND_MINUTES, minutes)


def effective_minutes(match, help_request, now=None):
    """精算に使う分数。優先順位: 承認された申告 > 実測 > 申告(実測がない場合) > 投稿時間。"""
    posted = help_request.duration_minutes or 0
    measured = measured_minutes(match, help_request, now)
    if match.declared_minutes:
        declared = _round_up_minutes(match.declared_minutes)
        if posted:
            declared = min(declared, posted * WORK_CAP_MULTIPLIER)
        if measured is None or match.declaration_approved:
            return max(WORK_ROUND_MINUTES, declared)
    if measured is not None:
        return measured
    return posted


def _own_active_match(match, user):
    if match.helper_id != user.pk:
        raise ServiceError('このマッチングのヘルパーではありません。')
    if match.status != Match.Status.IN_PROGRESS:
        raise ServiceError('進行中のマッチングではありません。')


def start_work(match, user):
    _own_active_match(match, user)
    if match.work_started_at:
        raise ServiceError('すでに作業を開始しています。')
    match.work_started_at = timezone.now()
    match.save(update_fields=['work_started_at'])
    return match


def stop_work(match, user):
    _own_active_match(match, user)
    if not match.work_started_at:
        raise ServiceError('まだ作業を開始していません。')
    if match.work_ended_at:
        raise ServiceError('すでに作業を終了しています。')
    match.work_ended_at = timezone.now()
    match.save(update_fields=['work_ended_at'])
    return match


def declare_minutes(match, user, minutes):
    """実績時間の申告。実測がなければそのまま採用し、実測がある場合は
    訂正申告として依頼者の承認待ちにする。"""
    _own_active_match(match, user)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ServiceError('分数を入力してください。')
    if minutes < 1 or minutes > 600:
        raise ServiceError('申告時間は1〜600分で入力してください。')
    match.declared_minutes = minutes
    match.declaration_approved = match.work_started_at is None
    match.save(update_fields=['declared_minutes', 'declaration_approved'])
    return match


def review_declaration(match, user, approve):
    """依頼者が訂正申告を承認/却下する。却下すると申告は取り消される。"""
    if match.help_request.requester_id != user.pk:
        raise ServiceError('申告を確認できるのは依頼者だけです。')
    if match.status != Match.Status.IN_PROGRESS or not match.declared_minutes:
        raise ServiceError('確認できる申告がありません。')
    if approve:
        match.declaration_approved = True
        match.save(update_fields=['declaration_approved'])
    else:
        match.declared_minutes = None
        match.declaration_approved = False
        match.save(update_fields=['declared_minutes', 'declaration_approved'])
    return match


def complete(help_request, user):
    """依頼者が完了を確定。参加していた全ヘルパーにポイントを付与する。"""
    if help_request.requester_id != user.pk:
        raise ServiceError('完了にできるのは依頼者だけです。')
    awarded = {}
    matches = []
    with transaction.atomic():
        # 依頼行をロックして最新の状態で判定する(二重送信・同時実行で精算が二重に走らないように)
        help_request = HelpRequest.objects.select_for_update().get(pk=help_request.pk)
        if help_request.status not in (HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS):
            raise ServiceError('この依頼は既に終了しています。')
        matches = list(help_request.active_matches)
        if not matches:
            raise ServiceError('進行中のマッチングがありません。')
        if help_request.points_cost and not _claim_points_settlement(help_request):
            raise ServiceError('この依頼は既に精算されています。')
        if help_request.points_cost:
            help_request.points_settled = True
            # 実績ベース精算: 各ヘルパーに実効分数×単価を支払う。預かり分を
            # 超えた分は依頼者の残高から追加で引き、余った分は依頼者へ返す。
            now = timezone.now()
            dues = {m.pk: effective_minutes(m, help_request, now) * POINTS_PER_MINUTE
                    for m in matches}
            requester = User.objects.select_for_update().get(pk=help_request.requester_id)
            available = help_request.points_cost + requester.points
            paid_total = 0
            for m in matches:
                pay = min(dues[m.pk], max(0, available - paid_total))
                awarded[m.pk] = pay
                paid_total += pay
            delta = paid_total - help_request.points_cost
            if delta:
                requester.points -= delta
                requester.save(update_fields=['points'])
                record_points(
                    requester, -delta,
                    PointTransaction.Kind.SETTLE_EXTRA if delta > 0 else PointTransaction.Kind.SETTLE_REFUND,
                    help_request,
                    description=f'「{help_request.title}」の実績精算' + ('の追加分' if delta > 0 else 'の返金'))
            help_request.points_settled = True
        else:
            for match in matches:
                awarded[match.pk] = HELPER_POINTS  # ポイント制より前の依頼は従来どおり
        for match in matches:
            match.status, match.completed_at = Match.Status.DONE, timezone.now()
            match.save(update_fields=['status', 'completed_at'])
            match.helper.add_points(awarded[match.pk])
            record_points(match.helper, awarded[match.pk], PointTransaction.Kind.EARNED,
                          help_request, counterparty=help_request.requester,
                          description=f'「{help_request.title}」のお手伝い報酬')
        help_request.status = HelpRequest.Status.DONE
        help_request.save(update_fields=['status', 'points_settled'])
    for match in matches:
        notify(match.helper, 'completed',
               f'「{help_request.title}」が完了しました(+{awarded[match.pk]}pt)', detail_url(help_request))
    return matches[0]


def grace_remaining_seconds(match):
    elapsed = (timezone.now() - match.created_at).total_seconds()
    return max(0, int(CANCEL_GRACE_SECONDS - elapsed))


def cancel(help_request, user, reason=''):
    """ヘルパーのキャンセル=自分の参加だけ取り消し(枠が空く) / 依頼者のキャンセル=依頼取り下げ(全員分)。

    マッチ成立から1分を過ぎたキャンセルはペナルティ対象。戻り値は付与されたペナルティ(なければNone)。
    """
    url = detail_url(help_request)
    is_requester = help_request.requester_id == user.pk
    my_match = help_request.matches.filter(status=Match.Status.IN_PROGRESS, helper=user).first()
    if my_match is None and not (is_requester and help_request.status in (
            HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS)):
        raise ServiceError('キャンセルできません。')

    now = timezone.now()
    penalized_match = None
    canceled_helpers = []
    reopen = False
    with transaction.atomic():
        if is_requester:
            _refund_escrow(help_request)
            for match in help_request.active_matches:
                penalized = grace_remaining_seconds(match) == 0
                match.status, match.canceled_at, match.canceled_by = Match.Status.CANCELED, now, user
                match.cancel_reason, match.penalized = reason[:200], penalized
                match.save(update_fields=['status', 'canceled_at', 'canceled_by', 'cancel_reason', 'penalized'])
                canceled_helpers.append(match.helper)
                if penalized and penalized_match is None:
                    penalized_match = match
            help_request.status = HelpRequest.Status.CANCELED
            help_request.applications.filter(status=Application.Status.APPLIED).update(
                status=Application.Status.REJECTED)
        else:
            match = my_match
            penalized = grace_remaining_seconds(match) == 0
            match.status, match.canceled_at, match.canceled_by = Match.Status.CANCELED, now, user
            match.cancel_reason, match.penalized = reason[:200], penalized
            match.save(update_fields=['status', 'canceled_at', 'canceled_by', 'cancel_reason', 'penalized'])
            if penalized:
                penalized_match = match
            reopen = help_request.expires_at > now
            if help_request.active_matches.exists():
                # 他のヘルパーが作業中なら依頼は継続する。預かり分は完了時の精算まで保持
                help_request.status = HelpRequest.Status.IN_PROGRESS
            elif not reopen:
                _refund_escrow(help_request)
                help_request.status = HelpRequest.Status.EXPIRED
            else:
                help_request.status = HelpRequest.Status.OPEN
        help_request.save(update_fields=['status', 'points_settled'])

    if is_requester:
        for helper in canceled_helpers:
            notify(helper, 'canceled', f'「{help_request.title}」は依頼者により取り下げられました', url)
    else:
        notify(help_request.requester, 'canceled',
               f'「{help_request.title}」のヘルパー({user.username}さん)がキャンセルしました'
               + ('。空いた枠を再募集します' if reopen else ''), url)
        if reopen:
            notify_nearby_helpers(help_request)
    return apply_cancel_penalty(user, penalized_match) if penalized_match is not None else None


def apply_cancel_penalty(user, match):
    since = timezone.now() - timedelta(days=PENALTY_WINDOW_DAYS)
    count = Match.objects.filter(canceled_by=user, penalized=True, canceled_at__gte=since).count()
    level, duration = PENALTY_LADDER.get(count, PENALTY_MAX)
    sanction = Sanction.objects.create(
        user=user, level=level, related_match=match,
        reason=f'マッチング成立から1分を過ぎたキャンセル(直近{PENALTY_WINDOW_DAYS}日で{count}回目)'
               + ('・運営確認待ち' if count > len(PENALTY_LADDER) else ''),
        ends_at=timezone.now() + duration,
    )
    notify(user, 'sanction', f'ペナルティ: {sanction.get_level_display()}({sanction.reason})',
           reverse('core:profile'))
    return sanction


def submit_review(help_request, user, rating, comment=''):
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise ServiceError('評価が不正です。')
    if not 1 <= rating <= 5:
        raise ServiceError('評価は1〜5で入力してください。')
    match = next((m for m in help_request.matches.filter(status=Match.Status.DONE)
                  .select_related('helper') if user.pk in m.participants()), None)
    if help_request.status != HelpRequest.Status.DONE or match is None:
        raise ServiceError('レビューできません。')
    if Review.objects.filter(reviewer=user, help_request=help_request).exists():
        raise ServiceError('すでにレビュー済みです。')
    reviewee = match.other_user(user)
    review = Review.objects.create(reviewer=user, reviewee=reviewee, help_request=help_request,
                                   rating=rating, comment=comment)
    if rating >= 4:
        reviewee.add_points(REVIEW_BONUS_POINTS)
        record_points(reviewee, REVIEW_BONUS_POINTS, PointTransaction.Kind.REVIEW_BONUS,
                      help_request, counterparty=user, description='高評価レビューのボーナス')
    return review


TIP_MAX_POINTS = 200


def tip_helper(match, tipper, amount):
    """完了したお手伝いへのお礼チップ。依頼者の残高からヘルパーへ移す。"""
    help_request = match.help_request
    if help_request.requester_id != tipper.pk:
        raise ServiceError('チップを渡せるのは依頼者だけです。')
    if help_request.status != HelpRequest.Status.DONE or match.status != Match.Status.DONE:
        raise ServiceError('完了したお手伝いにだけチップを渡せます。')
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise ServiceError('チップのポイント数が不正です。')
    if not 1 <= amount <= TIP_MAX_POINTS:
        raise ServiceError(f'チップは1〜{TIP_MAX_POINTS}ptで指定してください。')
    with transaction.atomic():
        payer = User.objects.select_for_update().get(pk=tipper.pk)
        if payer.points < amount:
            raise ServiceError(f'ポイントが不足しています(必要 {amount}pt / 保有 {payer.points}pt)。')
        payer.points -= amount
        payer.save(update_fields=['points'])
        helper = User.objects.get(pk=match.helper_id)
        record_points(payer, -amount, PointTransaction.Kind.TIP_SENT, help_request,
                      counterparty=helper,
                      description=f'「{help_request.title}」のお礼チップ')
        helper.add_points(amount)
        record_points(helper, amount, PointTransaction.Kind.TIP_RECEIVED, help_request,
                      counterparty=payer,
                      description=f'「{help_request.title}」のお礼チップ')
    notify(match.helper, 'tip',
           f'{tipper.username}さんから「{help_request.title}」のお礼に{amount}ptのチップが届きました',
           detail_url(help_request))
    return amount


def _notify_schedule_reminder(help_request, now, day_before):
    local = timezone.localtime(help_request.scheduled_for)
    local_now = timezone.localtime(now)
    if local.date() == local_now.date() + timedelta(days=1):
        when_label = f'明日{local:%H:%M}'
    elif local.date() == local_now.date():
        when_label = f'今日{local:%H:%M}'
    else:
        when_label = f'{local.month}/{local.day} {local:%H:%M}'
    if day_before:
        text = f'{when_label}〜「{help_request.title}」の予定です'
    else:
        text = f'まもなく{when_label}〜「{help_request.title}」の開始です'
    url = detail_url(help_request)
    count = 0
    notify(help_request.requester, 'schedule_reminder', text, url)
    count += 1
    for match in help_request.active_matches:
        notify(match.helper, 'schedule_reminder', text, url)
        count += 1
    return count


INVITE_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'  # 紛らわしい 0/O/1/I/L を除く
INVITE_CODE_LENGTH = 8
REFERRAL_REGISTER_HOURS = 48  # 招待コードを登録できるのはアカウント作成から48時間以内


def ensure_invite_code(user):
    """そのユーザーの招待コードを返す(未生成なら作る)。"""
    if user.invite_code:
        return user.invite_code
    from django.utils.crypto import get_random_string
    for _ in range(20):
        code = get_random_string(INVITE_CODE_LENGTH, INVITE_CODE_ALPHABET)
        if not User.objects.filter(invite_code=code).exists():
            User.objects.filter(pk=user.pk).update(invite_code=code)
            user.invite_code = code
            return code
    raise ServiceError('招待コードを生成できませんでした。')


def find_referrer_by_ref(ref):
    """招待リンクの ref(コード優先、旧リンク互換でユーザー名も可)から紹介者を引く。"""
    ref = (ref or '').strip()
    if not ref:
        return None
    return (User.objects.filter(invite_code=ref.upper(), is_active=True).first()
            or User.objects.filter(username=ref, is_active=True).first())


def referral_window_open(user):
    """招待コードをまだ登録できるか(未登録かつ作成から48時間以内)。"""
    return (user.referred_by_id is None
            and timezone.now() - user.date_joined <= timedelta(hours=REFERRAL_REGISTER_HOURS))


def register_invite_code(user, code):
    """招待コードの入力で紹介者を登録する。登録できたら紹介者を返す。"""
    code = (code or '').strip().upper()
    if not code:
        raise ServiceError('招待コードを入力してください。')
    if user.referred_by_id is not None:
        raise ServiceError('すでに招待が登録されています。')
    if timezone.now() - user.date_joined > timedelta(hours=REFERRAL_REGISTER_HOURS):
        raise ServiceError('招待コードの登録は、アカウント作成から48時間以内です。')
    referrer = User.objects.filter(invite_code=code, is_active=True).first()
    if referrer is None:
        raise ServiceError('その招待コードは見つかりませんでした。')
    if referrer.pk == user.pk:
        raise ServiceError('自分の招待コードは登録できません。')
    user.referred_by = referrer
    user.save(update_fields=['referred_by'])
    grant_referral_reward(user)  # 参加条件を満たしていればその場で紹介者へ付与
    return referrer


def referral_gate_passed(user):
    """招待ボーナスの参加条件：顔写真の承認済み、または写真免除(大学メール確認済み)。"""
    return bool(user.photo_approved or photo_exempt(user))


def grant_referral_reward(invitee):
    """招待ボーナスの条件を満たしていれば紹介者へ付与する(冪等)。付与したら True。"""
    if invitee.referred_by_id is None or invitee.referral_reward_at is not None:
        return False
    if not referral_gate_passed(invitee):
        return False
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=invitee.pk)
        if locked.referral_reward_at is not None or locked.referred_by_id is None:
            return False
        locked.referral_reward_at = timezone.now()
        locked.save(update_fields=['referral_reward_at'])
        referrer_id = locked.referred_by_id
    referrer = User.objects.get(pk=referrer_id)
    referrer.add_points(REFERRAL_REWARD_POINTS)
    record_points(referrer, REFERRAL_REWARD_POINTS, PointTransaction.Kind.REFERRAL, None,
                  counterparty=invitee,
                  description=f'{invitee.username}さんの招待ボーナス')
    notify(referrer, 'points',
           f'🎉 {invitee.username}さんが参加しました！招待ボーナス +{REFERRAL_REWARD_POINTS}pt を受け取りました。',
           '/me/points/')
    return True


def process_referral_rewards():
    """招待ボーナスの未付与分をまとめて処理する(定期処理から呼ぶ)。付与件数を返す。"""
    granted = 0
    invitees = (User.objects.filter(referred_by__isnull=False, referral_reward_at__isnull=True)
                .select_related('referred_by')[:100])
    for invitee in invitees:
        if grant_referral_reward(invitee):
            granted += 1
    return granted


# ---------- ポイントが貯まるミッション ----------

def _mission_photo(user):
    return referral_gate_passed(user)


def _mission_profile(user):
    return bool(user.bio.strip()) and user.skills.exists()


def _mission_first_help(user):
    return Match.objects.filter(helper=user, status=Match.Status.DONE).exists()


def _mission_first_request(user):
    return HelpRequest.objects.filter(requester=user, status=HelpRequest.Status.DONE).exists()


MISSIONS = [
    {'key': 'photo', 'title': '顔写真を登録して承認を受ける', 'points': 10, 'check': _mission_photo},
    {'key': 'profile', 'title': 'プロフィールを完成させる(自己紹介＋できること)', 'points': 10,
     'check': _mission_profile},
    {'key': 'first_help', 'title': '初めてお手伝いを完了する', 'points': 10, 'check': _mission_first_help},
    {'key': 'first_request', 'title': '初めて自分の依頼を完了する', 'points': 10,
     'check': _mission_first_request},
]


def mission_states(user):
    """ミッション一覧に達成/受け取り状態を付けて返す。"""
    claimed = set(user.mission_claims.values_list('key', flat=True))
    return [{**mission, 'achieved': mission['check'](user), 'claimed': mission['key'] in claimed}
            for mission in MISSIONS]


def claim_mission(user, key):
    """達成済みミッションのボーナスを受け取る。付与ポイントを返す。"""
    mission = next((m for m in MISSIONS if m['key'] == key), None)
    if mission is None:
        raise ServiceError('そのミッションはありません。')
    if user.mission_claims.filter(key=key).exists():
        raise ServiceError('このミッションは受け取り済みです。')
    if not mission['check'](user):
        raise ServiceError('まだ達成していません。')
    MissionClaim.objects.create(user=user, key=key)
    user.add_points(mission['points'])
    record_points(user, mission['points'], PointTransaction.Kind.MISSION, None,
                  description=f'ミッション達成：{mission["title"]}')
    return mission['points']


def grant_login_bonus(user):
    """1日1回のログインボーナス。今日すでに付与済みなら False。"""
    today = timezone.localdate()
    updated = (User.objects.filter(pk=user.pk)
               .exclude(last_login_bonus_date=today)
               .update(last_login_bonus_date=today))
    if not updated:
        return False
    user.add_points(LOGIN_BONUS_POINTS)
    record_points(user, LOGIN_BONUS_POINTS, PointTransaction.Kind.LOGIN_BONUS, None,
                  description='ログインボーナス')
    return True


def send_schedule_reminders(now=None):
    """日時指定の依頼について、前日(24時間前)と1時間前に依頼者と参加中のヘルパーへ知らせる。"""
    now = now or timezone.now()
    sent = 0
    base = HelpRequest.objects.filter(
        scheduled_for__isnull=False, scheduled_for__gt=now,
        status__in=(HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS))
    # 前日分: 開始まで24時間以内。ただし1時間を切ったものは1時間前リマインダーに任せる
    day_qs = base.filter(scheduled_for__lte=now + timedelta(hours=24),
                         scheduled_for__gt=now + timedelta(hours=1),
                         reminded_day_before_at__isnull=True)
    for help_request in day_qs:
        claimed = HelpRequest.objects.filter(
            pk=help_request.pk, reminded_day_before_at__isnull=True).update(
            reminded_day_before_at=now)
        if claimed:
            sent += _notify_schedule_reminder(help_request, now, day_before=True)
    hour_qs = base.filter(scheduled_for__lte=now + timedelta(hours=1),
                          reminded_hour_before_at__isnull=True)
    for help_request in hour_qs:
        claimed = HelpRequest.objects.filter(
            pk=help_request.pk, reminded_hour_before_at__isnull=True).update(
            reminded_hour_before_at=now)
        if claimed:
            sent += _notify_schedule_reminder(help_request, now, day_before=False)
    return sent


def expire_requests():
    """掲載期限を過ぎた募集を締め切る。定期処理から呼ぶ。"""
    expired = list(HelpRequest.objects.filter(status=HelpRequest.Status.OPEN, expires_at__lte=timezone.now())
                   .select_related('requester'))
    for help_request in expired:
        _refund_escrow(help_request)
        help_request.status = HelpRequest.Status.EXPIRED
        help_request.save(update_fields=['status', 'points_settled'])
        help_request.applications.filter(status=Application.Status.APPLIED).update(
            status=Application.Status.REJECTED)
        notify(help_request.requester, 'expired',
               f'「{help_request.title}」は掲載期限を過ぎたため締め切りました', detail_url(help_request))
    return len(expired)


# ---------- チャット ----------

# チャットで送れるスタンプ(キー -> 絵文字, ラベル)。LINEスタンプのように大きく表示する
STAMPS = {
    'thanks': ('🙏', 'ありがとう'),
    'good': ('👍', 'いいね'),
    'heart': ('❤️', 'だいすき'),
    'smile': ('😊', 'にっこり'),
    'laugh': ('😆', 'わはは'),
    'wow': ('😮', 'すごい'),
    'party': ('🎉', 'おめでとう'),
    'ganbare': ('💪', 'がんばって'),
    'otsukare': ('😴', 'おつかれさま'),
}


def stamp_info(stamp):
    """スタンプキーから (絵文字, ラベル) を返す。不正なキーは None。"""
    return STAMPS.get(stamp or '')


def chat_group_name(help_request):
    """グループチャットの配信グループ名(依頼単位)。"""
    return f'chat_{help_request.pk}'


def chat_participants(help_request):
    """チャット参加者のID集合: 依頼者 + キャンセル以外のマッチのヘルパー。"""
    ids = {help_request.requester_id}
    ids.update(help_request.matches.exclude(status=Match.Status.CANCELED)
               .values_list('helper_id', flat=True))
    return ids


def chat_is_open(match):
    """送信できるのは進行中だけ。完了・キャンセルした時点で送信は終了する。"""
    return match.status == Match.Status.IN_PROGRESS


def sender_can_post(help_request, sender):
    if help_request.status != HelpRequest.Status.IN_PROGRESS:
        return False
    if sender.pk == help_request.requester_id:
        return help_request.active_matches.exists()
    return help_request.matches.filter(status=Match.Status.IN_PROGRESS, helper=sender).exists()


def request_ended_at(help_request):
    """依頼の終了日時(全マッチの完了・キャンセルのうち最も遅いもの)。未終了なら None。"""
    times = []
    for completed_at, canceled_at in help_request.matches.filter(
            status__in=[Match.Status.DONE, Match.Status.CANCELED]).values_list('completed_at', 'canceled_at'):
        ended = completed_at or canceled_at
        if ended is not None:
            times.append(ended)
    return max(times) if times else None


def chat_retention_deadline(match):
    """チャット履歴の保存期限。進行中・募集中(終了日時なし)は None。"""
    help_request = match.help_request
    if help_request.status in (HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS):
        return None
    ended_at = request_ended_at(help_request)
    if ended_at is None:
        return None
    return ended_at + timedelta(days=help_request.chat_retention_days)


def chat_history_visible(match):
    deadline = chat_retention_deadline(match)
    return deadline is None or timezone.now() < deadline


def open_chats(user):
    """チャットを開けるマッチング(進行中、または完了後で保存期間内)。グループは依頼ごとに1件にまとめる。"""
    matches = (Match.objects.filter(Q(helper=user) | Q(help_request__requester=user))
               .filter(Q(status=Match.Status.IN_PROGRESS) | Q(status=Match.Status.DONE))
               .select_related('help_request__requester', 'helper').order_by('-created_at'))
    result, seen_requests = [], set()
    for match in matches:
        if not chat_history_visible(match):
            continue
        if match.help_request.requester_id == user.pk:
            if match.help_request_id in seen_requests:
                continue
            seen_requests.add(match.help_request_id)
        result.append(match)
    return result


def chat_counterpart(match, user):
    """そのチャットの「相手」を返す。1対1なら相手ユーザー、依頼者側で複数ヘルパーが
    参加しているグループチャットなら None(人にまとめられない)。"""
    help_request = match.help_request
    if help_request.requester_id != user.pk:
        return help_request.requester
    helpers = {m.helper_id: m.helper for m in help_request.matches.select_related('helper')
               if m.status in (Match.Status.IN_PROGRESS, Match.Status.DONE)}
    if len(helpers) == 1:
        return next(iter(helpers.values()))
    return None


def group_chats_by_person(user, chats):
    """メッセージ一覧用に、開けるチャットを相手ごとにまとめる。
    戻り値: (person_groups, group_chats)
      person_groups: [{'person': User, 'chats': [match...]}] (相手1人とのチャット群)
      group_chats: [match...] (複数ヘルパーのグループチャット。依頼単位でそのまま出す)
    各 match には呼び出し側で other / last_message / unread_count を付けておくこと。"""
    by_person, group_chats = {}, []
    for match in chats:
        person = chat_counterpart(match, user)
        if person is None:
            group_chats.append(match)
        else:
            by_person.setdefault(person.pk, {'person': person, 'chats': []})['chats'].append(match)
    person_groups = list(by_person.values())
    for group in person_groups:
        group['unread_total'] = sum(c.unread_count for c in group['chats'])
        group['active_count'] = sum(1 for c in group['chats'] if c.status == Match.Status.IN_PROGRESS)
        latest = max(group['chats'],
                     key=lambda c: c.last_message.created_at if c.last_message else c.created_at)
        group['latest'] = latest
        group['last_at'] = latest.last_message.created_at if latest.last_message else latest.created_at
    person_groups.sort(key=lambda g: g['last_at'], reverse=True)
    group_chats.sort(
        key=lambda c: c.last_message.created_at if c.last_message else c.created_at, reverse=True)
    return person_groups, group_chats


def purge_expired_chat_messages():
    """保存期間を過ぎた完了・キャンセル済み依頼のメッセージを削除する。定期処理から呼ぶ。"""
    now = timezone.now()
    targets = []
    for help_request in HelpRequest.objects.filter(
            status__in=[HelpRequest.Status.DONE, HelpRequest.Status.CANCELED,
                        HelpRequest.Status.EXPIRED]):
        ended_at = request_ended_at(help_request)
        if ended_at is not None and ended_at + timedelta(days=help_request.chat_retention_days) <= now:
            targets.append(help_request.pk)
    if not targets:
        return 0
    messages_qs = Message.objects.filter(help_request_id__in=targets)
    for message in messages_qs.only('image'):
        if message.image:
            message.image.delete(save=False)  # 保存ファイルも消す(本文と一緒に破棄)
    deleted, _ = messages_qs.delete()
    return deleted


REACTION_EMOJIS = ['❤️', '👍', '😊', '😮', '🙏', '🎉']


def reaction_summary(message):
    """リアクションの集計 [{emoji, count, user_ids}] を固定の絵文字順で返す。"""
    grouped = {}
    for reaction in message.reactions.all():
        entry = grouped.setdefault(reaction.emoji, {'emoji': reaction.emoji, 'count': 0, 'user_ids': []})
        entry['count'] += 1
        entry['user_ids'].append(reaction.user_id)
    return [grouped[e] for e in REACTION_EMOJIS if e in grouped]


def toggle_reaction(message, user, emoji):
    """リアクションの付け外し。付けたら True。参加者以外・対象外の絵文字はエラー。"""
    if emoji not in REACTION_EMOJIS:
        raise ServiceError('そのリアクションは使えません')
    if message.help_request_id is None or user.pk not in chat_participants(message.help_request):
        raise ServiceError('この会話の参加者だけがリアクションできます')
    request_status = HelpRequest.objects.values_list('status', flat=True).get(pk=message.help_request_id)
    if request_status in (HelpRequest.Status.DONE, HelpRequest.Status.CANCELED,
                          HelpRequest.Status.EXPIRED):
        raise ServiceError('この会話は終了しているため、リアクションできません')
    reaction, created = MessageReaction.objects.get_or_create(message=message, user=user, emoji=emoji)
    if not created:
        reaction.delete()
    push(chat_group_name(message.help_request),
         {'type': 'chat.reaction', 'message_id': message.pk, 'reactions': reaction_summary(message)})
    return created


def mark_chat_read(help_request, user):
    """依頼のチャットで、自分以外が送った表示可能なメッセージを既読にする。新しく既読になった件数を返す。"""
    if user.pk not in chat_participants(help_request):
        return 0
    ids = list(Message.objects.filter(help_request=help_request, is_hidden=False)
               .exclude(sender=user).exclude(reads__user=user).values_list('pk', flat=True))
    if not ids:
        return 0
    MessageRead.objects.bulk_create(
        [MessageRead(message_id=message_id, user=user) for message_id in ids], ignore_conflicts=True)
    Message.objects.filter(pk__in=ids, read_at__isnull=True).update(read_at=timezone.now())
    return len(ids)


def reply_excerpt(message):
    """返信先メッセージの引用表示用テキスト。"""
    if message.is_hidden:
        return 'このメッセージは表示されていません'
    if message.body:
        return message.body[:60]
    if message.image:
        return '📷 写真'
    info = stamp_info(message.stamp)
    if info:
        return f'{info[0]} スタンプ'
    return ''


def message_payload(message):
    reply = None
    if message.reply_to_id:
        target = message.reply_to
        reply = {'id': target.pk, 'sender': target.sender.username,
                 'excerpt': reply_excerpt(target)}
    return {
        'id': message.pk,
        'reply_to': reply,
        'sender': message.sender.username,
        'sender_id': message.sender_id,
        'body': '' if message.is_hidden else message.body,
        'stamp': '' if message.is_hidden else message.stamp,
        'stamp_emoji': '' if message.is_hidden else (stamp_info(message.stamp) or ('', ''))[0],
        'stamp_label': '' if message.is_hidden else (stamp_info(message.stamp) or ('', ''))[1],
        'image_url': '' if message.is_hidden or not message.image else reverse('core:chat_image', args=[message.pk]),
        'avatar_url': reverse('core:user_photo', args=[message.sender_id]) if message.sender.avatar else '',
        'report_url': reverse('core:report_user', args=[message.sender.username]),
        'read_count': message.reads.exclude(user_id=message.sender_id).count(),
        'reactions': reaction_summary(message) if message.pk else [],
        'hidden': message.is_hidden,
        'created_at': timezone.localtime(message.created_at).strftime('%H:%M'),
    }


def post_message(match, sender, body, image_file=None, reply_to_id=None, stamp=''):
    body = (body or '').strip()
    stamp = stamp or ''
    if stamp and stamp not in STAMPS:
        raise ServiceError('スタンプが不正です。')
    help_request = match.help_request
    if sender.pk not in chat_participants(help_request):
        raise ServiceError('このチャットには参加していません。')
    if not sender_can_post(help_request, sender):
        raise ServiceError('このチャットは終了しています。')
    if len(body) > 1000:
        raise ServiceError('メッセージは1000文字以内で入力してください。')
    image = None
    if image_file is not None:
        try:
            image = images.normalize_photo(image_file)
        except images.InvalidImage as error:
            raise ServiceError(str(error))
    if not body and image is None and not stamp:
        raise ServiceError('メッセージを入力してください。')

    score, labels = moderation.assess(match, sender, body)
    hidden = settings.MODERATION_AUTO_HIDE and score >= settings.MODERATION_HIDE_THRESHOLD
    own_match = (match if sender.pk == match.helper_id else
                 help_request.matches.filter(status=Match.Status.IN_PROGRESS, helper=sender).first() or match)
    reply_to = None
    if reply_to_id:
        try:
            reply_pk = int(reply_to_id)
        except (TypeError, ValueError):
            reply_pk = None
        if reply_pk:
            # 返信先は同じチャットの表示中メッセージだけを許可する
            reply_to = Message.objects.filter(pk=reply_pk, help_request=help_request,
                                              is_hidden=False).first()
    message = Message(match=own_match, help_request=help_request, sender=sender, body=body, stamp=stamp,
                      reply_to=reply_to,
                      risk_score=score, risk_labels=labels, is_hidden=hidden)
    if image is not None:
        message.image = image
    message.save()
    if hidden:
        push(f'user_{sender.pk}', {'type': 'notify', 'kind': 'warning', 'url': '',
                                    'message': '不適切な可能性がある内容のため、メッセージは相手に表示されませんでした。'})
    push(chat_group_name(help_request), {'type': 'chat.message', 'message': message_payload(message)})

    url = reverse('core:chat', args=[match.pk])
    if not hidden:
        for other in User.objects.filter(pk__in=chat_participants(help_request) - {sender.pk}):
            if not Notification.objects.filter(user=other, kind='message', url=url, is_read=False).exists():
                notify(other, 'message', f'{sender.username}さんから新しいメッセージ', url)
    return message


# ---------- 通報 ----------

def file_report(reporter, target, category, detail='', help_request=None, message=None):
    if reporter.pk == target.pk:
        raise ServiceError('自分自身は通報できません。')
    report = Report.objects.create(reporter=reporter, target_user=target, category=category, detail=detail,
                                   help_request=help_request, message=message)
    since = timezone.now() - timedelta(days=REPORT_WINDOW_DAYS)
    reporter_ids = (Report.objects.filter(target_user=target, created_at__gte=since)
                    .values_list('reporter', flat=True).distinct())
    # 自動ペナルティの集計は、顔写真の承認済み(または承認免除)の通報者だけを数える。
    # 捨てアカウントの通報だけで即時ペナルティが発動しないようにするため
    reporters = sum(1 for u in User.objects.filter(pk__in=reporter_ids)
                    if u.avatar_status == User.PhotoStatus.APPROVED or photo_exempt(u))
    if reporters >= REPORT_AUTO_SANCTION_REPORTERS and strongest_sanction(target) is None:
        Sanction.objects.create(
            user=target, level=Sanction.Level.NO_ACCEPT,
            reason=f'直近{REPORT_WINDOW_DAYS}日で{reporters}人から通報(運営確認待ち)',
            ends_at=timezone.now() + timedelta(days=REPORT_SANCTION_DAYS))
    return report


# ---------- 画面の常時表示・統計 ----------

def active_items(user):
    """「進行中の依頼」バー用。"""
    qs = (Match.objects.filter(Q(help_request__requester=user) | Q(helper=user),
                               status=Match.Status.IN_PROGRESS)
          .select_related('help_request', 'helper', 'help_request__requester'))
    matches, seen_requests = [], set()
    for m in qs:
        if m.help_request.requester_id == user.pk:
            if m.help_request_id in seen_requests:
                continue
            seen_requests.add(m.help_request_id)
        matches.append(m)
    pending = (HelpRequest.objects.filter(requester=user, status__in=[
                    HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS],
                                          applications__status=Application.Status.APPLIED)
               .annotate(applicants=Count('applications')).distinct())
    return matches, list(pending)


def hourly_stats(area='', days=90):
    """地域別・時間帯別の依頼件数(0〜23時)。"""
    qs = HelpRequest.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
    if area:
        qs = qs.filter(area_label__startswith=area)
    counts = dict(qs.annotate(hour=ExtractHour('created_at')).values('hour')
                  .annotate(c=Count('id')).values_list('hour', 'c'))
    return [counts.get(h, 0) for h in range(24)]


def top_areas(days=90, limit=10):
    return list(HelpRequest.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
                .exclude(area_label='').values('area_label').annotate(c=Count('id')).order_by('-c')[:limit])
