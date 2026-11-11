from django.conf import settings
from django.db.models import Q

from . import services
from .models import Message


def app_context(request):
    """全画面共通: 地図の設定、「進行中の依頼」バー、未読通知数、メッセージできる相手の数。"""
    context = {
        'vapid_public_key': settings.VAPID_PUBLIC_KEY if settings.VAPID_PRIVATE_KEY else '',
        'GOOGLE_MAPS_API_KEY': settings.GOOGLE_MAPS_API_KEY,
        'GOOGLE_MAPS_MAP_ID': settings.GOOGLE_MAPS_MAP_ID,
        'EMAIL_CODE_RESEND_SECONDS': settings.EMAIL_CODE_RESEND_SECONDS,
    }
    session = getattr(request, 'session', None)
    if session is not None:
        context['impersonator_id'] = session.get('impersonator_id')
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return context
    active_matches, pending_requests = services.active_items(user)
    suppress_pk = getattr(request, 'suppress_active_request_pk', None)
    if suppress_pk:  # チャット画面では、その依頼自身のバーはヘッダーと重複するので除く
        active_matches = [m for m in active_matches if m.help_request_id != suppress_pk]
        pending_requests = [hr for hr in pending_requests if hr.pk != suppress_pk]
    context.update({
        'active_matches': active_matches,
        'pending_requests': pending_requests,
        'unread_count': user.notifications.filter(is_read=False).count(),
        'unread_message_count': (Message.objects.filter(is_hidden=False)
                                 .exclude(sender=user).exclude(reads__user=user)
                                 .filter(Q(help_request__requester=user) | Q(help_request__matches__helper=user))
                                 .distinct().count()),
        'open_chat_count': len(services.open_chats(user)),
        'tour_autoshow': (not user.tour_done
                          and getattr(request, 'resolver_match', None) is not None
                          and request.resolver_match.url_name == 'help_request_list'),
    })
    return context
