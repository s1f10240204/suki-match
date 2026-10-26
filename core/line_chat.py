"""LINEチャットでの依頼投稿・検索・応募・エリア設定の会話処理。

リッチメニューやテキストコマンドから起動し、投稿は会話形式で入力する。
実際の投稿・応募は Web と同じ services の関数を通すので、利用条件も同じになる。
エリアは位置情報メッセージ、または地名・駅名の検索から選べる。
"""
from urllib.parse import parse_qs

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from . import geocoding, line_api, services
from .models import HelpRequest, LineChatState

CANCEL_WORDS = {'やめる', 'キャンセル', '中止', 'やっぱりやめる'}
COMMANDS = {'依頼する', '依頼を探す', '進行中', 'エリア設定'}
DURATIONS = {'15分': 15, '30分': 30, '1時間': 60, '2時間': 120}
GREEN = '#4c8b6b'
AREA_HOW_ITEMS = [('位置情報を送る', '位置情報を送る'), ('エリア名で指定', 'エリア名で指定')]


def _abs(path):
    return f'{settings.SITE_URL}{path}'


def _reply(reply_token, messages):
    if isinstance(messages, dict):
        messages = [messages]
    line_api.reply_messages(reply_token, messages)


def _state(user):
    state, _created = LineChatState.objects.get_or_create(user=user)
    return state


def _clear(state):
    state.step = ''
    state.data = {}
    state.save(update_fields=['step', 'data'])


def _save(state, step=None, **data):
    if step is not None:
        state.step = step
    state.data = {**state.data, **data}
    state.save(update_fields=['step', 'data'])


def _unlinked(reply_token):
    _reply(reply_token, line_api.text_message(
        'suki-match のアカウントと連携すると、LINEから依頼したり受けたりできます。\n'
        f'連携はこちら: {_abs(reverse("core:account_settings"))}'))


# ---------- メッセージ入口 ----------

def handle_message(user, text, reply_token):
    text = (text or '').strip()
    if user is None:
        _unlinked(reply_token)
        return
    state = _state(user)
    if text in CANCEL_WORDS:
        _clear(state)
        _reply(reply_token, line_api.text_message('やめました。またいつでもどうぞ。'))
        return
    if text in COMMANDS:
        _clear(state)
        if text == '依頼する':
            _start_post(user, state, reply_token)
        elif text == '依頼を探す':
            _browse(user, reply_token)
        elif text == 'エリア設定':
            _start_area_setting(user, state, reply_token)
        else:
            _status(user, reply_token)
        return
    if state.step:
        _step_input(user, state, text, reply_token)
        return
    _reply(reply_token, line_api.text_message(
        '下のメニューから選んでください。\n'
        '・「依頼する」: 会話で依頼を投稿\n'
        '・「依頼を探す」: 近くの募集中の依頼を表示\n'
        '・「進行中」: マッチング中や応募の状況を確認\n'
        '・「エリア設定」: 登録エリアの変更'))


def handle_location(user, latitude, longitude, reply_token):
    """位置情報メッセージ。エリア入力待ちのときだけ受け付ける。"""
    if user is None:
        _unlinked(reply_token)
        return
    state = _state(user)
    if state.step != 'area_location' or latitude is None or longitude is None:
        _reply(reply_token, line_api.text_message(
            '位置情報ありがとうございます。エリアを変更したいときは「エリア設定」と送ってください。'))
        return
    _apply_area(user, state, float(latitude), float(longitude), reply_token)


def handle_postback(user, data, reply_token):
    params = {key: values[0] for key, values in parse_qs(data or '').items()}
    action = params.get('action', '')
    if user is None:
        _reply(reply_token, line_api.text_message(
            f'先に suki-match との連携が必要です: {_abs(reverse("core:account_settings"))}'))
        return
    state = _state(user)
    if action == 'cancel':
        _clear(state)
        _reply(reply_token, line_api.text_message('やめました。'))
    elif action == 'post_confirm':
        _post_confirm(user, state, reply_token)
    elif action == 'accept':
        _accept(user, params.get('req', ''), reply_token)


# ---------- エリア設定（投稿フローと単独設定で共用） ----------

def _start_area_setting(user, state, reply_token):
    _save(state, 'area_how', area_for='profile')
    current = f'現在の登録エリア: {user.area_label}\n' if user.area_label else '登録エリアは未設定です。\n'
    _reply(reply_token, line_api.text_message(
        current + 'エリアの設定方法を選んでください。',
        quick_reply_items=AREA_HOW_ITEMS))


