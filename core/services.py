"""マッチングのドメインロジック。ビューとWebSocketの両方から使う。"""
from collections import defaultdict
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import ExtractHour
from django.urls import reverse
from django.utils import timezone

from . import images, line_api, moderation
from .geocoding import area_label_for
from .models import (ActivityBase, Application, HelpRequest, Match, Message, Notification,
                     NotificationSetting, Report, Review, Sanction, User)

RADIUS_METERS = 500
HELPER_POINTS = 10
REVIEW_BONUS_POINTS = 2
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

CHAT_OPEN_DAYS_AFTER_DONE = 3
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


def photo_block_reason(user):
    """顔写真の審査が済んでいないと、依頼も応募もできない。"""
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
    """通知履歴に残し、接続中ならWebSocketでも届ける。メールは定期処理でまとめて送る。"""
    notification = Notification.objects.create(user=user, kind=kind, text=text[:200], url=url)
    push(f'user_{user.pk}', {'type': 'notify', 'kind': kind, 'message': notification.text, 'url': url})
    line_api.maybe_notify(user, kind, notification.text, url)
    return notification


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
    when = (f'{timezone.localtime(help_request.scheduled_for):%m/%d %H:%M}〜'
            if help_request.scheduled_for else '今すぐ')
    text = f'近くで依頼「{help_request.title}」({when}・{help_request.duration_minutes}分)'
    helpers = nearby_helpers(help_request)
    for user in helpers:
        notify(user, 'nearby_request', text, detail_url(help_request))
    return len(helpers)


# ---------- 依頼のライフサイクル ----------

def publish_request(help_request):
    help_request.area_label = area_label_for(help_request.latitude, help_request.longitude)
    help_request.save(update_fields=['area_label'])
    return notify_nearby_helpers(help_request)


def _claim(help_request, helper):
    """先着1名: 募集中のものだけを原子的に進行中へ更新する。"""
    claimed = HelpRequest.objects.filter(
        pk=help_request.pk, status=HelpRequest.Status.OPEN, expires_at__gt=timezone.now(),
    ).update(status=HelpRequest.Status.IN_PROGRESS)
    if not claimed:
        raise ServiceError('この依頼はすでに締め切られました。')
    help_request.status = HelpRequest.Status.IN_PROGRESS
    return Match.objects.create(help_request=help_request, helper=helper)


def _check_can_help(help_request, user):
    if help_request.requester_id == user.pk:
        raise ServiceError('自分の依頼は受けられません。')
    reason = accept_block_reason(user)
    if reason:
        raise ServiceError(reason)
    if not help_request.is_open:
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
    reason = accept_block_reason(application.applicant)
    if reason:
        raise ServiceError('この応募者は現在お手伝いを受けられません。')
    with transaction.atomic():
        match = _claim(help_request, application.applicant)
        application.status = Application.Status.ACCEPTED
        application.save(update_fields=['status'])
        others = list(help_request.applications.filter(status=Application.Status.APPLIED).select_related('applicant'))
        help_request.applications.filter(status=Application.Status.APPLIED).update(status=Application.Status.REJECTED)
    url = detail_url(help_request)
    notify(application.applicant, 'matched', f'「{help_request.title}」の応募が承認されました', url)
    for other in others:
        notify(other.applicant, 'rejected', f'「{help_request.title}」は他の方に決まりました', url)
    return match


def complete(help_request, user):
    """依頼者が完了を確定。ヘルパーにポイントを付与する。"""
    if help_request.requester_id != user.pk:
        raise ServiceError('完了にできるのは依頼者だけです。')
    match = help_request.active_match
    if match is None:
        raise ServiceError('進行中のマッチングがありません。')
    with transaction.atomic():
        match.status, match.completed_at = Match.Status.DONE, timezone.now()
        match.save(update_fields=['status', 'completed_at'])
        help_request.status = HelpRequest.Status.DONE
        help_request.save(update_fields=['status'])
        match.helper.add_points(HELPER_POINTS)
    notify(match.helper, 'completed',
           f'「{help_request.title}」が完了しました(+{HELPER_POINTS}pt)', detail_url(help_request))
    return match


