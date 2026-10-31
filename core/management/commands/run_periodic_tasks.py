import time

from django.core.management.base import BaseCommand

from core import services


class Command(BaseCommand):
    help = '掲載期限切れの締め切りやチャット履歴の削除などを行う(cron等で数分おきに実行)'

    def add_arguments(self, parser):
        parser.add_argument('--loop', type=int, default=0, help='指定秒ごとに繰り返す(開発用)')

    def handle(self, *args, **options):
        while True:
            expired = services.expire_requests()
            purged = services.purge_expired_chat_messages()
            reminded = services.send_schedule_reminders()
            referrals = services.process_referral_rewards()
            self.stdout.write(f'締め切り: {expired}件 / チャット履歴削除: {purged}件 / リマインダー: {reminded}件 / 招待ボーナス: {referrals}件')
            if not options['loop']:
                break
            time.sleep(options['loop'])
