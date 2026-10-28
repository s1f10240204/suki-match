import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.staticfiles import finders
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, F, Q
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.templatetags.static import static

from . import images, line_api, line_chat, services, skills
from .forms import (AccountForm, ActivityBaseForm, ApplicationForm, CancelForm, HelpRequestForm, MessageForm,
                    NotificationSettingForm, ProfileForm, ReportForm, ReviewForm)
from .geocoding import area_label_for
from .models import Application, HelpRequest, Match, Message, PresetTag, Review, Sanction, Skill, User
from .services import ServiceError


def _run(request, action, success=''):
    """サービス呼び出しの ServiceError をメッセージ表示に変換する。"""
    try:
        result = action()
    except ServiceError as e:
        messages.error(request, str(e))
        return None
    if success:
        messages.success(request, success)
    return result


def _viewpoints(user):
    """距離の基準点: 現在地と活動拠点。"""
    points = [(user.latitude, user.longitude)] if user.has_location else []
    return points + [(b.latitude, b.longitude) for b in user.bases.all()]


# ---------- 依頼 ----------

def help_request_list(request):
    services.expire_requests()
    tab = 'scheduled' if request.GET.get('tab') == 'scheduled' else 'now'
    show_all = request.GET.get('all') == '1'
    qs = (HelpRequest.objects.filter(status__in=[HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS],
                                     expires_at__gt=timezone.now(),
                                     scheduled_for__isnull=(tab == 'now'))
          .annotate(joined_active=Count('matches', filter=Q(matches__status=Match.Status.IN_PROGRESS)))
          .filter(headcount__gt=F('joined_active'))
          .select_related('requester').prefetch_related('required_skills', 'preset_tags'))
    q = request.GET.get('q', '').strip()
    tag_id = request.GET.get('tag', '')
    skill_id = request.GET.get('skill', '')
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(area_label__icontains=q))
    if tag_id.isdigit():
        qs = qs.filter(preset_tags__id=int(tag_id))
    if skill_id.isdigit():
        qs = qs.filter(required_skills__id=int(skill_id))
    if tag_id.isdigit() or skill_id.isdigit():
        qs = qs.distinct()
    help_requests = list(qs.order_by('scheduled_for' if tab == 'scheduled' else 'expires_at'))
    for hr in help_requests:
        hr.remaining = hr.headcount - hr.joined_active

    user = request.user
    points = _viewpoints(user) if user.is_authenticated else []
    if points:
        for hr in help_requests:
            exact = min(services.distance_m(lat, lng, hr.latitude, hr.longitude) for lat, lng in points)
            public = min(services.distance_m(lat, lng, hr.public_latitude, hr.public_longitude) for lat, lng in points)
            hr.within_radius = exact <= services.RADIUS_METERS
            hr.distance_text = services.distance_label(public)
            hr.skill_ok = hr.requester_id != user.pk and services.skill_matches(user, hr)
            # 登録スキルと一致する(スキル指定のある)依頼は「おすすめ」として上に出す
            hr.recommended = hr.skill_ok and bool(hr.required_skills.all())
            hr.sort_key = (not hr.recommended, public)
        if not show_all:
            help_requests = [hr for hr in help_requests if hr.within_radius]
        help_requests.sort(key=lambda hr: hr.sort_key)
    recommended = [hr for hr in help_requests if getattr(hr, 'recommended', False)]
    others = [hr for hr in help_requests if not getattr(hr, 'recommended', False)]
    return render(request, 'core/help_request_list.html', {
        'help_requests': help_requests, 'recommended': recommended, 'others': others,
        'located': bool(points), 'show_all': show_all, 'tab': tab, 'radius': services.RADIUS_METERS,
        'photo_block_reason': services.photo_block_reason(user) if user.is_authenticated else '',
        'q': q, 'preset_tags': PresetTag.objects.all(), 'all_skills': Skill.objects.all(),
        'selected_tag': int(tag_id) if tag_id.isdigit() else '',
        'selected_skill': int(skill_id) if skill_id.isdigit() else '',
        'filtering': bool(q or tag_id or skill_id)})


