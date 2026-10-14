"""スキルタグ: 検索候補、ユーザー定義タグの追加、類似タグの抽出。"""
from datetime import timedelta
from difflib import SequenceMatcher

from django.db.models import Count, Q
from django.utils import timezone

from .models import Skill
from .text import normalize_text

MAX_NAME_LENGTH = 30
MAX_NEW_SKILLS_PER_DAY = 5
SIMILARITY_THRESHOLD = 0.75


class SkillLimitError(Exception):
    pass


def search(query, limit=10):
    """候補表示用。統合済みの別名は代表タグに置き換え、公式・利用数の多い順に返す。"""
    qs = Skill.objects.filter(canonical__isnull=True)
    key = normalize_text(query)
    if key:
        alias_targets = Skill.objects.filter(normalized__contains=key, canonical__isnull=False).values('canonical')
        qs = qs.filter(Q(normalized__contains=key) | Q(pk__in=alias_targets))
    qs = qs.annotate(uses=Count('users', distinct=True) + Count('help_requests', distinct=True))
    return list(qs.order_by('-is_official', '-uses', 'name')[:limit])


def _clean_names(names):
    cleaned, seen = [], set()
    for raw in names:
        name = raw.strip().replace(',', '')[:MAX_NAME_LENGTH]
        key = normalize_text(name)
        if key and key not in seen:
            seen.add(key)
            cleaned.append((name, key))
    return cleaned


def check_new_skill_limit(names, user):
    """新規タグの乱造防止。1ユーザー1日 MAX_NEW_SKILLS_PER_DAY 件まで。"""
    new = [key for _, key in _clean_names(names) if not Skill.objects.filter(normalized=key).exists()]
    since = timezone.now() - timedelta(days=1)
    created = Skill.objects.filter(created_by=user, created_at__gte=since).count()
    if new and created + len(new) > MAX_NEW_SKILLS_PER_DAY:
        raise SkillLimitError(f'新しいスキルの追加は1日{MAX_NEW_SKILLS_PER_DAY}件までです。')


def resolve_names(names, user=None):
    """名前のリスト → Skill のリスト。既存(表記ゆれ含む)があれば再利用し、なければ新規登録する。"""
    skills = []
    for name, key in _clean_names(names):
        skill = Skill.objects.filter(normalized=key).select_related('canonical').first()
        if skill is None:
            skill = Skill.objects.create(name=name, created_by=user)
        skills.append(skill.resolved)
    return list(dict.fromkeys(skills))


def similar_pairs(threshold=SIMILARITY_THRESHOLD):
    """表記の似ているタグの組(統合候補)。統合は管理画面で運営が判断する。"""
    skills = list(Skill.objects.filter(canonical__isnull=True))
    pairs = []
    for i, a in enumerate(skills):
        for b in skills[i + 1:]:
            ratio = SequenceMatcher(None, a.normalized, b.normalized).ratio()
            contained = min(len(a.normalized), len(b.normalized)) >= 2 and (
                a.normalized in b.normalized or b.normalized in a.normalized)
            if ratio >= threshold or contained:
                pairs.append((a, b, round(max(ratio, 0.8 if contained else 0), 2)))
    return sorted(pairs, key=lambda p: -p[2])


def merge(target, others):
    """others を target に統合する(別名として残し、ユーザー・依頼の紐付けは target へ移す)。"""
    target = target.resolved
    for skill in others:
        if skill.pk == target.pk:
            continue
        for user in skill.users.all():
            user.skills.remove(skill)
            user.skills.add(target)
        for hr in skill.help_requests.all():
            hr.required_skills.remove(skill)
            hr.required_skills.add(target)
        Skill.objects.filter(canonical=skill).update(canonical=target)
        skill.canonical = target
        skill.save(update_fields=['canonical'])
