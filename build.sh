#!/usr/bin/env bash
# Render のビルド時に実行する(render.yaml の buildCommand)
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput

# 管理者アカウント(無料プランはシェルが使えないため、環境変数から自動作成する)
# DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD を設定しておく
if [ -n "$DJANGO_SUPERUSER_USERNAME" ]; then
    python manage.py createsuperuser --noinput || echo "管理者はすでに存在します"
fi