@login_required
def help_request_create(request):
    reason = services.post_block_reason(request.user)
    if reason:
        messages.error(request, reason)
        return redirect('core:profile')
    initial = {'latitude': request.user.latitude, 'longitude': request.user.longitude}
    form = HelpRequestForm(request.POST or None, initial=initial, user=request.user)
    if request.method == 'POST' and form.is_valid():
        help_request = form.save()
        count = services.publish_request(help_request)
        messages.success(request, f'依頼を投稿しました。近くの{count}人に通知しました。')
        return redirect('core:help_request_detail', pk=help_request.pk)
    return render(request, 'core/help_request_form.html', {'form': form})


def help_request_detail(request, pk):
    services.expire_requests()
    help_request = get_object_or_404(
        HelpRequest.objects.select_related('requester').prefetch_related('required_skills', 'preset_tags'), pk=pk)
    user = request.user
    match = help_request.active_match or help_request.matches.filter(
        status=Match.Status.DONE).select_related('helper').first()
    is_requester = user.is_authenticated and user.pk == help_request.requester_id
    is_helper = user.is_authenticated and help_request.matches.filter(
        helper=user, status=Match.Status.IN_PROGRESS).exists()

    context = {
        'help_request': help_request, 'match': match,
        'is_requester': is_requester, 'is_helper': is_helper,
        'show_exact': services.can_view_exact_location(user, help_request),
        'cancel_form': CancelForm(),
        'participants': list(help_request.active_matches),
        'slots_left': help_request.slots_left,
        'accepting': help_request.accepting,
    }
    if user.is_authenticated and not is_requester:
        points = _viewpoints(user)
        if points:
            context['distance_text'] = services.distance_label(min(
                services.distance_m(lat, lng, help_request.public_latitude, help_request.public_longitude)
                for lat, lng in points))
        if help_request.accepting:
            context['help_block_reason'] = services.accept_block_reason(user)
            context['skill_ok'] = services.skill_matches(user, help_request)
            context['my_application'] = help_request.applications.filter(applicant=user).first()
            context['application_form'] = ApplicationForm()
    if is_requester and help_request.mode == HelpRequest.Mode.APPROVAL and help_request.status in (
            HelpRequest.Status.OPEN, HelpRequest.Status.IN_PROGRESS):
        context['applications'] = (help_request.applications.filter(status=Application.Status.APPLIED)
                                   .select_related('applicant'))
    if match is not None and (is_requester or is_helper):
        context['chat_open'] = services.chat_is_open(match)
        context['chat_history_visible'] = services.chat_history_visible(match)
        context['retention_deadline'] = services.chat_retention_deadline(match)
        if match.status == Match.Status.IN_PROGRESS:
            context['grace_seconds'] = services.grace_remaining_seconds(match)
        if (help_request.status == HelpRequest.Status.DONE
                and not Review.objects.filter(reviewer=user, help_request=help_request).exists()):
            context['review_target'] = match.other_user(user)
            context['review_form'] = ReviewForm()
    return render(request, 'core/help_request_detail.html', context)


