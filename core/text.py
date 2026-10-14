"""モデルに依存しない文字列処理。"""
import unicodedata

KATAKANA_START, KATAKANA_END = ord('ァ'), ord('ヶ')


def normalize_text(value):
    """表記ゆれ吸収: NFKC(全角英数→半角)、カタカナ→ひらがな、小文字化、空白除去。"""
    value = unicodedata.normalize('NFKC', value or '').lower()
    value = ''.join(
        chr(ord(ch) - 0x60) if KATAKANA_START <= ord(ch) <= KATAKANA_END else ch for ch in value)
    return ''.join(value.split())
