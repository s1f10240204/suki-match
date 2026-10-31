import uuid
from datetime import timedelta
from math import floor, sqrt

from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .text import normalize_text
from .text import reading as reading_of

LEVEL_MAX = 100
# (必要レベル, ランク名) 大きい順に判定する
RANKS = [
    (26, 'マスター'),
    (16, 'ベテラン'),
    (11, 'アクティブ'),
    (6, 'レギュラー'),
    (1, 'ビギナー'),
]

DEFAULT_LISTING_MINUTES = 180  # 掲載時間を指定しなかったときの既定(3時間)
# 公開用座標の格子サイズ(度)。緯度0.0025度 ≒ 約280m
PUBLIC_GRID_DEG = 0.0025


def level_for_points(points):
    """レベル1〜100。序盤は上がりやすく、後半は緩やかになる。"""
    return min(LEVEL_MAX, floor(sqrt(max(points, 0))) + 1)


def rank_for_level(level):
    return next(name for threshold, name in RANKS if level >= threshold)


def default_expiry():
    return timezone.now() + timedelta(minutes=DEFAULT_LISTING_MINUTES)


def snap_to_grid(value):
    return round(round(value / PUBLIC_GRID_DEG) * PUBLIC_GRID_DEG, 6)


class Skill(models.Model):
    name = models.CharField('スキル名', max_length=30, unique=True)
    normalized = models.CharField(max_length=30, db_index=True, editable=False)
    reading = models.CharField('読み', max_length=60, blank=True, db_index=True, editable=False)
    is_official = models.BooleanField('公式', default=False)
    created_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='created_skills', verbose_name='作成者')
    canonical = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='aliases', verbose_name='統合先',
                                  help_text='類似タグを統合するときに、代表となるタグを指定します')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = 'スキル'
        ordering = ['-is_official', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.normalized = normalize_text(self.name)
        self.reading = reading_of(self.name)[:60]
        super().save(*args, **kwargs)

    @property
    def resolved(self):
        return self.canonical or self


def chat_image_upload_to(instance, filename):
    return f'chat/{instance.match_id or "misc"}/{filename}'


def avatar_upload_to(instance, filename):
    # 推測されにくいファイル名にする(写真はモザイク判定つきのビュー経由でのみ配信)
    return f'avatars/{uuid.uuid4().hex}.jpg'


class User(AbstractUser):
    class PhotoStatus(models.TextChoices):
        NONE = 'none', '未登録'
        PENDING = 'pending', '審査中'
        APPROVED = 'approved', '承認済み'
        REJECTED = 'rejected', '再提出が必要'

    email = models.EmailField('メールアドレス', unique=True, null=True, blank=True)
    avatar = models.ImageField('顔写真', upload_to=avatar_upload_to, blank=True)
    avatar_status = models.CharField('顔写真の審査', max_length=20, choices=PhotoStatus.choices,
                                     default=PhotoStatus.NONE, db_index=True)
    avatar_reject_reason = models.CharField('差し戻し理由', max_length=200, blank=True)
    avatar_reviewed_at = models.DateTimeField('審査日時', null=True, blank=True)
    line_user_id = models.CharField('LINEユーザーID', max_length=64, blank=True, default='', db_index=True)
    birth_date = models.DateField('生年月日', null=True, blank=True)
    simple_ui = models.BooleanField('シンプルUI', default=False)
    is_shadow_banned = models.BooleanField('ゴーストBan', default=False,
                                           help_text='有効な間、本人の画面は通常どおりだが、他のユーザーからは存在しないように見える')
    motivation = models.CharField('お手伝いする理由', max_length=200, blank=True,
                                  help_text='例: 子育てがひと段落したので、地域の役に立ちたい')
    bio = models.TextField('自己紹介', blank=True)

    # 正確な位置は非公開。公開するのは area_label のみ
    latitude = models.FloatField('緯度', null=True, blank=True)
    longitude = models.FloatField('経度', null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    area_label = models.CharField('活動エリア', max_length=50, blank=True)

    points = models.IntegerField('ポイント', default=0)  # 使える残高
    total_earned = models.IntegerField('累計獲得ポイント', default=0)  # レベルはこちらで決まる(使っても減らない)
    level = models.PositiveSmallIntegerField('レベル', default=1)
    skills = models.ManyToManyField(Skill, blank=True, related_name='users', verbose_name='できること')

    def save(self, *args, **kwargs):
        if not self.email:
            self.email = None  # 空文字の重複で一意制約に引っかからないように
        super().save(*args, **kwargs)

    def age(self, on=None):
        """生年月日から満年齢を返す(未登録なら None)。"""
        if not self.birth_date:
            return None
        on = on or timezone.localdate()
        return on.year - self.birth_date.year - ((on.month, on.day) < (self.birth_date.month, self.birth_date.day))

    @property
    def rank(self):
        return rank_for_level(self.level)

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def photo_approved(self):
        return bool(self.avatar) and self.avatar_status == self.PhotoStatus.APPROVED

    def add_points(self, amount):
        # 行をロックしてから加算する(同時付与で加算が上書きされて消えないように)
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=self.pk)
            user.points += amount
            if amount > 0:
                user.total_earned += amount
            user.level = level_for_points(user.total_earned)
            user.save(update_fields=['points', 'total_earned', 'level'])
            self.points, self.total_earned, self.level = user.points, user.total_earned, user.level

    def resolved_skill_ids(self):
        return {s.resolved.pk for s in self.skills.select_related('canonical')}

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(condition=models.Q(points__gte=0),
                                   name='user_points_non_negative'),
            models.CheckConstraint(condition=models.Q(total_earned__gte=0),
                                   name='user_total_earned_non_negative'),
        ]


