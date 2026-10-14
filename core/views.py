from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services, skills
from .forms import (AccountForm, ActivityBaseForm, ApplicationForm, CancelForm, HelpRequestForm, MessageForm,
                    NotificationSettingForm, ProfileForm, ReportForm, ReviewForm)
from .geocoding import area_label_for
from .models import Application, HelpRequest, Match, Message, Review, Sanction, User
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
    qs = (HelpRequest.objects.filter(status=HelpRequest.Status.OPEN, expires_at__gt=timezone.now(),
                                     scheduled_for__isnull=(tab == 'now'))
          .select_related('requester').prefetch_related('required_skills', 'preset_tags'))
    help_requests = list(qs.order_by('scheduled_for' if tab == 'scheduled' else 'expires_at'))

    user = request.user
    points = _viewpoints(user) if user.is_authenticated else []
    if points:
        for hr in help_requests:
            exact = min(services.distance_m(lat, lng, hr.latitude, hr.longitude) for lat, lng in points)
            public = min(services.distance_m(lat, lng, hr.public_latitude, hr.public_longitude) for lat, lng in points)
            hr.within_radius = exact <= services.RADIUS_METERS
            hr.distance_text = services.distance_label(public)
            hr.skill_ok = hr.requester_id != user.pk and services.skill_matches(user, hr)
            hr.sort_key = public
        if not show_all:
            help_requests = [hr for hr in help_requests if hr.within_radius]
        help_requests.sort(key=lambda hr: hr.sort_key)
    return render(request, 'core/help_request_list.html', {
        'help_requests': help_requests, 'located': bool(points), 'show_all': show_all,
        'tab': tab, 'radius': services.RADIUS_METERS})


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
    is_helper = user.is_authenticated and match is not None and user.pk == match.helper_id

    context = {
        'help_request': help_request, 'match': match,
        'is_requester': is_requester, 'is_helper': is_helper,
        'show_exact': services.can_view_exact_location(user, help_request),
        'cancel_form': CancelForm(),
    }
    if user.is_authenticated and not is_requester:
        points = _viewpoints(user)
        if points:
            context['distance_text'] = services.distance_label(min(
                services.distance_m(lat, lng, help_request.public_latitude, help_request.public_longitude)
                for lat, lng in points))
        if help_request.is_open:
            context['help_block_reason'] = services.accept_block_reason(user)
            context['skill_ok'] = services.skill_matches(user, help_request)
            context['my_application'] = help_request.applications.filter(applicant=user).first()
            context['application_form'] = ApplicationForm()
    if is_requester and help_request.is_open and help_request.mode == HelpRequest.Mode.APPROVAL:
        context['applications'] = (help_request.applications.filter(status=Application.Status.APPLIED)
                                   .select_related('applicant'))
    if match is not None and (is_requester or is_helper):
        context['chat_open'] = services.chat_is_open(match)
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
    if request.user.pk not in match.participants():
        raise Http404
    form = MessageForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():  # JavaScriptが使えない環境向け
        _run(request, lambda: services.post_message(match, request.user, form.cleaned_data['body']))
        return redirect('core:chat', pk=pk)

    match.messages.filter(read_at__isnull=True).exclude(sender=request.user).update(read_at=timezone.now())
    request.user.notifications.filter(url=request.path, is_read=False).update(is_read=True)
    chat_messages = [m for m in match.messages.select_related('sender')
                     if not m.is_hidden or m.sender_id == request.user.pk]
    return render(request, 'core/chat.html', {
        'match': match, 'help_request': match.help_request, 'other': match.other_user(request.user),
        'chat_messages': chat_messages, 'chat_open': services.chat_is_open(match), 'form': form,
    })


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
    return render(request, 'core/account_settings.html', {
        'account_form': account_form, 'notify_form': notify_form})


@login_required
def notifications(request):
    items = list(request.user.notifications.all()[:50])
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'core/notifications.html', {'items': items})


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
def skill_search(request):
    results = skills.search(request.GET.get('q', ''))
    return JsonResponse({'results': [{'name': s.name, 'official': s.is_official} for s in results]})
