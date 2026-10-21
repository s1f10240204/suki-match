"""django-allauth のカスタマイズ。"""
from types import SimpleNamespace

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings


def site_info():
    """メールに載せるサービス名とドメイン(アクセス元のホスト名 127.0.0.1:8000 などを出さない)。"""
    return SimpleNamespace(name=settings.SITE_NAME, domain=settings.SITE_DOMAIN)


class AccountAdapter(DefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
        super().send_mail(template_prefix, email, {**context, 'current_site': site_info(),
                                                   'site_name': settings.SITE_NAME})
