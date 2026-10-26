"""LINE Messaging API 連携。

公式アカウントへのプッシュ通知と、Webhook の署名検証・返信を担当する。
チャネルアクセストークン等が未設定なら、すべての関数は何もしない。
"""
import base64
import hashlib
import hmac
import json
import logging
import urllib.request

from django.conf import settings

from .models import NotificationSetting

logger = logging.getLogger(__name__)

PUSH_URL = 'https://api.line.me/v2/bot/message/push'
REPLY_URL = 'https://api.line.me/v2/bot/message/reply'


def messaging_configured():
    return bool(settings.LINE_MESSAGING_CHANNEL_ACCESS_TOKEN)


def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LINE_MESSAGING_CHANNEL_ACCESS_TOKEN}',
        },
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except Exception:
        logger.exception('LINE API への送信に失敗しました')
        return False


def send_push(line_user_id, text):
    """指定した LINE ユーザーへテキストをプッシュ送信する。"""
    if not (messaging_configured() and line_user_id):
        return False
    return _post(PUSH_URL, {'to': line_user_id, 'messages': [{'type': 'text', 'text': text[:1000]}]})


def send_reply(reply_token, text):
    """Webhook イベントへの返信(無料・プッシュ枠の対象外)。"""
    if not (messaging_configured() and reply_token):
        return False
    return _post(REPLY_URL, {'replyToken': reply_token, 'messages': [{'type': 'text', 'text': text[:1000]}]})


def verify_signature(body, signature):
    """X-Line-Signature ヘッダの検証。チャネルシークレット未設定なら常に False。"""
    secret = settings.LINE_MESSAGING_CHANNEL_SECRET
    if not (secret and signature):
        return False
    digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(expected, signature)


def maybe_notify(user, kind, text, url=''):
    """services.notify から呼ばれる LINE 通知の振り分け。

    連携済みで、通知設定がオンのときだけ送る。失敗しても呼び出し元には影響させない。
    """
    try:
        if not (messaging_configured() and user.line_user_id):
            return
        setting = NotificationSetting.objects.filter(user=user).first()
        if kind == 'nearby_request':
            if setting is None or not setting.line_nearby:
                return
        else:
            if setting is not None and not setting.line_activity:
                return
        message = f'【suki-match】{text}'
        if url:
            message += f'\n{settings.SITE_URL}{url}'
        send_push(user.line_user_id, message)
    except Exception:
        logger.exception('LINE通知の振り分けに失敗しました')