def _ask_area_how(user, state, reply_token):
    """投稿フローのエリア選択。登録済みエリアがあれば「そのまま使う」も選べる。"""
    _save(state, 'post_area', area_for='post')
    items = []
    if user.latitude is not None and user.longitude is not None:
        items.append(('登録エリアを使う', '登録エリアを使う'))
    items += AREA_HOW_ITEMS
    current = f'登録エリア: {user.area_label or "未設定"}\n'
    _reply(reply_token, line_api.text_message(
        current + '依頼のエリアを選んでください。新しい場所を選ぶと、登録エリアもその場所に更新されます。',
        quick_reply_items=items))


def _area_how_input(user, state, text, reply_token):
    """area_how / post_area の選択肢入力。"""
    if text == '登録エリアを使う' and state.data.get('area_for') == 'post':
        if user.latitude is None or user.longitude is None:
            _reply(reply_token, line_api.text_message('登録エリアが未設定です。下の方法から選んでください。',
                                                      quick_reply_items=AREA_HOW_ITEMS))
            return
        _save(state, 'post_confirm', latitude=user.latitude, longitude=user.longitude)
        _reply(reply_token, _confirm_message(user, state.data))
        return
    if text == '位置情報を送る':
        _save(state, 'area_location')
        _reply(reply_token, line_api.text_message(
            'トーク画面の「＋」ボタン→「位置情報」から、場所を送ってください。'))
        return
    if text == 'エリア名で指定':
        _save(state, 'area_text')
        _reply(reply_token, line_api.text_message(
            'エリア名や駅名を送ってください（例: 東京都北区赤羽、池袋駅）。'))
        return
    _reply(reply_token, line_api.text_message('下のボタンから選んでください。'))


def _area_text_input(user, state, text, reply_token):
    candidates = geocoding.search_area(text)
    if not candidates:
        _reply(reply_token, line_api.text_message(
            '見つかりませんでした。都道府県名から入れるなど、別の書き方で送ってみてください。'))
        return
    if user.latitude is not None and user.longitude is not None:
        candidates.sort(key=lambda c: services.distance_m(user.latitude, user.longitude, c['lat'], c['lng']))
    candidates = candidates[:3]
    _save(state, 'area_pick', candidates=candidates)
    lines = ['候補が見つかりました。番号で選んでください。']
    for i, cand in enumerate(candidates, 1):
        lines.append(f'{i}. {cand["name"]}')
    _reply(reply_token, line_api.text_message(
        '\n'.join(lines), quick_reply_items=[(f'{i}番', str(i)) for i in range(1, len(candidates) + 1)]))


def _area_pick_input(user, state, text, reply_token):
    candidates = state.data.get('candidates') or []
    try:
        cand = candidates[int(text) - 1]
    except (ValueError, IndexError):
        _reply(reply_token, line_api.text_message('下のボタンから番号を選んでください。'))
        return
    _apply_area(user, state, cand['lat'], cand['lng'], reply_token)


def _apply_area(user, state, lat, lng, reply_token):
    services.update_user_location(user, lat, lng)
    if state.data.get('area_for') == 'post':
        _save(state, 'post_confirm', latitude=lat, longitude=lng)
        _reply(reply_token, _confirm_message(user, state.data))
    else:
        _clear(state)
        _reply(reply_token, line_api.text_message(
            f'登録エリアを「{user.area_label or "設定した場所"}」に変更しました。'))


# ---------- 依頼する ----------

def _start_post(user, state, reply_token):
    reason = services.post_block_reason(user)
    if reason:
        _reply(reply_token, line_api.text_message(reason))
        return
    _save(state, 'post_title')
    _reply(reply_token, line_api.text_message(
        '依頼の投稿をはじめます（途中で「やめる」と送れば中止できます）。\n'
        'まず、タイトルを送ってください。例: 買い物代行お願いします'))


