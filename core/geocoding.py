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
ZIP_URL = 'https://zipcloud.ibsnet.co.jp/api/search'
NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
SEARCH_CACHE_SECONDS = 300
_FULLWIDTH = str.maketrans('０１２３４５６７８９－', '0123456789-')
_ZIP_PART = re.compile(r'^〒?\d{3}-\d{4}$')


def _normalize_query(query):
    text = (query or '').translate(_FULLWIDTH).replace('〒', ' ').replace('　', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _haversine_m(lat1, lng1, lat2, lng2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def _gsi_candidates(query):
    data = json.loads(_fetch(f'{SEARCH_URL}?{urlencode({"q": query})}'))
    results = []
    for feature in data[:10]:
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        title = (feature.get('properties') or {}).get('title', '')
        if len(coords) == 2 and title:
            results.append({'name': title, 'lat': coords[1], 'lng': coords[0]})
    return results


def _zip_candidates(digits):
    """郵便番号7桁 → 住所候補。zipcloud で住所を引き、国土地理院で座標を付ける。"""
    data = json.loads(_fetch(f'{ZIP_URL}?{urlencode({"zipcode": digits})}'))
    results = []
    for item in (data.get('results') or [])[:3]:
        address = ''.join(item.get(k, '') for k in ('address1', 'address2', 'address3'))
        if not address:
            continue
        for cand in _gsi_candidates(address)[:1]:
            results.append({'name': f'〒{digits[:3]}-{digits[3:]} {address}',
                            'lat': cand['lat'], 'lng': cand['lng']})
    return results


def _nominatim_label(display_name):
    parts = [p.strip() for p in (display_name or '').split(',')]
    parts = [p for p in parts if p and p != '日本' and not _ZIP_PART.match(p)]
    return ' '.join(parts)


def _nominatim_candidates(query, limit=8):
    """駅名・施設名など、住所ではない地名の候補(OpenStreetMap / Nominatim)。"""
    params = urlencode({'q': query, 'format': 'jsonv2', 'countrycodes': 'jp', 'limit': limit})
    data = json.loads(_fetch(f'{NOMINATIM_URL}?{params}'))
    results = []
    for item in data:
        try:
            lat, lng = float(item['lat']), float(item['lon'])
        except (KeyError, TypeError, ValueError):
            continue
        label = _nominatim_label(item.get('display_name', ''))
        if label:
            results.append({'name': label, 'lat': lat, 'lng': lng})
    return results


_KANJI_DIGITS = str.maketrans('一二三四五六七八九', '123456789')


def _match_norm(text):
    """関連性チェック用の正規化(漢数字→数字、空白除去など)。"""
    text = (text or '').translate(_FULLWIDTH).translate(_KANJI_DIGITS)
    text = text.replace('十', '').replace(' ', '').replace('　', '').replace('・', '')
    return text


def _lcs_len(a, b):
    """最長共通部分文字列の長さ。"""
    if len(a) > len(b):
        a, b = b, a
    best = 0
    prev = [0] * (len(a) + 1)
    for j in range(1, len(b) + 1):
        cur = [0] * (len(a) + 1)
        for i in range(1, len(a) + 1):
            if a[i - 1] == b[j - 1]:
                cur[i] = prev[i - 1] + 1
                best = max(best, cur[i])
        prev = cur
    return best


def _relevant(query, title, min_lcs=None):
    """部分一致ノイズ対策: 検索語と候補タイトルの共通部分が十分長い候補だけ残す。

    「ラーメン山岡家…」のような店名検索で「ラ」だけ一致する無関係な地名
    (ラントウ前、ラッコベツ等)が大量に返るのを防ぐ。
    """
    core = _match_norm(query)
    if not core:
        return False
    threshold = min_lcs if min_lcs else (2 if len(core) <= 3 else 3)
    return _lcs_len(core, _match_norm(title)) >= threshold


def _area_fallback(query):
    """店名などで直接見つからないとき、末尾の地名らしい部分で探し直す。

    例: 「ラーメン山岡家武蔵村山店」→「武蔵村山」で武蔵村山市が引ける。
    Nominatim(駅・地名)を優先し、住所らしい接尾辞は GSI でも探す。
    """
    core = query[:-1] if query.endswith('店') else query
    for start in range(0, min(8, len(core) - 2)):
        suffix = core[start:]
        if len(suffix) < 3:
            break
        if suffix[0] in 'ー・っゃゅょ':
            continue  # 単語の途中から始まる接尾辞はノイズが多い
        try:
            hits = [c for c in _nominatim_candidates(suffix, limit=3)
                    if _relevant(suffix, c['name'], min_lcs=3)]
            if not hits:
                min_lcs = 3 if len(suffix) <= 3 else 4
                hits = [c for c in _gsi_candidates(suffix)
                        if _relevant(suffix, c['name'], min_lcs=min_lcs)]
        except Exception:
            logger.warning('fallback search failed', exc_info=True)
            return []
        if hits:
            return hits[:3]
    return []


def _merged_candidates(normalized):
    digits = re.sub(r'[\s-]', '', normalized)
    if re.fullmatch(r'\d{7}', digits):
        return _zip_candidates(digits)
    try:
        results = [c for c in _gsi_candidates(normalized) if _relevant(normalized, c['name'])]
    except Exception:
        logger.warning('gsi search failed', exc_info=True)
        results = []
    seen = {(round(r['lat'], 4), round(r['lng'], 4)) for r in results}
    try:
        for cand in _nominatim_candidates(normalized):
            key = (round(cand['lat'], 4), round(cand['lng'], 4))
            if key not in seen and _relevant(normalized, cand['name']):
                seen.add(key)
                results.append(cand)
    except Exception:
        logger.warning('nominatim search failed', exc_info=True)
    if not results:
        results = _area_fallback(normalized)
    return results


def search_places(query, base_lat=None, base_lng=None, limit=8):
    """住所・地名・駅名・郵便番号から候補 {'name','lat','lng'} を返す。

    国土地理院(住所)と Nominatim(駅名・施設名等)を併用し、郵便番号も引ける。
    base_lat/base_lng があれば近い順に並べる。取得できなければ空リスト。
    """
    if not settings.GEOCODER_ENABLED or not query:
        return []
    normalized = _normalize_query(query)
    if not normalized:
        return []
    cache_key = f'place_search:{normalized}'
    results = cache.get(cache_key)
    if results is None:
        try:
            results = _merged_candidates(normalized)
        except Exception:  # ネットワーク障害等で操作を止めない
            logger.warning('place search failed', exc_info=True)
            return []
        cache.set(cache_key, results, SEARCH_CACHE_SECONDS)
    results = list(results)
    if base_lat is not None and base_lng is not None:
        results.sort(key=lambda r: _haversine_m(base_lat, base_lng, r['lat'], r['lng']))
    return results[:limit]


def search_area(query):
    """地名・住所・駅名・郵便番号から候補を返す(LINE のエリア指定用)。"""
    return search_places(query)