class PresetTag(models.Model):
    label = models.CharField('定型文', max_length=30, unique=True)
    order = models.PositiveSmallIntegerField('並び順', default=0)

    class Meta:
        verbose_name = verbose_name_plural = '定型タグ'
        ordering = ['order', 'pk']

    def __str__(self):
        return self.label


class HelpRequest(models.Model):
    class Mode(models.TextChoices):
        FIRST_COME = 'first_come', '先着順(すぐ決定)'
        APPROVAL = 'approval', '承認制(応募者から選ぶ)'

    class Status(models.TextChoices):
        OPEN = 'open', '募集中'
        IN_PROGRESS = 'in_progress', '進行中'
        DONE = 'done', '完了'
        CANCELED = 'canceled', '取り下げ'
        EXPIRED = 'expired', '期限切れ'

    class ChatRetention(models.IntegerChoices):
        IMMEDIATE = 0, '完了後すぐ削除'
        ONE_DAY = 1, '完了後1日'
        THREE_DAYS = 3, '完了後3日'
        SEVEN_DAYS = 7, '完了後7日(最長)'

    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='requests', verbose_name='依頼者')
    title = models.CharField('タイトル', max_length=200)
    description = models.TextField('詳細', blank=True)
    preset_tags = models.ManyToManyField(PresetTag, blank=True, verbose_name='定型タグ')
    required_skills = models.ManyToManyField(Skill, blank=True, related_name='help_requests',
                                             verbose_name='必要なスキル')
    mode = models.CharField('募集方式', max_length=20, choices=Mode.choices, default=Mode.FIRST_COME)
    headcount = models.PositiveSmallIntegerField('募集人数', default=1, help_text='同時に手伝ってもらう人数。2人以上はグループチャットになります。')
    duration_minutes = models.PositiveIntegerField('所要時間(分)', default=15)
    points_cost = models.PositiveIntegerField('依頼ポイント', default=0,
                                              help_text='投稿時に依頼者から預かったポイント。完了時にヘルパーへ渡る')
    points_settled = models.BooleanField('ポイント精算済み', default=False)
    chat_retention_days = models.PositiveSmallIntegerField(
        'チャット履歴の保存期間', choices=ChatRetention.choices, default=ChatRetention.THREE_DAYS,
        help_text='完了・キャンセル後はチャットに送信できなくなります。履歴はこの期間だけ閲覧でき、過ぎると自動で削除されます。')
    status = models.CharField('状況', max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)

    # 正確な位置はマッチ参加者にのみ開示する
    latitude = models.FloatField('緯度')
    longitude = models.FloatField('経度')
    address_detail = models.CharField('詳しい場所', max_length=200, blank=True,
                                      help_text='マッチングした相手にだけ表示されます(例: 〇〇マンション3階)')
    public_latitude = models.FloatField(null=True, blank=True, editable=False)
    public_longitude = models.FloatField(null=True, blank=True, editable=False)
    area_label = models.CharField('エリア', max_length=50, blank=True)

    expires_at = models.DateTimeField('掲載期限', default=default_expiry, db_index=True)
    scheduled_for = models.DateTimeField('手伝ってほしい日時', null=True, blank=True,
                                         help_text='予約掲載のときだけ指定します')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '依頼'

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        self.public_latitude = snap_to_grid(self.latitude)
        self.public_longitude = snap_to_grid(self.longitude)
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status == self.Status.OPEN and self.expires_at > timezone.now()

    @property
    def active_match(self):
        return self.matches.filter(status=Match.Status.IN_PROGRESS).select_related('helper').first()

    @property
    def active_matches(self):
        return self.matches.filter(status=Match.Status.IN_PROGRESS).select_related('helper')

    @property
    def joined_count(self):
        return self.active_matches.count()

    @property
    def slots_left(self):
        return max(0, self.headcount - self.joined_count)

    @property
    def accepting(self):
        """まだ参加を受け付けているか(満員でなく、期限内)。"""
        return (self.status in (self.Status.OPEN, self.Status.IN_PROGRESS)
                and self.slots_left > 0 and self.expires_at > timezone.now())

    @property
    def is_group(self):
        return self.headcount > 1


