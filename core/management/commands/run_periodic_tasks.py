import time

from django.core.management.base import BaseCommand

from core import services


class Command(BaseCommand):
    help = '掲載期限切れの締め切りと、通知メールのまとめ送信を行う(cron等で数分おきに実行)'

    def add_arguments(self, parser):
        parser.add_argument('--loop', type=int, default=0, help='指定秒ごとに繰り返す(開発用)')

    def handle(self, *args, **options):
        while True:
            expired = services.expire_requests()
            mailed = services.send_email_digests()
            self.stdout.write(f'締め切り: {expired}件 / メール送信: {mailed}人')
            if not options['loop']:
                break
            time.sleep(options['loop'])
