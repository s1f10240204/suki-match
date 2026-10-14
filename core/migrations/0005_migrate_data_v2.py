"""v2へのデータ移行: 状態値の英字化、スキル文字列→スキルタグ、レベル計算、初期データ投入。"""
import re
from math import floor, sqrt

from django.db import migrations

from core.text import normalize_text

STATUS_MAP = {'募集中': 'open', '進行中': 'in_progress', '完了': 'done', 'キャンセル': 'canceled'}

OFFICIAL_SKILLS = [
    '見守り', '子どもの送迎', '買い物', '荷物運び', '話し相手', 'ゴミ出し', '料理', '掃除',
    'スマホ操作', '勉強の手伝い', 'ペットの世話', '庭仕事', '電球交換', '付き添い',
]
PRESET_TAGS = [
    '詳細は現地で相談', '詳細はチャットで', '急ぎです', '15分以内で終わります', '子どもがいます', 'ペットがいます',
]
GRID = 0.0025


def get_or_create_skill(Skill, name, official=False):
    name = name.strip()[:30]
    normalized = normalize_text(name)
    skill = Skill.objects.filter(normalized=normalized).first()
    if skill is None:
        skill = Skill.objects.create(name=name, normalized=normalized, is_official=official)
    elif official and not skill.is_official:
        skill.is_official = True
        skill.save(update_fields=['is_official'])
    return skill


def split_names(text):
    return [s for s in (part.strip() for part in re.split(r'[,、，]', text or '')) if s]


def forwards(apps, schema_editor):
    User = apps.get_model('core', 'User')
    HelpRequest = apps.get_model('core', 'HelpRequest')
    Match = apps.get_model('core', 'Match')
    Skill = apps.get_model('core', 'Skill')
    PresetTag = apps.get_model('core', 'PresetTag')

    for old, new in STATUS_MAP.items():
        HelpRequest.objects.filter(status=old).update(status=new)
        Match.objects.filter(status=old).update(status=new)

    for name in OFFICIAL_SKILLS:
        get_or_create_skill(Skill, name, official=True)
    for order, label in enumerate(PRESET_TAGS):
        PresetTag.objects.get_or_create(label=label, defaults={'order': order})

    for user in User.objects.all():
        for name in split_names(user.skills_text):
            user.skills.add(get_or_create_skill(Skill, name))
        user.level = min(100, floor(sqrt(max(user.points, 0))) + 1)
        user.save(update_fields=['level'])

    for hr in HelpRequest.objects.all():
        for name in split_names(hr.required_skill_text):
            hr.required_skills.add(get_or_create_skill(Skill, name))
        hr.public_latitude = round(round(hr.latitude / GRID) * GRID, 6)
        hr.public_longitude = round(round(hr.longitude / GRID) * GRID, 6)
        hr.save(update_fields=['public_latitude', 'public_longitude'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_schema_v2'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
