"""スキルタグ: 検索候補、ユーザー定義タグの追加、類似タグの抽出。

類似度は「文字2-gramのJaccard係数」と「正規化レーベンシュタイン距離」を、
表記(正規化済み)と読み(ひらがな)の両方で計算して最大値をとる。
短い日本語のタグでは、意味ベクトル(Embedding)より軽く、表記ゆれ・誤字・読みの一致に強い。
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Skill
from .text import normalize_text, reading

MAX_NAME_LENGTH = 30
MAX_NEW_SKILLS_PER_DAY = 5
SIMILARITY_THRESHOLD = 0.75   # 統合候補として運営に出す下限
SUGGEST_THRESHOLD = 0.5       # 入力中に「似ているタグ」として出す下限
CONTAINMENT_SCORE = 0.8       # 片方がもう片方を含む(例: 見守り ⊂ 子どもの見守り)


def bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def jaccard(a, b):
    x, y = bigrams(a), bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def levenshtein_ratio(a, b):
    """1 - 編集距離 / 長い方の文字数(0〜1)。"""
    if not a or not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return 1 - previous[-1] / max(len(a), len(b))


def _score(a, b):
    contained = min(len(a), len(b)) >= 2 and (a in b or b in a)
    return max(jaccard(a, b), levenshtein_ratio(a, b), CONTAINMENT_SCORE if contained else 0.0)


def similarity(a_norm, a_read, b_norm, b_read):
    """表記と読みの両方で比べた類似度(0〜1)。"""
    return round(max(_score(a_norm, b_norm), _score(a_read, b_read) * 0.95 if a_read and b_read else 0.0), 2)


class SkillLimitError(Exception):
    pass


def search(query, limit=10):
    """候補表示用。表記か読みに入力を含むタグを、公式・利用数の多い順に返す。
    足りなければ類似度の高いタグを「似ているタグ」(is_similar=True)として追加する。
    統合済みの別名は代表タグに置き換える。
    """
    canonical = Skill.objects.filter(canonical__isnull=True).annotate(
        uses=Count('users', distinct=True) + Count('help_requests', distinct=True))
    key, key_reading = normalize_text(query), reading(query)
    if not key:
        results = list(canonical.order_by('-is_official', '-uses', 'name')[:limit])
    else:
        match = Q(normalized__contains=key) | Q(reading__contains=key_reading)
        alias_targets = Skill.objects.filter(match, canonical__isnull=False).values('canonical')
        results = list(canonical.filter(match | Q(pk__in=alias_targets)).order_by('-is_official', '-uses', 'name')[:limit])
        if len(results) < limit:
            found = {s.pk for s in results}
            scored = [(similarity(key, key_reading, s.normalized, s.reading), s)
                      for s in canonical if s.pk not in found]
            for score, skill in sorted(scored, key=lambda p: -p[0])[:limit - len(results)]:
                if score >= SUGGEST_THRESHOLD:
                    skill.is_similar = True
                    results.append(skill)
    for skill in results:
        skill.is_similar = getattr(skill, 'is_similar', False)
    return results


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
            score = similarity(a.normalized, a.reading, b.normalized, b.reading)
            if score >= threshold:
                pairs.append((a, b, score))
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
