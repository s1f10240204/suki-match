"""チャット監視: ルール(正規表現・辞書)と行動パターンからリスクスコアを算出する。

閾値は settings.MODERATION_* で調整する。β期間は記録のみ(MODERATION_AUTO_HIDE=0)。
"""
import re
import unicodedata

from django.utils import timezone

# (ラベル, 重み, パターン)。本文はNFKC正規化・小文字化してから照合する
RULES = [
    ('contact_exchange', 0.4, re.compile(r'(line|ライン)\s*(id|交換|教え)|カカオ|instagram|インスタ|dm(で|して)')),
    ('contact_exchange', 0.4, re.compile(r'0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}')),          # 電話番号
    ('contact_exchange', 0.4, re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')),                      # メール
    ('external_link', 0.3, re.compile(r'https?://')),
    ('dating', 0.5, re.compile(r'出会い|デート|彼氏|彼女|付き合|二人きり|ふたりきり|飲みに行|ホテル|泊ま')),
    ('sexual', 0.8, re.compile(r'エッチ|えっち|セックス|裸|下着|援助|パパ活|ママ活')),
    ('money', 0.4, re.compile(r'振込|振り込|口座|先払い|手数料|暗号資産|投資')),
]

ONE_SIDED_MESSAGES = 5  # 相手の返信がないまま連投した件数
LATE_NIGHT_HOURS = range(0, 5)


def normalize(text):
    return unicodedata.normalize('NFKC', text).lower()


def rule_score(text):
    text = normalize(text)
    score, labels = 0.0, []
    for label, weight, pattern in RULES:
        if pattern.search(text):
            score += weight
            if label not in labels:
                labels.append(label)
    return score, labels


def behavior_score(match, sender, now=None):
    """送信者の行動パターン: 一方的な連投、深夜帯。"""
    now = now or timezone.now()
    score, labels = 0.0, []
    recent = list(match.messages.order_by('-created_at').values_list('sender_id', flat=True)[:ONE_SIDED_MESSAGES])
    if len(recent) == ONE_SIDED_MESSAGES and all(sender_id == sender.pk for sender_id in recent):
        score += 0.2
        labels.append('one_sided')
    if timezone.localtime(now).hour in LATE_NIGHT_HOURS:
        score += 0.1
        labels.append('late_night')
    return score, labels


def assess(match, sender, body):
    """(リスクスコア 0〜1, ラベル一覧)"""
    s1, l1 = rule_score(body)
    s2, l2 = behavior_score(match, sender)
    return min(1.0, round(s1 + s2, 2)), l1 + l2
