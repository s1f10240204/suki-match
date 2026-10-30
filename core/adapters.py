"""django-allauth のカスタマイズ。"""
from types import SimpleNamespace

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings


def site_info():
    """メールに載せるサービス名とドメイン(アクセス元のホスト名 127.0.0.1:8000 などを出さない)。"""
    return SimpleNamespace(name=settings.SITE_NAME, domain=settings.SITE_DOMAIN)


STARTER_POINTS = 100


class AccountAdapter(DefaultAccountAdapter):
    def save_user(self, request, user, form, commit=True):
        is_new = not user.pk
        user = super().save_user(request, user, form, commit=False)
        if is_new:
            user.points = STARTER_POINTS  # 新規登録のスターターポイント(最初の依頼に使える)
        if commit:
            user.save()
        return user

    def send_mail(self, template_prefix, email, context):
        super().send_mail(template_prefix, email, {**context, 'current_site': site_info(),
                                                   'site_name': settings.SITE_NAME})
