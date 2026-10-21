"""モデルに依存しない文字列処理。"""
import unicodedata

KATAKANA_START, KATAKANA_END = ord('ァ'), ord('ヶ')


def normalize_text(value):
    """表記ゆれ吸収: NFKC(全角英数→半角)、カタカナ→ひらがな、小文字化、空白除去。"""
    value = unicodedata.normalize('NFKC', value or '').lower()
    value = ''.join(
        chr(ord(ch) - 0x60) if KATAKANA_START <= ord(ch) <= KATAKANA_END else ch for ch in value)
    return ''.join(value.split())


try:
    import pykakasi
    _kakasi = pykakasi.kakasi()
except ImportError:  # pragma: no cover - 未インストール時は表記のみで判定する
    _kakasi = None


def reading(value):
    """漢字かな混じりの読み(ひらがな・正規化済み)。「見守り」→「みまもり」。"""
    if _kakasi is None:
        return normalize_text(value)
    return normalize_text(''.join(item['hira'] for item in _kakasi.convert(value or '')))
