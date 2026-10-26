from django.conf import settings
from django.db.models import Q

from . import services
from .models import Message


def app_context(request):
    """全画面共通: 地図の設定、「進行中の依頼」バー、未読通知数、メッセージできる相手の数。"""
    context = {
        'GOOGLE_MAPS_API_KEY': settings.GOOGLE_MAPS_API_KEY,
        'GOOGLE_MAPS_MAP_ID': settings.GOOGLE_MAPS_MAP_ID,
        'EMAIL_CODE_RESEND_SECONDS': settings.EMAIL_CODE_RESEND_SECONDS,
    }
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return context
    active_matches, pending_requests = services.active_items(user)
    context.update({
        'active_matches': active_matches,
        'pending_requests': pending_requests,
        'unread_count': user.notifications.filter(is_read=False).count(),
        'unread_message_count': (Message.objects.filter(read_at__isnull=True, is_hidden=False)
                                 .exclude(sender=user)
                                 .filter(Q(match__helper=user) | Q(match__help_request__requester=user)).count()),
        'open_chat_count': len(services.open_chats(user)),
    })
    return context
