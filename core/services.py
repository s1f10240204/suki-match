"""マッチングのドメインロジック(距離判定・スキル判定・通知・不正検知)。"""
from math import asin, cos, radians, sin, sqrt

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.urls import reverse

from .models import Match, User

RADIUS_METERS = 500
HELPER_POINTS = 10
REVIEW_BONUS_POINTS = 2

# 不正検知: 一定回数以上マッチした上でキャンセル率が閾値を超えたら受諾不可
ANOMALY_MIN_MATCHES = 5
ANOMALY_CANCEL_RATE = 0.5


def distance_m(lat1, lon1, lat2, lon2):
    """2地点間の距離(m)。ハーバーサイン公式。"""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def skill_matches(user, required_skill):
    if not required_skill:
        return True
    return required_skill.strip().lower() in [s.lower() for s in user.skill_list]


def nearby_helpers(help_request):
    """依頼の半径500m以内にいて、スキル条件を満たす依頼者以外のユーザー。"""
    candidates = User.objects.filter(latitude__isnull=False, longitude__isnull=False).exclude(
        pk=help_request.requester_id)
    return [
        u for u in candidates
        if distance_m(u.latitude, u.longitude, help_request.latitude, help_request.longitude) <= RADIUS_METERS
        and skill_matches(u, help_request.required_skill)
        and not is_suspicious(u)
    ]


def is_suspicious(user):
    """統計的に不自然な行動(高いキャンセル率)を検知する。"""
    matches = Match.objects.filter(helper=user)
    total = matches.count()
    if total < ANOMALY_MIN_MATCHES:
        return False
    return matches.filter(status=Match.CANCELED).count() / total > ANOMALY_CANCEL_RATE


def notify_user(user_id, message, url=''):
    """WebSocket経由でユーザーへ通知(接続していなければ何もしない)。"""
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f'user_{user_id}', {'type': 'notify', 'message': message, 'url': url})


def notify_nearby_helpers(help_request):
    url = reverse('core:help_request_detail', args=[help_request.pk])
    message = f'近くで「{help_request.title}」の依頼が出ました({help_request.duration_minutes}分)'
    helpers = nearby_helpers(help_request)
    for user in helpers:
        notify_user(user.pk, message, url)
    return len(helpers)
