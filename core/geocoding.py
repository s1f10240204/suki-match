"""国土地理院APIによる逆ジオコーディング(座標 → 「東京都北区赤羽」)。

APIへは座標だけを送り、結果は area_label として保存する。画面表示のたびには呼ばない。
"""
import json
import logging
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

REVERSE_URL = 'https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress'
MUNI_URL = 'https://maps.gsi.go.jp/js/muni.js'
MUNI_CACHE_KEY = 'gsi_muni_table'
TIMEOUT_SECONDS = 3
CHOME_PATTERN = re.compile(r'[0-9０-９一二三四五六七八九十〇]+丁目$')


def _fetch(url):
    request = Request(url, headers={'User-Agent': 'Suki-match/1.0'})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode('utf-8')


def _muni_table():
    """市区町村コード → (都道府県, 市区町村) の対応表。1か月キャッシュする。"""
    table = cache.get(MUNI_CACHE_KEY)
    if table is None:
        # 例: GSI.MUNI_ARRAY["13117"] = '13,東京都,13117,北区';
        table = {}
        for code, value in re.findall(r'MUNI_ARRAY\["(\d+)"\]\s*=\s*\'([^\']*)\'', _fetch(MUNI_URL)):
            parts = value.split(',')
            if len(parts) >= 4:
                table[code.lstrip('0')] = (parts[1], parts[3].replace('　', ''))
        cache.set(MUNI_CACHE_KEY, table, 60 * 60 * 24 * 30)
    return table


def area_label_for(lat, lng):
    """市区町村+町名までの大まかな地名。取得できなければ空文字。"""
    if not settings.GEOCODER_ENABLED:
        return ''
    try:
        data = json.loads(_fetch(f'{REVERSE_URL}?{urlencode({"lat": lat, "lon": lng})}'))
        result = data.get('results') or {}
        pref, city = _muni_table().get(str(result.get('muniCd', '')).lstrip('0'), ('', ''))
        town = CHOME_PATTERN.sub('', result.get('lv01Nm') or '')
        if town in ('-', '−'):
            town = ''
        return f'{pref}{city}{town}'[:50]
    except Exception:  # ネットワーク障害等で依頼投稿を止めない
        logger.warning('reverse geocoding failed', exc_info=True)
        return ''


SEARCH_URL = 'https://msearch.gsi.go.jp/address-search/AddressSearch'


def search_area(query):
    """地名・住所・駅名などから候補を最大10件返す（国土地理院 住所検索API）。

    各候補は {'name', 'lat', 'lng'}。取得できなければ空リスト。
    """
    if not settings.GEOCODER_ENABLED or not query:
        return []
    try:
        data = json.loads(_fetch(f'{SEARCH_URL}?{urlencode({"q": query})}'))
        results = []
        for feature in data[:10]:
            coords = (feature.get('geometry') or {}).get('coordinates') or []
            title = (feature.get('properties') or {}).get('title', '')
            if len(coords) == 2 and title:
                results.append({'name': title, 'lat': coords[1], 'lng': coords[0]})
        return results
    except Exception:  # ネットワーク障害等で操作を止めない
        logger.warning('forward geocoding failed', exc_info=True)
        return []
