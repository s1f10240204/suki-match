from django.contrib import admin
from django.urls import include, path

# アップロードされた顔写真は /media/ で直接配信せず、審査・モザイク判定つきの
# core:user_photo(/photos/<id>/)からだけ見られるようにしている
urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('', include('core.urls')),
]
