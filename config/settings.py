import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# ローカルの秘密情報(メールのパスワード等)は .env から読む。.env は Git に含めない
load_dotenv(BASE_DIR / '.env')


def env_bool(name, default):
    return os.environ.get(name, '1' if default else '0') == '1'


SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY', 'django-insecure-#(c5umtbor79n^-*e)bz8a9vz74z4k^9&7%h)42rpo41dz0wb9')

DEBUG = env_bool('DJANGO_DEBUG', True)

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
# Render が自動で設定する公開ホスト名(例: suki-match.onrender.com)
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
CSRF_TRUSTED_ORIGINS = [f'https://{host}' for host in ALLOWED_HOSTS if host not in ('localhost', '127.0.0.1')]

if not DEBUG:
    if SECRET_KEY.startswith('django-insecure'):
        raise ImproperlyConfigured('本番では環境変数 DJANGO_SECRET_KEY を設定してください。')
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r'^healthz/$']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'anymail',
    'core',  # allauth より前に置き、core/templates/allauth/ の上書きを優先させる
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
]
# Googleの認証情報が設定されているときだけGoogleログインを有効化する
if GOOGLE_CLIENT_ID:
    INSTALLED_APPS.append('allauth.socialaccount.providers.google')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # 本番で静的ファイル(CSS/JS)を配信する
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.app_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

ASGI_APPLICATION = 'config.asgi.application'

# 本番(複数プロセス)では REDIS_URL を設定する。未設定時は開発用のインメモリ
REDIS_URL = os.environ.get('REDIS_URL', '')
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        }
    }
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        }
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer'
        }
    }

# 例: DATABASE_URL=postgres://user:pass@localhost:5432/sukimatch (psycopg が必要)
DATABASES = {
    # ASGI(Daphne)では接続を使い回すと不具合が出やすいので、リクエストごとに閉じる
    'default': dj_database_url.config(default=f'sqlite:///{BASE_DIR / "db.sqlite3"}', conn_max_age=0),
}

AUTH_USER_MODEL = 'core.User'

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'ja'

TIME_ZONE = 'Asia/Tokyo'

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# 顔写真の保存先。Render 無料プランはディスクが再起動で消えるため、本番ではDBに保存する
STORAGES = {
    'default': {
        'BACKEND': 'core.storage.DatabaseStorage' if env_bool('MEDIA_IN_DATABASE', False)
        else 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

LOGIN_URL = 'account_login'
LOGIN_REDIRECT_URL = 'core:help_request_list'
LOGOUT_REDIRECT_URL = 'core:help_request_list'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- サイト情報(メールの件名・本文に出るサービス名とドメイン。core.adapters で使う) ---
SITE_NAME = 'suki-match'
SITE_DOMAIN = os.environ.get('SITE_DOMAIN', RENDER_EXTERNAL_HOSTNAME or 'localhost:8000')

# --- django-allauth ---
ACCOUNT_LOGIN_METHODS = {'username', 'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = os.environ.get('ACCOUNT_EMAIL_VERIFICATION', 'mandatory')
# メール確認はURLではなく6桁の数字コードを入力する方式
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_FORMAT = {'length': 6, 'numeric': True, 'dashed': False}
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT = 15 * 60
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_MAX_ATTEMPTS = 5
ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_RESEND = 3   # コードの再送は3回まで
EMAIL_CODE_RESEND_SECONDS = 60                   # 再送ボタンが押せるまでの秒数(画面のタイマー)
ACCOUNT_RATE_LIMITS = {'confirm_email': f'1/{EMAIL_CODE_RESEND_SECONDS}s/key'}
ACCOUNT_EMAIL_SUBJECT_PREFIX = '[suki-match] '
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'http' if DEBUG else 'https'
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = False
ACCOUNT_SIGNUP_REDIRECT_URL = 'core:profile_edit'
ACCOUNT_ADAPTER = 'core.adapters.AccountAdapter'
# Googleログイン: 確認画面を挟まずにGoogleへ移動し、Googleで確認済みのメールはそのまま信用する
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APPS': [{'client_id': GOOGLE_CLIENT_ID, 'secret': GOOGLE_CLIENT_SECRET}] if GOOGLE_CLIENT_ID else [],
        'SCOPE': ['profile', 'email'],
        'VERIFIED_EMAIL': True,
    },
}

# --- メール ---
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
# Render 無料プランは SMTP(587番など)が使えないため、Brevo の HTTPS API で送る
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
if BREVO_API_KEY:
    EMAIL_BACKEND = 'anymail.backends.brevo.EmailBackend'
    ANYMAIL = {'BREVO_API_KEY': BREVO_API_KEY}
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'suki-match <noreply@suki-match.local>')
SITE_URL = os.environ.get('SITE_URL', f'https://{RENDER_EXTERNAL_HOSTNAME}' if RENDER_EXTERNAL_HOSTNAME
                          else 'http://127.0.0.1:8000')
# 定期処理(/tasks/run/)を外部(GitHub Actions)から呼ぶときの合言葉
TASKS_TOKEN = os.environ.get('TASKS_TOKEN', '')

# --- Suki-match ---
# Google Maps JavaScript API(地図表示と住所検索)。未設定時は OpenStreetMap + 国土地理院の住所検索で代替する
GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')
GOOGLE_MAPS_MAP_ID = os.environ.get('GOOGLE_MAPS_MAP_ID', 'DEMO_MAP_ID')
# 国土地理院APIで座標を「東京都北区赤羽」形式に変換する(オフライン開発では 0 に)
GEOCODER_ENABLED = env_bool('GEOCODER_ENABLED', True)
# チャット監視: β期間は記録のみ(0)。1にすると高リスクのメッセージを自動で非表示にする
MODERATION_AUTO_HIDE = env_bool('MODERATION_AUTO_HIDE', False)
MODERATION_REVIEW_THRESHOLD = float(os.environ.get('MODERATION_REVIEW_THRESHOLD', '0.5'))
MODERATION_HIDE_THRESHOLD = float(os.environ.get('MODERATION_HIDE_THRESHOLD', '0.8'))
