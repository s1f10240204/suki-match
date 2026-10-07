from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import HelpRequestForm, ProfileForm, ReviewForm, SignUpForm
from .models import HelpRequest, Match, Review
from .services import (HELPER_POINTS, RADIUS_METERS, REVIEW_BONUS_POINTS, distance_m,
                       is_suspicious, notify_nearby_helpers, notify_user, skill_matches)


def signup(request):
    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.save())
        messages.success(request, '登録しました。現在地とスキルをプロフィールで設定すると近くの依頼が届きます。')
        return redirect('core:profile')
    return render(request, 'registration/signup.html', {'form': form})


def help_request_list(request):
    help_requests = list(HelpRequest.objects.filter(status=HelpRequest.OPEN)
                         .select_related('requester').order_by('-created_at'))
    user = request.user
    located = user.is_authenticated and user.has_location
    nearby_only = located and request.GET.get('all') != '1'
    if located:
        for hr in help_requests:
            hr.distance = round(distance_m(user.latitude, user.longitude, hr.latitude, hr.longitude))
            hr.skill_ok = skill_matches(user, hr.required_skill)
        if nearby_only:
            help_requests = [hr for hr in help_requests if hr.distance <= RADIUS_METERS]
            help_requests.sort(key=lambda hr: hr.distance)
    return render(request, 'core/help_request_list.html', {
        'help_requests': help_requests, 'located': located,
        'nearby_only': nearby_only, 'radius': RADIUS_METERS})


@login_required
def help_request_create(request):
    initial = {}
    if request.user.has_location:
        initial = {'latitude': request.user.latitude, 'longitude': request.user.longitude}
    form = HelpRequestForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        help_request = form.save(commit=False)
        help_request.requester = request.user
        help_request.save()
        count = notify_nearby_helpers(help_request)
        messages.success(request, f'依頼を投稿しました。近くの{count}人に通知しました。')
        return redirect('core:help_request_detail', pk=help_request.pk)
    return render(request, 'core/help_request_form.html', {'form': form})


def help_request_detail(request, pk):
    help_request = get_object_or_404(HelpRequest.objects.select_related('requester'), pk=pk)
    user = request.user
    match = help_request.matches.filter(status=Match.IN_PROGRESS).select_related('helper').first()
    if match is None:
        match = help_request.matches.filter(status=Match.DONE).select_related('helper').first()
    is_requester = user.is_authenticated and user == help_request.requester
    is_helper = user.is_authenticated and match is not None and user == match.helper

    distance = None
    if user.is_authenticated and user.has_location:
        distance = round(distance_m(user.latitude, user.longitude, help_request.latitude, help_request.longitude))

    can_accept = (user.is_authenticated and not is_requester
                  and help_request.status == HelpRequest.OPEN)
    blocked_reason = ''
    if can_accept:
        if is_suspicious(user):
            can_accept, blocked_reason = False, 'キャンセル率が高いため、現在お手伝いを受けられません。'
        elif not skill_matches(user, help_request.required_skill):
            blocked_reason = f'この依頼は「{help_request.required_skill}」が得意な方向けです(受諾は可能です)。'

    review_target = None
    if match and match.status == Match.DONE and (is_requester or is_helper):
        already = Review.objects.filter(reviewer=user, help_request=help_request).exists()
        if not already:
            review_target = match.helper if is_requester else help_request.requester

    return render(request, 'core/help_request_detail.html', {
        'help_request': help_request, 'match': match,
        'is_requester': is_requester, 'is_helper': is_helper,
        'can_accept': can_accept, 'blocked_reason': blocked_reason,
        'distance': distance, 'review_target': review_target,
        'review_form': ReviewForm() if review_target else None,
    })


