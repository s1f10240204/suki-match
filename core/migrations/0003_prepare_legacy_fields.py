"""旧フィールドを退避し、メールの一意制約に備えて空文字をNULLにする。"""
from django.db import migrations, models


def blank_email_to_null(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.filter(email='').update(email=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_helprequest_duration_minutes_and_more'),
    ]

    operations = [
        migrations.RenameField(model_name='user', old_name='skills', new_name='skills_text'),
        migrations.RenameField(model_name='helprequest', old_name='required_skill', new_name='required_skill_text'),
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(blank=True, max_length=254, null=True, verbose_name='メールアドレス'),
        ),
        migrations.RunPython(blank_email_to_null, migrations.RunPython.noop),
    ]