def _step_input(user, state, text, reply_token):
    step = state.step
    if step in ('post_area', 'area_how'):
        _area_how_input(user, state, text, reply_token)
    elif step == 'area_text':
        _area_text_input(user, state, text, reply_token)
    elif step == 'area_pick':
        _area_pick_input(user, state, text, reply_token)
    elif step == 'area_location':
        _reply(reply_token, line_api.text_message(
            '位置情報を待っています。トーク画面の「＋」ボタン→「位置情報」から送ってください。やめるときは「やめる」と送ってください。'))
    elif step == 'post_title':
        if not text:
            _reply(reply_token, line_api.text_message('タイトルを送ってください。'))
            return
        _save(state, 'post_desc', title=text[:200])
        _reply(reply_token, line_api.text_message('詳しい内容を送ってください。なければ「なし」と送ってください。'))
    elif step == 'post_desc':
        _save(state, 'post_mode', description='' if text == 'なし' else text[:1000])
        _reply(reply_token, line_api.text_message(
            '募集の方式を選んでください。',
            quick_reply_items=[('先着順（すぐ決定）', '先着順'), ('承認制（応募者から選ぶ）', '承認制')]))
    elif step == 'post_mode':
        if text not in ('先着順', '承認制'):
            _reply(reply_token, line_api.text_message(
                '下のボタンから選んでください。',
                quick_reply_items=[('先着順（すぐ決定）', '先着順'), ('承認制（応募者から選ぶ）', '承認制')]))
            return
        _save(state, 'post_duration', mode='first_come' if text == '先着順' else 'approval')
        _reply(reply_token, line_api.text_message(
            '手伝ってほしい時間の目安を選んでください。',
            quick_reply_items=[(label, label) for label in DURATIONS]))
    elif step == 'post_duration':
        if text not in DURATIONS:
            _reply(reply_token, line_api.text_message(
                '下のボタンから選んでください。',
                quick_reply_items=[(label, label) for label in DURATIONS]))
            return
        _save(state, 'post_when', duration=DURATIONS[text])
        _reply(reply_token, line_api.text_message(
            'いつの依頼ですか？',
            quick_reply_items=[('今すぐ', '今すぐ'), ('日時を指定する', '日時を指定')]))
    elif step == 'post_when':
        if text == '日時を指定':
            _clear(state)
            _reply(reply_token, line_api.text_message(
                '日時の指定はサイトの依頼フォームからお願いします。\n'
                f'{_abs(reverse("core:help_request_create"))}'))
            return
        if text != '今すぐ':
            _reply(reply_token, line_api.text_message(
                '下のボタンから選んでください。',
                quick_reply_items=[('今すぐ', '今すぐ'), ('日時を指定する', '日時を指定')]))
            return
        _ask_area_how(user, state, reply_token)
    else:
        _clear(state)
        _reply(reply_token, line_api.text_message('最初からやり直してください。メニューの「依頼する」からどうぞ。'))


def _confirm_message(user, data):
    mode_label = '先着順（すぐ決定）' if data.get('mode') == 'first_come' else '承認制（応募者から選ぶ）'
    lines = [
        {'type': 'text', 'text': 'この内容で投稿しますか？', 'weight': 'bold', 'size': 'lg', 'wrap': True},
        {'type': 'text', 'text': f"タイトル: {data.get('title', '')}", 'wrap': True, 'margin': 'md'},
        {'type': 'text', 'text': f"内容: {data.get('description') or '（なし）'}", 'wrap': True, 'size': 'sm', 'color': '#666666'},
        {'type': 'text', 'text': f"方式: {mode_label}", 'size': 'sm', 'color': '#666666'},
        {'type': 'text', 'text': f"時間の目安: 約{data.get('duration', 15)}分 / 今すぐ", 'size': 'sm', 'color': '#666666'},
        {'type': 'text', 'text': f"エリア: {user.area_label or '設定済みの場所'}", 'size': 'sm', 'color': '#666666'},
    ]
    bubble = {
        'type': 'bubble',
        'body': {'type': 'box', 'layout': 'vertical', 'contents': lines},
        'footer': {'type': 'box', 'layout': 'vertical', 'spacing': 'sm', 'contents': [
            {'type': 'button', 'style': 'primary', 'color': GREEN,
             'action': {'type': 'postback', 'label': '投稿する', 'data': 'action=post_confirm', 'displayText': '投稿する'}},
            {'type': 'button', 'style': 'link',
             'action': {'type': 'postback', 'label': 'やめる', 'data': 'action=cancel', 'displayText': 'やめる'}},
        ]},
    }
    return {'type': 'flex', 'altText': '依頼内容の確認', 'contents': bubble}


def _post_confirm(user, state, reply_token):
    data = state.data or {}
    if state.step != 'post_confirm' or not data.get('title'):
        _reply(reply_token, line_api.text_message('投稿の準備ができていません。メニューの「依頼する」からやり直してください。'))
        return
    reason = services.post_block_reason(user)
    if reason:
        _clear(state)
        _reply(reply_token, line_api.text_message(reason))
        return
    help_request = HelpRequest.objects.create(
        requester=user, title=data['title'], description=data.get('description', ''),
        mode=data.get('mode', 'first_come'), duration_minutes=data.get('duration', 15),
        latitude=data.get('latitude', user.latitude), longitude=data.get('longitude', user.longitude))
    count = services.publish_request(help_request)
    _clear(state)
    _reply(reply_token, line_api.text_message(
        f'依頼を投稿しました！近くの{count}人に通知しました。\n'
        f'詳細: {_abs(services.detail_url(help_request))}'))


