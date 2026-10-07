from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import User


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """ログインユーザーごとの通知チャンネル。現在地の更新も受け付ける。"""

    async def connect(self):
        user = self.scope['user']
        if not user.is_authenticated:
            await self.close()
            return
        self.group = f'user_{user.pk}'
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get('type') != 'location':
            return
        try:
            lat, lng = float(content['lat']), float(content['lng'])
        except (KeyError, TypeError, ValueError):
            return
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return
        await self._save_location(lat, lng)

    @database_sync_to_async
    def _save_location(self, lat, lng):
        User.objects.filter(pk=self.scope['user'].pk).update(latitude=lat, longitude=lng)

    async def notify(self, event):
        await self.send_json({'message': event['message'], 'url': event.get('url', '')})
