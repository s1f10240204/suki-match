from django import template
from django.utils import timezone
from django.utils.timesince import timesince
from django.urls import reverse

from .. import services

register = template.Library()


@register.inclusion_tag('core/_avatar.html', takes_context=True)
def avatar(context, user, size='md'):
    """顔写真。マッチング成立前の相手にはモザイク版(サーバー側で生成)を表示する。"""
    request = context.get('request')
    viewer = getattr(request, 'user', None)
    photo_url, mosaic = '', False
    if user.avatar and viewer is not None:
        is_self = viewer.is_authenticated and viewer.pk == user.pk
        if user.photo_approved or is_self or viewer.is_staff:
            # 判定結果は1リクエスト内でキャッシュする(一覧で同じ人が何度も出るため)
            cache = request.__dict__.setdefault('_photo_reveal_cache', {})
            if user.pk not in cache:
                cache[user.pk] = services.photo_revealed(viewer, user)
            mosaic = not cache[user.pk]
            photo_url = f"{reverse('core:user_photo', args=[user.pk])}?m={int(mosaic)}&v={user.avatar.name[-12:-4]}"
    return {'u': user, 'size': size, 'photo_url': photo_url, 'mosaic': mosaic,
            'pending': user.avatar and user.avatar_status != user.PhotoStatus.APPROVED}


@register.filter
def ago(value):
    """「たった今」「5分前」「3時間前」のような相対時刻。"""
    if (timezone.now() - value).total_seconds() < 60:
        return 'たった今'
    return timesince(value, depth=1) + '前'
