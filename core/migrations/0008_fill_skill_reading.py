"""既存スキルの読み(ひらがな)を埋める。"""
from django.db import migrations

from core.text import reading


def fill(apps, schema_editor):
    Skill = apps.get_model('core', 'Skill')
    for skill in Skill.objects.all():
        skill.reading = reading(skill.name)[:60]
        skill.save(update_fields=['reading'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_photo_review_and_skill_reading'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
