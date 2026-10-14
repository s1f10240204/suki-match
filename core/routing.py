from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path('ws/notifications/', consumers.NotificationConsumer.as_asgi()),
    path('ws/chat/<int:match_id>/', consumers.ChatConsumer.as_asgi()),
]