@login_required
@require_POST
def accept_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    if help_request.requester == request.user:
        messages.error(request, '自分の依頼は受けられません。')
    elif is_suspicious(request.user):
        messages.error(request, 'キャンセル率が高いため、現在お手伝いを受けられません。')
    else:
        with transaction.atomic():
            # 先着1名: 募集中のものだけを原子的に進行中へ更新する
            claimed = HelpRequest.objects.filter(pk=pk, status=HelpRequest.OPEN).update(
                status=HelpRequest.IN_PROGRESS)
            if claimed:
                Match.objects.create(help_request=help_request, helper=request.user)
        if claimed:
            messages.success(request, 'お手伝いが決まりました。')
            notify_user(help_request.requester_id,
                        f'{request.user.username}さんが「{help_request.title}」を引き受けました',
                        reverse('core:help_request_detail', args=[pk]))
        else:
            messages.error(request, 'この依頼はすでに他の方が引き受けました。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def complete_help_request(request, pk):
    """依頼者が完了を確定。ヘルパーにポイントを付与する。"""
    help_request = get_object_or_404(HelpRequest, pk=pk, requester=request.user)
    match = help_request.matches.filter(status=Match.IN_PROGRESS).first()
    if match is None:
        messages.error(request, '進行中のマッチングがありません。')
        return redirect('core:help_request_detail', pk=pk)
    with transaction.atomic():
        match.status = Match.DONE
        match.save(update_fields=['status'])
        help_request.status = HelpRequest.DONE
        help_request.save(update_fields=['status'])
        match.helper.add_points(HELPER_POINTS)
    notify_user(match.helper_id, f'「{help_request.title}」が完了しました(+{HELPER_POINTS}pt)',
                reverse('core:help_request_detail', args=[pk]))
    messages.success(request, '完了しました。ヘルパーのレビューをお願いします。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def cancel_help_request(request, pk):
    """ヘルパーのキャンセル=再募集 / 依頼者のキャンセル=依頼取り下げ。"""
    help_request = get_object_or_404(HelpRequest, pk=pk)
    match = help_request.matches.filter(status=Match.IN_PROGRESS).first()
    link = reverse('core:help_request_detail', args=[pk])
    if match and match.helper == request.user:
        match.status = Match.CANCELED
        match.save(update_fields=['status'])
        help_request.status = HelpRequest.OPEN
        help_request.save(update_fields=['status'])
        notify_user(help_request.requester_id, f'「{help_request.title}」のヘルパーがキャンセルしました。再募集します', link)
        notify_nearby_helpers(help_request)
        messages.info(request, 'キャンセルしました。依頼は再募集されます。')
    elif help_request.requester == request.user and help_request.status in (
            HelpRequest.OPEN, HelpRequest.IN_PROGRESS):
        if match:
            match.status = Match.CANCELED
            match.save(update_fields=['status'])
            notify_user(match.helper_id, f'「{help_request.title}」は依頼者により取り下げられました', link)
        help_request.status = HelpRequest.CANCELED
        help_request.save(update_fields=['status'])
        messages.info(request, '依頼を取り下げました。')
    else:
        messages.error(request, 'キャンセルできません。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
@require_POST
def submit_review(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk, status=HelpRequest.DONE)
    match = help_request.matches.filter(status=Match.DONE).select_related('helper').first()
    if match is None or request.user not in (help_request.requester, match.helper):
        messages.error(request, 'レビューできません。')
        return redirect('core:help_request_detail', pk=pk)
    reviewee = match.helper if request.user == help_request.requester else help_request.requester
    form = ReviewForm(request.POST)
    if Review.objects.filter(reviewer=request.user, help_request=help_request).exists():
        messages.error(request, 'すでにレビュー済みです。')
    elif form.is_valid():
        review = form.save(commit=False)
        review.reviewer, review.reviewee, review.help_request = request.user, reviewee, help_request
        review.save()
        if review.rating >= 4:
            reviewee.add_points(REVIEW_BONUS_POINTS)
        messages.success(request, 'レビューを送信しました。')
    else:
        messages.error(request, '評価は1〜5で入力してください。')
    return redirect('core:help_request_detail', pk=pk)


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'プロフィールを更新しました。')
        return redirect('core:profile')
    reviews = request.user.received_reviews.select_related('reviewer').order_by('-created_at')[:10]
    return render(request, 'core/profile.html', {
        'form': form, 'reviews': reviews,
        'avg_rating': request.user.received_reviews.aggregate(a=Avg('rating'))['a'],
        'my_requests': request.user.requests.order_by('-created_at')[:10],
        'my_helps': request.user.helps.select_related('help_request').order_by('-created_at')[:10],
    })
