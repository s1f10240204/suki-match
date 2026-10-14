from django.core.management.base import BaseCommand

from core import skills


class Command(BaseCommand):
    help = '表記の似ているスキルタグの組を一覧表示する(統合は管理画面のアクションで行う)'

    def add_arguments(self, parser):
        parser.add_argument('--threshold', type=float, default=skills.SIMILARITY_THRESHOLD)

    def handle(self, *args, **options):
        pairs = skills.similar_pairs(options['threshold'])
        for a, b, score in pairs:
            self.stdout.write(f'{score:.2f}  {a.name}  ⇔  {b.name}')
        self.stdout.write(f'{len(pairs)}組の統合候補')