class Application(models.Model):
    class Status(models.TextChoices):
        APPLIED = 'applied', '応募中'
        ACCEPTED = 'accepted', '承認'
        REJECTED = 'rejected', '見送り'
        WITHDRAWN = 'withdrawn', '辞退'

    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name='applications')
    applicant = models.ForeignKey(User, on_delete=models.CASCADE, related_name='applications')
    message = models.CharField('ひとこと', max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.APPLIED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '応募'
        constraints = [
            models.UniqueConstraint(fields=['help_request', 'applicant'], name='one_application_per_request'),
        ]


class Match(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = 'in_progress', '進行中'
        DONE = 'done', '完了'
        CANCELED = 'canceled', 'キャンセル'

    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name='matches')
    helper = models.ForeignKey(User, on_delete=models.CASCADE, related_name='helps')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    canceled_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    cancel_reason = models.CharField('キャンセル理由', max_length=200, blank=True)
    penalized = models.BooleanField('ペナルティ対象', default=False)
    # 作業時間の実測(タイマー)と申告。精算は effective_minutes で決める
    work_started_at = models.DateTimeField('作業開始', null=True, blank=True)
    work_ended_at = models.DateTimeField('作業終了', null=True, blank=True)
    declared_minutes = models.PositiveIntegerField('申告時間(分)', null=True, blank=True)
    declaration_approved = models.BooleanField('申告の承認', default=False)

    class Meta:
        verbose_name = verbose_name_plural = 'マッチング'
        constraints = [
            models.UniqueConstraint(fields=['help_request', 'helper'], condition=Q(status='in_progress'),
                                    name='one_active_match_per_helper'),
        ]

    def participants(self):
        return {self.helper_id, self.help_request.requester_id}

    def other_user(self, user):
        return self.help_request.requester if user.pk == self.helper_id else self.helper


class Review(models.Model):
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='given_reviews')
    reviewee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_reviews')
    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE)
    rating = models.IntegerField('評価')
    comment = models.TextField('コメント', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = 'レビュー'
        constraints = [
            models.UniqueConstraint(fields=['reviewer', 'help_request'], name='one_review_per_request'),
        ]


class SanctionQuerySet(models.QuerySet):
    def active(self):
        now = timezone.now()
        return self.filter(starts_at__lte=now).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))


class Sanction(models.Model):
    class Level(models.TextChoices):
        WARNING = 'warning', '警告'
        NO_ACCEPT = 'no_accept', 'お手伝いの受付停止'
        NO_ACTIVITY = 'no_activity', '依頼・受付ともに停止'
        BANNED = 'banned', '利用停止(BAN)'

    class Source(models.TextChoices):
        AUTO = 'auto', '自動'
        ADMIN = 'admin', '運営'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sanctions')
    level = models.CharField('内容', max_length=20, choices=Level.choices)
    reason = models.CharField('理由', max_length=200)
    source = models.CharField('付与元', max_length=10, choices=Source.choices, default=Source.AUTO)
    starts_at = models.DateTimeField('開始', default=timezone.now)
    ends_at = models.DateTimeField('終了', null=True, blank=True, help_text='空欄は無期限')
    related_match = models.ForeignKey(Match, null=True, blank=True, on_delete=models.SET_NULL)

    objects = SanctionQuerySet.as_manager()

    class Meta:
        verbose_name = verbose_name_plural = 'ペナルティ'


