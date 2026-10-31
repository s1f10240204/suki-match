"""django-allauth のカスタマイズ。"""
from types import SimpleNamespace

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings


def site_info():
    """メールに載せるサービス名とドメイン(アクセス元のホスト名 127.0.0.1:8000 などを出さない)。"""
    return SimpleNamespace(name=settings.SITE_NAME, domain=settings.SITE_DOMAIN)


STARTER_POINTS = 100


def apply_referral(request, user):
    """招待リンク(?ref=ユーザー名)経由の登録なら、紹介者をユーザーに記録する。"""
    if request is None or user.referred_by_id:
        return
    ref = (request.session.get('ref_username') or '').strip()
    if not ref or ref == user.username:
        return
    from .models import User
    referrer = User.objects.filter(username=ref, is_active=True).first()
    if referrer is not None:
        user.referred_by = referrer


class AccountAdapter(DefaultAccountAdapter):
    def save_user(self, request, user, form, commit=True):
        is_new = not user.pk
        user = super().save_user(request, user, form, commit=False)
        if is_new:
            user.points = STARTER_POINTS  # 新規登録のスターターポイント(最初の依頼に使える)
            apply_referral(request, user)
        if commit:
            user.save()
            if is_new:
                from .models import PointTransaction
                PointTransaction.objects.create(
                    user=user, amount=STARTER_POINTS, balance_after=user.points,
                    kind=PointTransaction.Kind.STARTER, description='新規登録のスターターポイント')
        return user

    def send_mail(self, template_prefix, email, context):
        super().send_mail(template_prefix, email, {**context, 'current_site': site_info(),
                                                   'site_name': settings.SITE_NAME})
