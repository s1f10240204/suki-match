import time
from collections import deque

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import services
from .models import Match


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """ログインユーザーごとの通知チャンネル。現在地の更新も受け付ける。"""

    async def connect(self):
        user = self.scope['user']
        if not user.is_authenticated:
            await self.close()
            return
        self.group = f'user_{user.pk}'
        await self.channel_layer.group_add(self.group, self.channel_name)
        await database_sync_to_async(services.set_online)(user.pk, 1)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)
            await database_sync_to_async(services.set_online)(self.scope['user'].pk, -1)

    async def receive_json(self, content, **kwargs):
        if content.get('type') != 'location':
            return
        try:
            lat, lng = float(content['lat']), float(content['lng'])
        except (KeyError, TypeError, ValueError):
            return
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return
        await database_sync_to_async(services.update_user_location)(self.scope['user'], lat, lng)

    async def notify(self, event):
        await self.send_json({'message': event['message'], 'url': event.get('url', ''),
                              'kind': event.get('kind', '')})


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """マッチングした2人のチャット。"""
    RATE_LIMIT = 5          # RATE_WINDOW 秒あたりの送信数
    RATE_WINDOW = 10

    async def connect(self):
        user = self.scope['user']
        self.match_id = self.scope['url_route']['kwargs']['match_id']
        if not user.is_authenticated or not await self._can_join(user):
            await self.close()
            return
        self.group = f'chat_{self.match_id}'
        self.sent_at = deque(maxlen=self.RATE_LIMIT)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    @database_sync_to_async
    def _can_join(self, user):
        match = Match.objects.select_related('help_request').filter(pk=self.match_id).first()
        return match is not None and user.pk in match.participants()

    async def receive_json(self, content, **kwargs):
        now = time.monotonic()
        if len(self.sent_at) == self.RATE_LIMIT and now - self.sent_at[0] < self.RATE_WINDOW:
            await self.send_json({'error': '送信が速すぎます。少し待ってから送ってください。'})
            return
        self.sent_at.append(now)
        try:
            await self._post(content.get('body', ''))
        except services.ServiceError as e:
            await self.send_json({'error': str(e)})

    @database_sync_to_async
    def _post(self, body):
        match = Match.objects.select_related('help_request__requester', 'helper').get(pk=self.match_id)
        services.post_message(match, self.scope['user'], body)

    async def chat_message(self, event):
        await self.send_json({'message': event['message']})
