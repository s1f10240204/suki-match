"""allauth のソーシャルログインと LINE ユーザー ID の同期。

LINE でログイン/連携したユーザーは、SocialAccount(provider='line') の uid が
そのまま LINE のユーザー ID になるので、通知送信用に User.line_user_id へ写す。
Messaging API と LINE ログインは同じプロバイダ配下のチャネルにすること
(プロバイダが違うとユーザー ID が一致しない)。
"""
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.signals import social_account_added, social_account_removed, social_account_updated
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


def _sync_line_user_id(user):
    if user is None or not getattr(user, 'pk', None):
        return
    account = SocialAccount.objects.filter(user=user, provider='line').first()
    if account and user.line_user_id != account.uid:
        user.line_user_id = account.uid
        user.save(update_fields=['line_user_id'])


@receiver(social_account_added)
def _on_social_added(request, sociallogin, **kwargs):
    _sync_line_user_id(sociallogin.user)


@receiver(social_account_updated)
def _on_social_updated(request, sociallogin, **kwargs):
    _sync_line_user_id(sociallogin.user)


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    _sync_line_user_id(user)
    from . import services
    from .adapters import apply_referral
    # フォームなしのソーシャル登録など、アダプタを通らなかった紹介を取りこぼさない
    # (登録から7日以内の初回ログインだけを対象にする)
    if user.referred_by_id is None and request is not None:
        from datetime import timedelta
        from django.utils import timezone
        if timezone.now() - user.date_joined < timedelta(days=7):
            apply_referral(request, user)
            if user.referred_by_id is not None:
                user.save(update_fields=['referred_by'])
    try:
        services.grant_login_bonus(user)
    except Exception:
        pass  # ボーナス付与の失敗でログイン自体を止めない


@receiver(social_account_removed)
def _on_social_removed(request, socialaccount, **kwargs):
    user = socialaccount.user
    if socialaccount.provider == 'line' and user.line_user_id == socialaccount.uid:
        user.line_user_id = ''
        user.save(update_fields=['line_user_id'])
