"""allauth のソーシャルログインと LINE ユーザー ID の同期。

LINE でログイン/連携したユーザーは、SocialAccount(provider='line') の uid が
そのまま LINE のユーザー ID になるので、通知送信用に User.line_user_id へ写す。
Messaging API と LINE ログインは同じプロバイダ配下のチャネルにすること
(プロバイダが違うとユーザー ID が一致しない)。
"""
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.signals import (pre_social_login, social_account_added,
                                       social_account_removed, social_account_updated)
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


def _sync_line_user_id(user):
    if user is None or not getattr(user, 'pk', None):
        return
    account = SocialAccount.objects.filter(user=user, provider='line').first()
    if account and user.line_user_id != account.uid:
        user.line_user_id = account.uid
        user.save(update_fields=['line_user_id'])


@receiver(pre_social_login)
def _on_pre_social_login(request, sociallogin, **kwargs):
    """Google ログイン時に、確認済みの @iniad.org メールを EmailAddress にも反映する。

    後付け連携(connect)では allauth が EmailAddress を作らないため、
    顔写真免除(photo_exempt)が効かなくなる。その救済として毎ログイン時に同期する。
    """
    try:
        if sociallogin.account.provider != 'google':
            return
        user = sociallogin.user
        if user is None or not getattr(user, 'pk', None):
            return  # 新規登録時は allauth 側で確認済みアドレスが保存される
        from allauth.account.models import EmailAddress
        for addr in sociallogin.email_addresses:
            if addr.verified and addr.email.lower().endswith('@iniad.org'):
                EmailAddress.objects.update_or_create(
                    user=user, email=addr.email.lower(),
                    defaults={'verified': True, 'primary': True})
    except Exception:
        pass  # 同期の失敗でログイン自体を止めない


@receiver(social_account_added)
def _on_social_added(request, sociallogin, **kwargs):
    _sync_line_user_id(sociallogin.user)
    try:
        from . import services
        services.grant_starter_points(sociallogin.user)
    except Exception:
        pass  # 付与の失敗でログイン自体を止めない


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
        services.grant_starter_points(user)  # ソーシャル登録で漏れた場合の救済(冪等)
    except Exception:
        pass
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
