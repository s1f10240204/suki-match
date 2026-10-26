from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from . import services, skills
from .models import (ActivityBase, Application, HelpRequest, Match, Message, Notification, NotificationSetting,
                     PresetTag, Report, Review, Sanction, Skill, User)


PHOTO_REJECT_REASON = '顔がはっきり写った本人の写真を登録してください'


class PasswordConfirmMixin:
    """間違って押すと危ないアクションに、パスワード再入力つきの確認画面を挟む共通処理。"""

    def password_confirmed_action(self, request, queryset, *, action_name, title, warning, execute):
        if request.POST.get('confirm_execute'):
            if not request.user.check_password(request.POST.get('confirm_password', '')):
                self.message_user(request, 'パスワードが違います。実行していません。', messages.ERROR)
            else:
                execute()
            return HttpResponseRedirect(request.path)
        opts = self.model._meta
        context = {
            **self.admin_site.each_context(request),
            'title': title,
            'warning': warning,
            'objects': list(queryset),
            'action_name': action_name,
            'changelist_url': reverse(f'admin:{opts.app_label}_{opts.model_name}_changelist'),
        }
        return TemplateResponse(request, 'admin/confirm_with_password.html', context)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('username', 'photo_thumb', 'avatar_status', 'email', 'area_label', 'level', 'is_active')
    list_filter = ('avatar_status',) + BaseUserAdmin.list_filter
    readonly_fields = ('photo_preview',)
    fieldsets = BaseUserAdmin.fieldsets + (
        ('顔写真の審査', {'fields': ('photo_preview', 'avatar_status', 'avatar_reject_reason', 'avatar_reviewed_at')}),
        ('suki-match', {'fields': ('motivation', 'bio', 'skills', 'area_label',
                                   'latitude', 'longitude', 'location_updated_at', 'points', 'level')}),
    )
    filter_horizontal = BaseUserAdmin.filter_horizontal + ('skills',)
    actions = ['approve_photos', 'reject_photos']

    def _img(self, obj, size):
        if not obj.avatar:
            return '-'
        return format_html('<img src="{}?m=0" style="width:{}px;height:{}px;object-fit:cover;border-radius:8px">',
                           reverse('core:user_photo', args=[obj.pk]), size, size)

    @admin.display(description='写真')
    def photo_thumb(self, obj):
        return self._img(obj, 40)

    @admin.display(description='写真')
    def photo_preview(self, obj):
        return self._img(obj, 240)

    @admin.action(description='選択したユーザーの顔写真を承認する')
    def approve_photos(self, request, queryset):
        users = list(queryset.exclude(avatar=''))
        for user in users:
            services.review_photo(user, approve=True)
        self.message_user(request, f'{len(users)}人の顔写真を承認しました。')

    @admin.action(description='選択したユーザーの顔写真を差し戻す(再提出を依頼)')
    def reject_photos(self, request, queryset):
        users = list(queryset.exclude(avatar=''))
        for user in users:
            services.review_photo(user, approve=False, reason=PHOTO_REJECT_REASON)
        self.message_user(request, f'{len(users)}人の顔写真を差し戻しました。')


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_official', 'canonical', 'created_by', 'created_at')
    list_filter = ('is_official', ('canonical', admin.EmptyFieldListFilter))
    search_fields = ('name', 'normalized')
    actions = ['merge_into_first']

    @admin.action(description='選択したタグを、最初に作られたタグへ統合する')
    def merge_into_first(self, request, queryset):
        selected = list(queryset.order_by('-is_official', 'created_at'))
        if len(selected) < 2:
            self.message_user(request, '2つ以上選んでください。', messages.WARNING)
            return
        skills.merge(selected[0], selected[1:])
        self.message_user(request, f'{len(selected) - 1}件を「{selected[0].resolved.name}」に統合しました。')


