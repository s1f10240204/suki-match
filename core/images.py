"""顔写真の処理: 位置情報(EXIF)の除去・縮小と、マッチング前に見せるモザイク画像の生成。"""
from io import BytesIO
from pathlib import PurePosixPath

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

MAX_SIDE = 800          # 保存する写真の最大辺(px)
MOSAIC_BLOCKS = 10      # モザイクの粗さ(縦横のブロック数)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


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
        image = ImageOps.exif_transpose(image).convert('RGB')
    except (UnidentifiedImageError, OSError):
        raise InvalidImage('画像ファイルを読み込めませんでした。JPEGかPNGを選んでください。')
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    return ContentFile(_to_jpeg(image), name='photo.jpg')


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