def grace_remaining_seconds(match):
    elapsed = (timezone.now() - match.created_at).total_seconds()
    return max(0, int(CANCEL_GRACE_SECONDS - elapsed))


def cancel(help_request, user, reason=''):
    """ヘルパーのキャンセル=再募集 / 依頼者のキャンセル=依頼取り下げ。

    マッチ成立から1分を過ぎたキャンセルはペナルティ対象。戻り値は付与されたペナルティ(なければNone)。
    """
    match = help_request.active_match
    url = detail_url(help_request)
    is_helper = match is not None and match.helper_id == user.pk
    is_requester = help_request.requester_id == user.pk
    if not is_helper and not (is_requester and help_request.status in (
            HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS)):
        raise ServiceError('キャンセルできません。')

    penalized = match is not None and grace_remaining_seconds(match) == 0
    with transaction.atomic():
        if match is not None:
            match.status, match.canceled_at, match.canceled_by = Match.Status.CANCELED, timezone.now(), user
            match.cancel_reason, match.penalized = reason[:200], penalized
            match.save(update_fields=['status', 'canceled_at', 'canceled_by', 'cancel_reason', 'penalized'])
        if is_helper:
            reopen = help_request.expires_at > timezone.now()
            help_request.status = HelpRequest.Status.OPEN if reopen else HelpRequest.Status.EXPIRED
        else:
            help_request.status = HelpRequest.Status.CANCELED
            help_request.applications.filter(status=Application.Status.APPLIED).update(
                status=Application.Status.REJECTED)
        help_request.save(update_fields=['status'])

    if is_helper:
        notify(help_request.requester, 'canceled',
               f'「{help_request.title}」のヘルパーがキャンセルしました' + ('。再募集します' if reopen else ''), url)
        if reopen:
            notify_nearby_helpers(help_request)
    elif match is not None:
        notify(match.helper, 'canceled', f'「{help_request.title}」は依頼者により取り下げられました', url)
    return apply_cancel_penalty(user, match) if penalized else None


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
    match = help_request.matches.filter(status=Match.Status.DONE).select_related('helper').first()
    if help_request.status != HelpRequest.Status.DONE or match is None or user.pk not in match.participants():
        raise ServiceError('レビューできません。')
    if Review.objects.filter(reviewer=user, help_request=help_request).exists():
        raise ServiceError('すでにレビュー済みです。')
    reviewee = match.other_user(user)
    review = Review.objects.create(reviewer=user, reviewee=reviewee, help_request=help_request,
                                   rating=rating, comment=comment)
    if rating >= 4:
        reviewee.add_points(REVIEW_BONUS_POINTS)
    return review


def expire_requests():
    """掲載期限を過ぎた募集を締め切る。定期処理から呼ぶ。"""
    expired = list(HelpRequest.objects.filter(status=HelpRequest.Status.OPEN, expires_at__lte=timezone.now())
                   .select_related('requester'))
    for help_request in expired:
        help_request.status = HelpRequest.Status.EXPIRED
        help_request.save(update_fields=['status'])
        help_request.applications.filter(status=Application.Status.APPLIED).update(
            status=Application.Status.REJECTED)
        notify(help_request.requester, 'expired',
               f'「{help_request.title}」は掲載期限を過ぎたため締め切りました', detail_url(help_request))
    return len(expired)


# ---------- チャット ----------

def chat_is_open(match):
    if match.status == Match.Status.IN_PROGRESS:
        return True
    return (match.status == Match.Status.DONE and match.completed_at is not None
            and match.completed_at > timezone.now() - timedelta(days=CHAT_OPEN_DAYS_AFTER_DONE))


def open_chats(user):
    """メッセージできるマッチング(進行中、または完了から3日以内)。"""
    since = timezone.now() - timedelta(days=CHAT_OPEN_DAYS_AFTER_DONE)
    return list(Match.objects.filter(Q(helper=user) | Q(help_request__requester=user))
                .filter(Q(status=Match.Status.IN_PROGRESS) | Q(status=Match.Status.DONE, completed_at__gt=since))
                .select_related('help_request__requester', 'helper').order_by('-created_at'))


def message_payload(message):
    return {
        'id': message.pk,
        'sender': message.sender.username,
        'sender_id': message.sender_id,
        'body': '' if message.is_hidden else message.body,
        'hidden': message.is_hidden,
        'created_at': timezone.localtime(message.created_at).strftime('%H:%M'),
    }