@admin.register(HelpRequest)
class HelpRequestAdmin(admin.ModelAdmin):
    list_display = ('title', 'requester', 'mode', 'status', 'area_label', 'expires_at', 'created_at')
    list_filter = ('status', 'mode')
    search_fields = ('title', 'description', 'requester__username')


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('help_request', 'helper', 'status', 'penalized', 'created_at', 'canceled_at')
    list_filter = ('status', 'penalized')


@admin.register(Sanction)
class SanctionAdmin(PasswordConfirmMixin, admin.ModelAdmin):
    list_display = ('user', 'level', 'reason', 'source', 'starts_at', 'ends_at')
    list_filter = ('level', 'source')
    search_fields = ('user__username', 'reason')
    actions = ['end_now']

    @admin.action(description='選択したペナルティを今すぐ解除する')
    def end_now(self, request, queryset):
        return self.password_confirmed_action(
            request, queryset, action_name='end_now',
            title='ペナルティを今すぐ解除しますか？',
            warning='選択したペナルティが直ちに解除され、対象ユーザーが再び活動できるようになります。',
            execute=lambda: queryset.update(ends_at=timezone.now()))


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('sender', 'short_body', 'risk_score', 'risk_labels', 'is_hidden', 'created_at')
    list_filter = ('is_hidden',)
    ordering = ('-risk_score', '-created_at')
    actions = ['hide', 'unhide']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.GET.get('review') == '1':  # 要確認: /admin/core/message/?review=1
            qs = qs.filter(risk_score__gte=settings.MODERATION_REVIEW_THRESHOLD)
        return qs

    @admin.display(description='本文')
    def short_body(self, obj):
        return obj.body[:40]

    @admin.action(description='非表示にする')
    def hide(self, request, queryset):
        queryset.update(is_hidden=True)

    @admin.action(description='表示に戻す')
    def unhide(self, request, queryset):
        queryset.update(is_hidden=False)


@admin.register(Report)
class ReportAdmin(PasswordConfirmMixin, admin.ModelAdmin):
    list_display = ('target_user', 'category', 'reporter', 'status', 'created_at')
    list_filter = ('status', 'category')
    list_editable = ('status',)
    actions = ['suspend_7_days', 'ban']

    def _sanction(self, request, queryset, level, days):
        for report in queryset.select_related('target_user'):
            Sanction.objects.create(
                user=report.target_user, level=level, source=Sanction.Source.ADMIN,
                reason=f'通報対応({report.get_category_display()})',
                ends_at=timezone.now() + timedelta(days=days) if days else None)
            report.status, report.handled_by = Report.Status.RESOLVED, request.user
            report.save(update_fields=['status', 'handled_by'])

    @admin.action(description='対象ユーザーを7日間「依頼・受付ともに停止」にする')
    def suspend_7_days(self, request, queryset):
        return self.password_confirmed_action(
            request, queryset, action_name='suspend_7_days',
            title='対象ユーザーを7日間停止しますか？',
            warning='選択した通報の対象ユーザーが、7日間「依頼・受付ともに停止」になります。通報は「対応済み」になります。',
            execute=lambda: self._sanction(request, queryset, Sanction.Level.NO_ACTIVITY, 7))

    @admin.action(description='対象ユーザーを利用停止(BAN)にする')
    def ban(self, request, queryset):
        return self.password_confirmed_action(
            request, queryset, action_name='ban',
            title='対象ユーザーを利用停止(BAN)にしますか？',
            warning='選択した通報の対象ユーザーが利用停止(BAN)になります。通報は「対応済み」になります。',
            execute=lambda: self._sanction(request, queryset, Sanction.Level.BANNED, None))


admin.site.register(Review)
admin.site.register(Application)
admin.site.register(PresetTag)
admin.site.register(Notification)
admin.site.register(NotificationSetting)
admin.site.register(ActivityBase)


# ---------- 管理画面の表記 ----------
admin.site.site_header = 'suki-match 管理'
admin.site.site_title = 'suki-match 管理'
admin.site.index_title = '管理メニュー（詳しい操作はここから）'
