"""顔写真の処理: 位置情報(EXIF)の除去・縮小と、マッチング前に見せるモザイク画像の生成。"""
from io import BytesIO
from pathlib import PurePosixPath

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

MAX_SIDE = 800          # 保存する写真の最大辺(px)
MOSAIC_BLOCKS = 10      # モザイクの粗さ(縦横のブロック数)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000  # 解凍爆弾対策: デコード前にピクセル数の上限を検証する
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class InvalidImage(Exception):
    pass


def _to_jpeg(image, quality=85):
    buffer = BytesIO()
    image.save(buffer, format='JPEG', quality=quality, optimize=True)  # EXIFは書き出さない
    return buffer.getvalue()


def normalize_photo(uploaded):
    """アップロードされた画像 → 向きを補正し、EXIF(撮影場所など)を除いたJPEG。"""
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise InvalidImage('画像は8MB以下にしてください。')
    try:
        image = Image.open(uploaded)
        if image.width * image.height > MAX_PIXELS:
            raise InvalidImage('画像の解像度が大きすぎます。')
        image = ImageOps.exif_transpose(image).convert('RGB')
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise InvalidImage('画像ファイルを読み込めませんでした。JPEGかPNGを選んでください。')
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    return ContentFile(_to_jpeg(image), name='photo.jpg')


def photo_signature(data):
    """写真の近似指紋(128bit: dHash64 + aHash64)を32桁の16進文字列で返す。
    同じ写真の再圧縮・リサイズでは近い値になり、別アカウントでの使い回し検知に使う。
    真っ平らな画像(指紋が全部0)や読み込めない画像は ''(判定対象外)。"""
    try:
        image = Image.open(BytesIO(data)).convert('L')
    except Exception:
        return ''
    small = image.resize((9, 8), Image.BILINEAR)
    px = list(small.getdata())
    d_bits = []
    for row in range(8):
        base = row * 9
        for col in range(8):
            d_bits.append(1 if px[base + col] > px[base + col + 1] else 0)
    avg_img = image.resize((8, 8), Image.BILINEAR)
    vals = list(avg_img.getdata())
    mean = sum(vals) / len(vals)
    a_bits = [1 if v > mean else 0 for v in vals]
    if not any(d_bits) and not any(a_bits):
        return ''
    value = 0
    for bit in d_bits + a_bits:
        value = (value << 1) | bit
    return f'{value:032x}'


def signature_distance(sig_a, sig_b):
    """指紋同士の距離を (dHash距離, aHash距離) で返す。比較不能なら (999, 999)。"""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return (999, 999)
    xor = int(sig_a, 16) ^ int(sig_b, 16)
    return (bin(xor >> 64).count('1'), bin(xor & ((1 << 64) - 1)).count('1'))


def mosaic_path(photo_name):
    path = PurePosixPath(photo_name)
    return str(path.parent / 'mosaic' / path.name)


def ensure_mosaic(photo_name):
    """モザイク版を(なければ)作って、そのパスを返す。"""
    target = mosaic_path(photo_name)
    if not default_storage.exists(target):
        with default_storage.open(photo_name, 'rb') as f:
            image = Image.open(f).convert('RGB')
        width, height = image.size
        small = image.resize((MOSAIC_BLOCKS, max(1, MOSAIC_BLOCKS * height // width)), Image.BILINEAR)
        mosaic = small.resize((width, height), Image.NEAREST).filter(ImageFilter.GaussianBlur(4))
        default_storage.save(target, ContentFile(_to_jpeg(mosaic, quality=70)))
    return target


def delete_photo(photo_name):
    for name in (photo_name, mosaic_path(photo_name)):
        if name and default_storage.exists(name):
            default_storage.delete(name)