class Message(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='messages')
    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name='messages',
                                     null=True, blank=True, verbose_name='依頼')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='replies', verbose_name='返信先')
    body = models.TextField('本文', max_length=1000, blank=True)
    image = models.ImageField('写真', upload_to=chat_image_upload_to, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    risk_score = models.FloatField('リスクスコア', default=0)
    risk_labels = models.JSONField('検知ラベル', default=list, blank=True)
    is_hidden = models.BooleanField('非表示', default=False)

    class Meta:
        verbose_name = verbose_name_plural = 'メッセージ'
        ordering = ['created_at']


class MessageRead(models.Model):
    """グループチャット対応の既読記録(誰がどこまで読んだか)。"""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reads', verbose_name='メッセージ')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='message_reads', verbose_name='読んだ人')
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '既読'
        constraints = [
            models.UniqueConstraint(fields=['message', 'user'], name='one_read_per_user'),
        ]


class MessageReaction(models.Model):
    """チャットメッセージへのリアクション(長押しで付ける)。"""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions', verbose_name='メッセージ')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='message_reactions', verbose_name='付けた人')
    emoji = models.CharField('絵文字', max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = 'リアクション'
        constraints = [
            models.UniqueConstraint(fields=['message', 'user', 'emoji'], name='one_reaction_per_user_emoji'),
        ]


class PushSubscription(models.Model):
    """Web Push の購読情報(ホーム画面に追加した PWA 等へのポップアップ通知用)。"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_subscriptions', verbose_name='ユーザー')
    endpoint = models.URLField('エンドポイント', max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = 'プッシュ通知の購読'


class Report(models.Model):
    class Category(models.TextChoices):
        DATING = 'dating', '出会い目的'
        INAPPROPRIATE = 'inappropriate', '不適切な依頼・内容'
        HARASSMENT = 'harassment', '迷惑行為・ハラスメント'
        NO_SHOW = 'no_show', '無断キャンセル・不履行'
        OTHER = 'other', 'その他'

    class Status(models.TextChoices):
        OPEN = 'open', '未対応'
        REVIEWING = 'reviewing', '確認中'
        RESOLVED = 'resolved', '対応済み'
        DISMISSED = 'dismissed', '問題なし'

    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reports_made')
    target_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reports_received',
                                    verbose_name='通報対象')
    help_request = models.ForeignKey(HelpRequest, null=True, blank=True, on_delete=models.SET_NULL)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL)
    category = models.CharField('理由', max_length=20, choices=Category.choices)
    detail = models.TextField('詳しい内容', blank=True)
    status = models.CharField('対応状況', max_length=20, choices=Status.choices, default=Status.OPEN)
    handled_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '通報'


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=30)
    text = models.CharField(max_length=200)
    url = models.CharField(max_length=200, blank=True)
    is_read = models.BooleanField(default=False)
    # メールダイジェストで処理済み(送信・スキップどちらも)
    email_processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = verbose_name_plural = '通知'
        ordering = ['-created_at']


class NotificationSetting(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='notification_setting')
    email_nearby = models.BooleanField('近くの新しい依頼をメールで受け取る', default=False)
    email_activity = models.BooleanField('マッチング・メッセージをメールで受け取る', default=True)
    line_activity = models.BooleanField('マッチング・メッセージをLINEで受け取る', default=True)
    line_nearby = models.BooleanField('近くの新しい依頼をLINEで受け取る', default=False)
    push_activity = models.BooleanField('マッチング・メッセージをプッシュ通知で受け取る', default=True)
    forced_off = models.BooleanField('管理者による通知停止', default=False,
                                     help_text='オンにすると、このユーザーへのメール・LINE・プッシュ通知をすべて止めます(管理者のみ変更可)')

    class Meta:
        verbose_name = verbose_name_plural = '通知設定'

class ActivityBase(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bases')
    label = models.CharField('名前', max_length=30, help_text='例: 自宅付近、職場')
    latitude = models.FloatField('緯度')
    longitude = models.FloatField('経度')
    area_label = models.CharField('エリア', max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '活動拠点'


class StoredFile(models.Model):
    """DatabaseStorage(core/storage.py)が使う、DB上のファイル。"""
    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '保存ファイル'

    def __str__(self):
        return self.name


class LineChatState(models.Model):
    """LINEチャットでの依頼投稿フローの途中状態。"""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='line_chat_state')
    step = models.CharField(max_length=30, blank=True)
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = 'LINEチャット状態'


class PointTransaction(models.Model):
    """ポイント増減の台帳。マイページの残高タップで履歴を表示するために記録する。"""

    class Kind(models.TextChoices):
        STARTER = 'starter', 'スターターポイント'
        ESCROW = 'escrow', '依頼の預かり'
        SETTLE_EXTRA = 'settle_extra', '実績精算の追加支払い'
        SETTLE_REFUND = 'settle_refund', '実績精算の返金'
        EARNED = 'earned', 'お手伝い報酬'
        REVIEW_BONUS = 'review_bonus', 'レビューボーナス'
        REFUND = 'refund', '返金'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='point_transactions',
                             verbose_name='ユーザー')
    amount = models.IntegerField('増減')
    balance_after = models.IntegerField('増減後の残高')
    kind = models.CharField('種別', max_length=20, choices=Kind.choices)
    help_request = models.ForeignKey(HelpRequest, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='point_transactions', verbose_name='依頼')
    counterparty = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', verbose_name='相手')
    description = models.CharField('内容', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = verbose_name_plural = 'ポイント履歴'
        ordering = ['-created_at', '-pk']