@login_required
@require_POST
def accept_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    _run(request, lambda: services.accept_first_come(help_request, request.user),
         'お手伝いが決まりました。チャットで依頼者と連絡を取りましょう。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def apply_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    form = ApplicationForm(request.POST)
    if form.is_valid():
        _run(request, lambda: services.apply(help_request, request.user, form.cleaned_data['message']),
             '応募しました。依頼者の承認をお待ちください。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def withdraw_application(request, pk):
    application = get_object_or_404(Application, pk=pk, applicant=request.user)
    _run(request, lambda: services.withdraw(application, request.user), '応募を取り消しました。')
    return redirect('core:help_request_detail', pk=application.help_request_id)


@login_required
@require_POST
def approve_application(request, pk):
    application = get_object_or_404(Application.objects.select_related('help_request', 'applicant'), pk=pk)
    _run(request, lambda: services.approve(application, request.user),
         f'{application.applicant.username}さんにお願いしました。')
    return redirect('core:help_request_detail', pk=application.help_request_id)


@login_required
@require_POST
def complete_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    _run(request, lambda: services.complete(help_request, request.user),
         '完了しました。お相手のレビューをお願いします。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def cancel_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    form = CancelForm(request.POST)
    reason = form.cleaned_data['reason'] if form.is_valid() else ''
    try:
        sanction = services.cancel(help_request, request.user, reason)
    except ServiceError as e:
        messages.error(request, str(e))
    else:
        messages.info(request, 'キャンセルしました。')
        if sanction is not None:
            messages.warning(request, f'ペナルティ: {sanction.get_level_display()}({sanction.reason})')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def submit_review(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    form = ReviewForm(request.POST)
    if form.is_valid():
        _run(request, lambda: services.submit_review(help_request, request.user, form.cleaned_data['rating'],
                                                     form.cleaned_data['comment']), 'レビューを送信しました。')
    else:
        messages.error(request, '評価を選んでください。')
    return redirect('core:help_request_detail', pk=pk)


# ---------- チャット ----------

@login_required
def chat(request, pk):
    match = get_object_or_404(Match.objects.select_related('help_request__requester', 'helper'), pk=pk)
    if request.user.pk not in services.chat_participants(match.help_request):
        raise Http404
    form = MessageForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():  # JavaScriptが使えない環境向け
        _run(request, lambda: services.post_message(match, request.user, form.cleaned_data['body'],
                                                    form.cleaned_data.get('image')))
        return redirect('core:chat', pk=pk)

    help_request = match.help_request
    marked = services.mark_chat_read(help_request, request.user)
    if marked:  # ページを開いて既読にした場合も、送信者へリアルタイムに知らせる
        services.push(services.chat_group_name(help_request),
                      {'type': 'chat.read', 'reader_id': request.user.pk})
    request.user.notifications.filter(url=request.path, is_read=False).update(is_read=True)
    history_visible = services.chat_history_visible(match)
    chat_messages = []
    if history_visible:
        for m in help_request.messages.select_related('sender').prefetch_related('reads'):
            if m.is_hidden and m.sender_id != request.user.pk:
                continue
            m.read_count = sum(1 for r in m.reads.all() if r.user_id != m.sender_id)
            chat_messages.append(m)
    members = [help_request.requester] + [m.helper for m in
                                          help_request.matches.exclude(status=Match.Status.CANCELED)
                                          .select_related('helper').order_by('created_at')]
    return render(request, 'core/chat.html', {
        'match': match, 'help_request': help_request, 'other': match.other_user(request.user),
        'chat_messages': chat_messages, 'chat_open': services.chat_is_open(match), 'form': form,
        'history_visible': history_visible,
        'retention_deadline': services.chat_retention_deadline(match),
        'is_group': help_request.is_group, 'members': members,
    })


@login_required
def message_list(request):
    chats = services.open_chats(request.user)
    if not chats:
        messages.info(request, '現在はマッチングしていないため、メッセージ機能は使用できません。')
        return redirect('core:help_request_list')
    if len(chats) == 1:
        return redirect('core:chat', pk=chats[0].pk)
    for match in chats:
        match.other = match.other_user(request.user)
        match.last_message = match.help_request.messages.exclude(is_hidden=True).order_by('-created_at').first()
        match.unread_count = (match.help_request.messages.filter(is_hidden=False)
                              .exclude(sender=request.user).exclude(reads__user=request.user).count())
    return render(request, 'core/message_list.html', {'chats': chats})


# ---------- マイページ・アカウント ----------

@login_required
def profile(request):
    user = request.user
    next_level_points = user.level ** 2  # level = floor(sqrt(points)) + 1 の逆算
    prev_level_points = (user.level - 1) ** 2
    span = max(next_level_points - prev_level_points, 1)
    return render(request, 'core/profile.html', {
        'sanctions': Sanction.objects.filter(user=user).active().order_by('-starts_at'),
        'reviews': user.received_reviews.select_related('reviewer').order_by('-created_at')[:10],
        'avg_rating': user.received_reviews.aggregate(a=Avg('rating'))['a'],
        'my_requests': user.requests.order_by('-created_at')[:10],
        'my_helps': user.helps.select_related('help_request').order_by('-created_at')[:10],
        'help_count': user.helps.filter(status=Match.Status.DONE).count(),
        'request_count': user.requests.count(),
        'bases': user.bases.all(),
        'base_form': ActivityBaseForm(),
        'radius': services.RADIUS_METERS,
        'points_to_next': max(next_level_points - user.points, 0),
        'level_progress': 100 if user.level >= 100 else int((user.points - prev_level_points) * 100 / span),
    })


@login_required
def profile_edit(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        lat, lng = form.cleaned_data.get('latitude'), form.cleaned_data.get('longitude')
        if lat is not None and lng is not None and (
                (lat, lng) != (user.latitude, user.longitude) or user.location_updated_at is None):
            services.update_user_location(user, lat, lng)
        messages.success(request, 'プロフィールを更新しました。')
        return redirect('core:profile')
    return render(request, 'core/profile_edit.html', {'form': form})


@login_required
def account_settings(request):
    account_form = AccountForm(request.POST if 'save_account' in request.POST else None, instance=request.user)
    setting = services.notification_setting(request.user)
    notify_form = NotificationSettingForm(request.POST if 'save_notify' in request.POST else None,
                                          instance=setting, prefix='notify')
    for form in (account_form, notify_form):
        if form.is_bound and form.is_valid():
            form.save()
            messages.success(request, '設定を保存しました。')
            return redirect('core:account_settings')
    line_friend_url = (f'https://line.me/R/ti/p/{settings.LINE_OFFICIAL_ACCOUNT_ID}'
                       if settings.LINE_OFFICIAL_ACCOUNT_ID else '')
    return render(request, 'core/account_settings.html', {
        'account_form': account_form, 'notify_form': notify_form,
        'line_login_enabled': bool(settings.LINE_LOGIN_CHANNEL_ID),
        'line_linked': bool(request.user.line_user_id),
        'line_friend_url': line_friend_url})


@login_required
def notifications(request):
    items = list(request.user.notifications.all()[:50])
    for n in items:
        n.local_date = timezone.localtime(n.created_at).date()
    request.user.notifications.filter(is_read=False).update(is_read=True)
    today = timezone.localdate()
    return render(request, 'core/notifications.html', {
        'items': items, 'today': today, 'yesterday': today - timedelta(days=1)})


@login_required
@require_POST
def add_base(request):
    form = ActivityBaseForm(request.POST)
    if form.is_valid():
        base = form.save(commit=False)
        base.user = request.user
        base.area_label = area_label_for(base.latitude, base.longitude)
        base.save()
        messages.success(request, f'活動拠点「{base.label}」を登録しました。')
    else:
        messages.error(request, ' '.join(e for errors in form.errors.values() for e in errors))
    return redirect('core:profile')


@login_required
@require_POST
def delete_base(request, pk):
    get_object_or_404(request.user.bases, pk=pk).delete()
    messages.info(request, '活動拠点を削除しました。')
    return redirect('core:profile')


# ---------- 顔写真 ----------

def user_photo(request, pk):
    """顔写真。マッチング成立前の相手にはモザイク版を返す。審査前の写真は本人と運営にだけ見せる。"""
    target = get_object_or_404(User, pk=pk, is_active=True)
    if not target.avatar:
        raise Http404
    is_self_or_staff = request.user.is_authenticated and (request.user.pk == target.pk or request.user.is_staff)
    if not target.photo_approved and not is_self_or_staff:
        raise Http404
    name = target.avatar.name if services.photo_revealed(request.user, target) else images.ensure_mosaic(target.avatar.name)
    response = FileResponse(target.avatar.storage.open(name, 'rb'), content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=60'
    return response


# ---------- 公開プロフィール・通報 ----------

def public_profile(request, username):
    target = get_object_or_404(User, username=username, is_active=True)
    return render(request, 'core/public_profile.html', {
        'target': target,
        'skills': target.skills.all(),
        'avg_rating': target.received_reviews.aggregate(a=Avg('rating'))['a'],
        'review_count': target.received_reviews.count(),
        'help_count': target.helps.filter(status=Match.Status.DONE).count(),
        'reviews': target.received_reviews.select_related('reviewer').order_by('-created_at')[:5],
    })


@login_required
def report_user(request, username):
    target = get_object_or_404(User, username=username)
    help_request = HelpRequest.objects.filter(pk=request.GET.get('request') or None).first()
    message = Message.objects.filter(pk=request.GET.get('message') or None, sender=target).select_related(
        'match__help_request').first()
    if message is not None and request.user.pk not in message.match.participants():
        message = None
    form = ReportForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        done = _run(request, lambda: services.file_report(
            request.user, target, form.cleaned_data['category'], form.cleaned_data['detail'],
            help_request=help_request, message=message),
            '通報を受け付けました。運営が内容を確認します。')
        if done:
            return redirect('core:public_profile', username=username)
    return render(request, 'core/report_form.html', {
        'form': form, 'target': target, 'help_request': help_request, 'reported_message': message})


# ---------- 統計・API ----------

def stats(request):
    areas = services.top_areas()
    area = request.GET.get('area', '')
    counts = services.hourly_stats(area)
    peak = max(counts) or 1
    return render(request, 'core/stats.html', {
        'areas': areas, 'area': area, 'total': sum(counts),
        'hours': [{'hour': h, 'count': c, 'pct': int(c * 100 / peak)} for h, c in enumerate(counts)],
    })


@login_required
@login_required
def chat_image(request, pk):
    """チャットの写真。参加者(と運営)にだけ配信する。"""
    message = get_object_or_404(Message, pk=pk)
    if not message.image:
        raise Http404
    if not request.user.is_staff and request.user.pk not in message.match.participants():
        raise Http404
    response = FileResponse(message.image.storage.open(message.image.name, 'rb'), content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=300'
    return response


@login_required
def area_label_api(request):
    """座標 → 大まかな地名(現在地ボタンの入力欄表示用)。"""
    try:
        lat, lng = float(request.GET['lat']), float(request.GET['lng'])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({'label': ''})
    return JsonResponse({'label': area_label_for(lat, lng)})


def skill_search(request):
    results = skills.search(request.GET.get('q', ''))
    return JsonResponse({'results': [{'name': s.name, 'official': s.is_official, 'similar': s.is_similar}
                                     for s in results]})


# ---------- 運用 ----------

def healthz(request):
    """ホスティング(Render)の死活確認用。"""
    return HttpResponse('ok', content_type='text/plain')


@csrf_exempt
@require_POST
def run_tasks(request):
    """期限切れの締め切りとメールのまとめ送信。GitHub Actions などから合言葉つきで定期的に呼ぶ。"""
    token = request.headers.get('X-Tasks-Token', '')
    if not settings.TASKS_TOKEN or not constant_time_compare(token, settings.TASKS_TOKEN):
        return HttpResponseForbidden('forbidden')
    return JsonResponse({'expired': services.expire_requests(), 'mailed': services.send_email_digests(),
                         'purged_messages': services.purge_expired_chat_messages()})


def csrf_failure(request, reason=''):
    """CSRF検証に失敗したときに表示する案内画面(デフォルトの403画面の代わり)。

    メール認証コード送信直後の二重送信などで発生しうるため、認証が完了して
    いる可能性があることを伝えてログインへ誘導する。
    """
    return render(request, 'core/csrf_failure.html', status=403)


def too_many_requests(request, exception=None):
    """429(短時間にリクエストが多すぎる)のときに表示する案内画面。

    allauth のレート制限などが返すデフォルトの英語画面の代わりに使う。
    """
    return render(request, 'core/429.html', status=429)


def pwa_manifest(request):
    """PWAマニフェスト(ホーム画面に追加したときの名前・アイコンなど)。"""
    return JsonResponse({
        'name': 'suki-match',
        'short_name': 'suki-match',
        'description': 'スキマ時間で、近くの困りごとをお手伝いするマッチングサービス',
        'lang': 'ja',
        'id': '/',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'background_color': '#fbf8f3',
        'theme_color': '#4c8b6b',
        'icons': [
            {'src': static('core/icons/icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('core/icons/icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('core/icons/icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }, content_type='application/manifest+json')


def service_worker(request):
    """最小の Service Worker(ホーム画面への追加に必要)。ページは常にネットワークから取得する。"""
    js = ("self.addEventListener('install', function () { self.skipWaiting(); });\n"
          "self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });\n"
          "self.addEventListener('fetch', function (e) {\n"
          "    if (e.request.method !== 'GET') { return; }\n"
          "    e.respondWith(fetch(e.request));\n"
          "});\n")
    response = HttpResponse(js, content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response



# ---------- LINE Webhook ----------

@csrf_exempt
@require_POST
def line_webhook(request):
    """LINE 公式アカウントの Webhook。チャットでの操作は line_chat に委譲する。"""
    if not line_api.verify_signature(request.body, request.headers.get('X-Line-Signature', '')):
        return HttpResponseForbidden('invalid signature')
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return HttpResponseForbidden('invalid body')
    for event in payload.get('events', []):
        event_type = event.get('type')
        line_user_id = (event.get('source') or {}).get('userId', '')
        reply_token = event.get('replyToken', '')
        user = User.objects.filter(line_user_id=line_user_id).first() if line_user_id else None
        if event_type == 'unfollow' and line_user_id:
            User.objects.filter(line_user_id=line_user_id).update(line_user_id='')
        elif event_type == 'follow':
            line_api.send_reply(reply_token,
                                '友だち追加ありがとうございます！\n'
                                '下のメニューから、依頼したり・探したりできます。\n'
                                '通知を受け取るには、suki-match の「アカウント設定」から LINE 連携をしてください。\n'
                                f'{settings.SITE_URL}/me/settings/')
        elif event_type == 'message':
            message = event.get('message') or {}
            if message.get('type') == 'text':
                line_chat.handle_message(user, message.get('text', ''), reply_token)
            elif message.get('type') == 'location':
                line_chat.handle_location(user, message.get('latitude'), message.get('longitude'), reply_token)
        elif event_type == 'postback':
            line_chat.handle_postback(user, (event.get('postback') or {}).get('data', ''), reply_token)
    return HttpResponse('OK')


@staff_member_required
def line_richmenu_setup(request):
    """リッチメニューを LINE 側に登録する（スタッフ用・1回だけ実行すればよい）。"""
    result = None
    if request.method == 'POST':
        image_path = finders.find('core/line/richmenu.png')
        if not image_path:
            result = 'メニュー画像が見つかりませんでした。'
        elif not line_api.messaging_configured():
            result = 'LINE Messaging API が未設定のため実行できません。'
        else:
            with open(image_path, 'rb') as fh:
                result = line_api.setup_rich_menu(fh.read())
    return render(request, 'core/line_richmenu_setup.html', {'result': result})

