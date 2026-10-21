"""アップロードファイルをデータベースに保存するストレージ。

Render の無料プランのようにディスクが再起動で消える環境でも、顔写真が残るようにする。
写真は審査・モザイク判定つきのビュー(core:user_photo)からだけ配信するので、公開URLは持たない。
"""
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile  # モデル読み込み前に import されても動くように遅延させる
        return StoredFile

    def _open(self, name, mode='rb'):
        stored = self._model().objects.filter(name=name).only('content').first()
        if stored is None:
            raise FileNotFoundError(name)
        return ContentFile(bytes(stored.content), name=name)

    def _save(self, name, content):
        data = b''.join(content.chunks())
        self._model().objects.update_or_create(name=name, defaults={'content': data, 'size': len(data)})
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        stored = self._model().objects.filter(name=name).only('size').first()
        if stored is None:
            raise FileNotFoundError(name)
        return stored.size

    def url(self, name):
        raise NotImplementedError('写真は core:user_photo から配信します。')
