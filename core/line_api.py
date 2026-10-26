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


# ---------- 複数メッセージ送信・Flex ----------

def reply_messages(reply_token, messages):
    """Webhook への返信。messages は LINE メッセージオブジェクトのリスト。"""
    if not (messaging_configured() and reply_token and messages):
        return False
    return _post(REPLY_URL, {'replyToken': reply_token, 'messages': messages[:5]})


def push_messages(line_user_id, messages):
    if not (messaging_configured() and line_user_id and messages):
        return False
    return _post(PUSH_URL, {'to': line_user_id, 'messages': messages[:5]})


def text_message(text, quick_reply_items=None):
    message = {'type': 'text', 'text': text[:5000]}
    if quick_reply_items:
        message['quickReply'] = {'items': [
            {'type': 'action', 'action': {'type': 'message', 'label': label[:20], 'text': value}}
            for label, value in quick_reply_items]}
    return message


# ---------- リッチメニュー ----------

RICHMENU_API = 'https://api.line.me/v2/bot/richmenu'
RICHMENU_DATA_API = 'https://api-data.line.me/v2/bot/richmenu'


def _api_request(url, method='GET', payload=None, content_type='application/json', raw=None):
    data = raw if raw is not None else (json.dumps(payload).encode('utf-8') if payload is not None else None)
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={'Authorization': f'Bearer {settings.LINE_MESSAGING_CHANNEL_ACCESS_TOKEN}',
                 'Content-Type': content_type})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode('utf-8')
            return json.loads(body) if body else {}
    except Exception:
        logger.exception('LINE API リクエストに失敗しました: %s', url)
        return None


def setup_rich_menu(image_bytes):
    """リッチメニューを作成し、画像を設定してデフォルトにする。古いメニューは削除する。"""
    from django.urls import reverse
    base = settings.SITE_URL
    area = lambda x, y, w, h, action: {'bounds': {'x': x, 'y': y, 'width': w, 'height': h}, 'action': action}
    msg = lambda label, text: {'type': 'message', 'label': label, 'text': text}
    uri = lambda label, url: {'type': 'uri', 'label': label, 'uri': url}
    W, H, COL, ROW = 2500, 1686, 833, 843
    menu = {
        'size': {'width': W, 'height': H},
        'selected': True,
        'name': 'suki-match メニュー',
        'chatBarText': 'メニュー',
        'areas': [
            area(0, 0, COL, ROW, msg('依頼する', '依頼する')),
            area(COL, 0, COL, ROW, msg('依頼を探す', '依頼を探す')),
            area(COL * 2, 0, W - COL * 2, ROW, msg('進行中', '進行中')),
            area(0, ROW, COL, H - ROW, uri('依頼フォーム', base + reverse('core:help_request_create'))),
            area(COL, ROW, COL, H - ROW, uri('アカウント設定', base + reverse('core:account_settings'))),
            area(COL * 2, ROW, W - COL * 2, H - ROW, uri('サイトを開く', base + '/')),
        ],
    }
    existing = _api_request(f'{RICHMENU_API}/list') or {}
    created = _api_request(RICHMENU_API, method='POST', payload=menu)
    if not created or 'richMenuId' not in created:
        return 'リッチメニューの作成に失敗しました'
    rich_menu_id = created['richMenuId']
    if _api_request(f'{RICHMENU_DATA_API}/{rich_menu_id}/content', method='POST',
                    raw=image_bytes, content_type='image/png') is None:
        return 'メニュー画像のアップロードに失敗しました'
    if _api_request(f'https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}', method='POST') is None:
        return 'リッチメニューのデフォルト設定に失敗しました'
    for old in existing.get('richmenus', []):
        if old.get('richMenuId') != rich_menu_id:
            _api_request(f'{RICHMENU_API}/{old["richMenuId"]}', method='DELETE')
    return f'リッチメニューを設定しました（ID: {rich_menu_id}）'
