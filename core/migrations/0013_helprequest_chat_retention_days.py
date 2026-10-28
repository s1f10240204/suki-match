from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_linechatstate'),
    ]

    operations = [
        migrations.AddField(
            model_name='helprequest',
            name='chat_retention_days',
            field=models.PositiveSmallIntegerField(
                choices=[(0, '完了後すぐ削除'), (1, '完了後1日'), (3, '完了後3日'), (7, '完了後7日(最長)')],
                default=3,
                help_text='完了・キャンセル後はチャットに送信できなくなります。履歴はこの期間だけ閲覧でき、過ぎると自動で削除されます。',
                verbose_name='チャット履歴の保存期間'),
        ),
    ]
