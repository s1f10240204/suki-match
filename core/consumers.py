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
    REACTION_LIMIT = 30     # リアクションの連打制限(同ウィンドウあたり)

    async def connect(self):
        user = self.scope['user']
        self.match_id = self.scope['url_route']['kwargs']['match_id']
        if not user.is_authenticated:
            await self.close()
            return
        info = await self._join_info()
        if info is None or user.pk not in info[1]:
            await self.close()
            return
        self.group = info[0]  # グループチャットは依頼単位で配信
        self.sent_at = deque(maxlen=self.RATE_LIMIT)
        self.reacted_at = deque(maxlen=self.REACTION_LIMIT)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self._mark_read()  # 開いた時点で相手のメッセージを既読にする

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    @database_sync_to_async
    def _join_info(self):
        match = Match.objects.select_related('help_request').filter(pk=self.match_id).first()
        if match is None:
            return None
        return services.chat_group_name(match.help_request), services.chat_participants(match.help_request)

    async def receive_json(self, content, **kwargs):
        if content.get('type') == 'reaction':
            now = time.monotonic()
            if len(self.reacted_at) == self.REACTION_LIMIT and now - self.reacted_at[0] < self.RATE_WINDOW:
                return  # 連打時は黙って捨てる
            self.reacted_at.append(now)
            await self._react(int(content.get('message_id') or 0), str(content.get('emoji') or ''))
            return
        if content.get('type') == 'read':
            await self._mark_read()
            return
        now = time.monotonic()
        if len(self.sent_at) == self.RATE_LIMIT and now - self.sent_at[0] < self.RATE_WINDOW:
            await self.send_json({'error': '送信が速すぎます。少し待ってから送ってください。'})
            return
        self.sent_at.append(now)
        try:
            await self._post(content.get('body', ''), content.get('reply_to'), content.get('stamp', ''))
        except services.ServiceError as e:
            await self.send_json({'error': str(e)})

    @database_sync_to_async
    def _post(self, body, reply_to_id=None, stamp=''):
        match = Match.objects.select_related('help_request__requester', 'helper').get(pk=self.match_id)
        services.post_message(match, self.scope['user'], body, reply_to_id=reply_to_id, stamp=stamp or '')

    async def chat_message(self, event):
        await self.send_json({'message': event['message']})

    @database_sync_to_async
    def _mark_read_db(self):
        match = Match.objects.filter(pk=self.match_id).select_related('help_request').first()
        if match is None:
            return 0
        return services.mark_chat_read(match.help_request, self.scope['user'])

    async def _mark_read(self):
        if await self._mark_read_db():
            await self.channel_layer.group_send(
                self.group, {'type': 'chat.read', 'reader_id': self.scope['user'].pk})

    async def chat_read(self, event):
        await self.send_json({'kind': 'read', 'reader_id': event['reader_id']})

    async def chat_reaction(self, event):
        await self.send_json({'kind': 'reaction_update', 'message_id': event['message_id'],
                              'reactions': event['reactions']})

    @database_sync_to_async
    def _react(self, message_id, emoji):
        from .models import Message
        message = Message.objects.select_related('help_request').filter(pk=message_id).first()
        if message is None:
            return
        try:
            services.toggle_reaction(message, self.scope['user'], emoji)
        except services.ServiceError:
            pass