def post_message(match, sender, body):
    body = (body or '').strip()
    if sender.pk not in match.participants():
        raise ServiceError('このチャットには参加していません。')
    if not chat_is_open(match):
        raise ServiceError('このチャットは終了しています。')
    if not body:
        raise ServiceError('メッセージを入力してください。')
    if len(body) > 1000:
        raise ServiceError('メッセージは1000文字以内で入力してください。')

    score, labels = moderation.assess(match, sender, body)
    hidden = settings.MODERATION_AUTO_HIDE and score >= settings.MODERATION_HIDE_THRESHOLD
    message = Message.objects.create(match=match, sender=sender, body=body,
                                     risk_score=score, risk_labels=labels, is_hidden=hidden)
    if hidden:
        push(f'user_{sender.pk}', {'type': 'notify', 'kind': 'warning', 'url': '',
                                    'message': '不適切な可能性がある内容のため、メッセージは相手に表示されませんでした。'})
    push(f'chat_{match.pk}', {'type': 'chat.message', 'message': message_payload(message)})

    other = match.other_user(sender)
    url = reverse('core:chat', args=[match.pk])
    if not hidden and not Notification.objects.filter(user=other, kind='message', url=url, is_read=False).exists():
        notify(other, 'message', f'{sender.username}さんから新しいメッセージ', url)
    return message


# ---------- 通報 ----------

def file_report(reporter, target, category, detail='', help_request=None, message=None):
    if reporter.pk == target.pk:
        raise ServiceError('自分自身は通報できません。')
    report = Report.objects.create(reporter=reporter, target_user=target, category=category, detail=detail,
                                   help_request=help_request, message=message)
    since = timezone.now() - timedelta(days=REPORT_WINDOW_DAYS)
    reporters = (Report.objects.filter(target_user=target, created_at__gte=since)
                 .values('reporter').distinct().count())
    if reporters >= REPORT_AUTO_SANCTION_REPORTERS and strongest_sanction(target) is None:
        Sanction.objects.create(
            user=target, level=Sanction.Level.NO_ACCEPT,
            reason=f'直近{REPORT_WINDOW_DAYS}日で{reporters}人から通報(運営確認待ち)',
            ends_at=timezone.now() + timedelta(days=REPORT_SANCTION_DAYS))
    return report


# ---------- 画面の常時表示・メール・統計 ----------

def active_items(user):
    """「進行中の依頼」バー用。"""
    matches = Match.objects.filter(Q(help_request__requester=user) | Q(helper=user),
                                   status=Match.Status.IN_PROGRESS)
    pending = (HelpRequest.objects.filter(requester=user, status=HelpRequest.Status.OPEN,
                                          applications__status=Application.Status.APPLIED)
               .annotate(applicants=Count('applications')).distinct())
    return list(matches.select_related('help_request', 'helper', 'help_request__requester')), list(pending)


def send_email_digests(now=None):
    """未読の通知をユーザーごとに1通のメールにまとめて送る。オンライン中・静かな時間帯は送らない。"""
    now = now or timezone.now()
    grouped = defaultdict(list)
    for n in Notification.objects.filter(email_processed_at__isnull=True).select_related('user'):
        grouped[n.user].append(n)

    sent = 0
    for user, notes in grouped.items():
        setting = notification_setting(user)
        if setting.is_quiet(now):
            continue  # 静かな時間帯が明けてから送る
        to_send = [] if is_online(user.pk) or not user.email else [
            n for n in notes if not n.is_read
            and (setting.email_nearby if n.kind == 'nearby_request' else setting.email_activity)]
        if to_send:
            lines = [f'・{n.text}\n  {settings.SITE_URL}{n.url}' for n in to_send]
            send_mail(f'[Suki-match] 新しいお知らせが{len(to_send)}件あります',
                      '\n'.join(lines) + '\n\n通知メールの設定はマイページから変更できます。',
                      settings.DEFAULT_FROM_EMAIL, [user.email])
            sent += 1
        Notification.objects.filter(pk__in=[n.pk for n in notes]).update(email_processed_at=now)
    return sent


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
