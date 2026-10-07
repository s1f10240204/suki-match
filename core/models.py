from django.contrib.auth.models import AbstractUser
from django.db import models

# (必要ポイント, ランク名) 大きい順に判定する
RANKS = [
    (300, 'マスター'),
    (100, 'ベテラン'),
    (30, 'レギュラー'),
    (0, 'ビギナー'),
]


class User(AbstractUser):
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    points = models.IntegerField(default=0)
    rank = models.CharField(max_length=50, default='ビギナー')
    skills = models.TextField(blank=True, help_text='できること。カンマ区切り(例: 見守り, 買い物, 荷物運び)')

    @property
    def skill_list(self):
        return [s.strip() for s in self.skills.replace('、', ',').split(',') if s.strip()]

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    def add_points(self, amount):
        self.points += amount
        self.rank = next(name for threshold, name in RANKS if self.points >= threshold)
        self.save(update_fields=['points', 'rank'])


class HelpRequest(models.Model):
    OPEN = '募集中'
    IN_PROGRESS = '進行中'
    DONE = '完了'
    CANCELED = 'キャンセル'

    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='requests')
    title = models.CharField(max_length=200)
    description = models.TextField()
    required_skill = models.CharField(max_length=50, blank=True, help_text='空欄なら誰でもOK')
    duration_minutes = models.PositiveIntegerField(default=15)
    latitude = models.FloatField()
    longitude = models.FloatField()
    status = models.CharField(max_length=20, default=OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class Match(models.Model):
    IN_PROGRESS = '進行中'
    DONE = '完了'
    CANCELED = 'キャンセル'

    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name='matches')
    helper = models.ForeignKey(User, on_delete=models.CASCADE, related_name='helps')
    status = models.CharField(max_length=20, default=IN_PROGRESS)
    created_at = models.DateTimeField(auto_now_add=True)


class Review(models.Model):
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='given_reviews')
    reviewee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_reviews')
    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE)
    rating = models.IntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['reviewer', 'help_request'], name='one_review_per_request'),
        ]
