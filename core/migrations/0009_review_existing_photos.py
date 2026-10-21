"""審査制度の導入前に登録された顔写真を、EXIF除去・ファイル名変更のうえ「審査中」にする。"""
import uuid
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import migrations
from PIL import Image, ImageOps, UnidentifiedImageError


def convert(apps, schema_editor):
    User = apps.get_model('core', 'User')
    for user in User.objects.exclude(avatar='').filter(avatar_status='none'):
        old = user.avatar.name
        try:
            with default_storage.open(old, 'rb') as f:
                image = ImageOps.exif_transpose(Image.open(f)).convert('RGB')
        except (FileNotFoundError, UnidentifiedImageError, OSError):
            user.avatar = ''  # 読めない・存在しない写真は登録し直してもらう
            user.save(update_fields=['avatar'])
            continue
        image.thumbnail((800, 800))
        buffer = BytesIO()
        image.save(buffer, format='JPEG', quality=85)  # EXIF(撮影場所など)は書き出さない
        user.avatar = default_storage.save(f'avatars/{uuid.uuid4().hex}.jpg', ContentFile(buffer.getvalue()))
        user.avatar_status = 'pending'
        user.save(update_fields=['avatar', 'avatar_status'])
        default_storage.delete(old)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_fill_skill_reading'),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