# ---------- 依頼を探す・引き受ける ----------

def _request_bubble(user, help_request):
    accept_label = '引き受ける' if help_request.mode == HelpRequest.Mode.FIRST_COME else '応募する'
    mode_label = '先着順' if help_request.mode == HelpRequest.Mode.FIRST_COME else '承認制'
    contents = [
        {'type': 'text', 'text': help_request.title, 'weight': 'bold', 'size': 'lg', 'wrap': True},
        {'type': 'text', 'text': f'{help_request.area_label or "エリア未設定"} ・ {mode_label}',
         'size': 'sm', 'color': '#666666', 'margin': 'sm'},
    ]
    distance = services.public_distance(user, help_request)
    when = '今すぐ' if help_request.scheduled_for is None else f'{timezone.localtime(help_request.scheduled_for):%m/%d %H:%M}〜'
    contents.append({'type': 'text',
                     'text': f'約{help_request.duration_minutes}分 ・ {services.distance_label(distance)} ・ {when}',
                     'size': 'sm', 'color': '#666666'})
    if help_request.description:
        contents.append({'type': 'text', 'text': help_request.description[:120],
                         'size': 'sm', 'color': '#666666', 'wrap': True, 'margin': 'sm'})
    return {
        'type': 'bubble',
        'body': {'type': 'box', 'layout': 'vertical', 'contents': contents},
        'footer': {'type': 'box', 'layout': 'vertical', 'spacing': 'sm', 'contents': [
            {'type': 'button', 'style': 'primary', 'color': GREEN,
             'action': {'type': 'postback', 'label': accept_label,
                        'data': f'action=accept&req={help_request.pk}', 'displayText': accept_label}},
            {'type': 'button', 'style': 'link',
             'action': {'type': 'uri', 'label': '詳細を見る', 'uri': _abs(services.detail_url(help_request))}},
        ]},
    }


def _browse(user, reply_token):
    services.expire_requests()
    qs = (HelpRequest.objects.filter(status=HelpRequest.Status.OPEN, expires_at__gt=timezone.now())
          .exclude(requester=user).select_related('requester'))
    items = list(qs)
    if user.latitude is not None and user.longitude is not None:
        items.sort(key=lambda r: services.public_distance(user, r))
    else:
        items.sort(key=lambda r: r.created_at, reverse=True)
    items = items[:8]
    if not items:
        _reply(reply_token, line_api.text_message('募集中の依頼は今のところありません。また後で見てみてください。'))
        return
    carousel = {'type': 'flex', 'altText': '募集中の依頼',
                'contents': {'type': 'carousel', 'contents': [_request_bubble(user, r) for r in items]}}
    _reply(reply_token, [line_api.text_message(f'募集中の依頼が{len(items)}件あります。'), carousel])


def _accept(user, req_pk, reply_token):
    try:
        help_request = HelpRequest.objects.get(pk=int(req_pk))
    except (ValueError, HelpRequest.DoesNotExist):
        _reply(reply_token, line_api.text_message('その依頼は見つかりませんでした。'))
        return
    if not help_request.is_open:
        _reply(reply_token, line_api.text_message('この依頼はすでに終了しています。'))
        return
    try:
        if help_request.mode == HelpRequest.Mode.FIRST_COME:
            services.accept_first_come(help_request, user)
            text = (f'「{help_request.title}」を引き受けました！依頼者に通知しました。\n'
                    f'チャットはこちら: {_abs(services.detail_url(help_request))}')
        else:
            services.apply(help_request, user)
            text = f'「{help_request.title}」に応募しました。依頼者の承認をお待ちください。'
    except services.ServiceError as error:
        text = str(error)
    _reply(reply_token, line_api.text_message(text))


# ---------- 進行中 ----------

def _status(user, reply_token):
    matches, pending = services.active_items(user)
    lines = []
    if matches:
        lines.append('【進行中のマッチング】')
        for match in matches:
            other = match.other_user(user).username
            lines.append(f'・「{match.help_request.title}」（相手: {other}さん）')
    if pending:
        lines.append('【応募が来ている自分の依頼】')
        for help_request in pending:
            lines.append(f'・「{help_request.title}」に応募 {help_request.applicants}件')
    own_open = HelpRequest.objects.filter(
        requester=user, status=HelpRequest.Status.OPEN, expires_at__gt=timezone.now()).count()
    if own_open:
        lines.append(f'募集中の自分の依頼: {own_open}件')
    if not lines:
        lines.append('進行中のマッチングや依頼はありません。')
    lines.append(f'サイトで確認: {_abs("/")}')
    _reply(reply_token, line_api.text_message('\n'.join(lines)))
