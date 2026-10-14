from . import services


def app_context(request):
    """全画面共通: 「進行中の依頼」バーと未読通知数。"""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}
    active_matches, pending_requests = services.active_items(user)
    return {
        'active_matches': active_matches,
        'pending_requests': pending_requests,
        'unread_count': user.notifications.filter(is_read=False).count(),
    }
