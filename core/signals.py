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


@receiver(social_account_removed)
def _on_social_removed(request, socialaccount, **kwargs):
    user = socialaccount.user
    if socialaccount.provider == 'line' and user.line_user_id == socialaccount.uid:
        user.line_user_id = ''
        user.save(update_fields=['line_user_id'])
