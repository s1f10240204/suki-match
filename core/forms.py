import re
from datetime import timedelta

from django import forms
from django.urls import reverse_lazy
from django.utils import timezone

from . import skills as skill_service
from .models import ActivityBase, Application, HelpRequest, NotificationSetting, PresetTag, Report, User

LISTING_CHOICES = [(15, '15分'), (30, '30分'), (60, '1時間'), (120, '2時間'), (180, '3時間')]
SCHEDULE_MIN_LEAD = timedelta(minutes=30)
SCHEDULE_MAX_LEAD = timedelta(days=7)
LOCATION_REQUIRED = '場所を指定してください(「現在地を取得」または地図をタップ)。'


class SkillNamesField(forms.CharField):
    """スキル名のリスト。JSで候補選択つきのタグ入力に置き換わる(JSなしではカンマ区切り入力)。"""

    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('widget', forms.TextInput(attrs={
            'data-skill-picker': reverse_lazy('core:skill_search'),
            'placeholder': 'スキル名を入力して候補から選択(カンマ区切り)', 'autocomplete': 'off'}))
        super().__init__(**kwargs)

    def to_python(self, value):
        return [s for s in (p.strip() for p in re.split(r'[,、，]', super().to_python(value) or '')) if s]


def validate_skill_limit(names, user):
    try:
        skill_service.check_new_skill_limit(names, user)
    except skill_service.SkillLimitError as e:
        raise forms.ValidationError(str(e))
    return names


# 緯度経度は hidden。画面の「現在地を取得」ボタンと地図で入力する
class ProfileForm(forms.ModelForm):
    skill_names = SkillNamesField(label='できること', help_text='候補にないスキルは新しく追加できます')
    latitude = forms.FloatField(required=False, widget=forms.HiddenInput)
    longitude = forms.FloatField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = User
        fields = ('avatar', 'motivation', 'bio')
        widgets = {'bio': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['skill_names'].initial = ', '.join(s.name for s in self.instance.skills.all())
        self.fields['latitude'].initial = self.instance.latitude
        self.fields['longitude'].initial = self.instance.longitude

    def clean_skill_names(self):
        return validate_skill_limit(self.cleaned_data['skill_names'], self.instance)

    def save(self, commit=True):
        user = super().save(commit)
        user.skills.set(skill_service.resolve_names(self.cleaned_data['skill_names'], user))
        return user


class AccountForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('username',)
        labels = {'username': 'ユーザー名'}


class HelpRequestForm(forms.ModelForm):
    listing_minutes = forms.TypedChoiceField(label='掲載時間', choices=LISTING_CHOICES, coerce=int, initial=60,
                                             help_text='この時間を過ぎると自動で締め切ります')
    skill_names = SkillNamesField(label='必要なスキル',
                                  help_text='指定すると、そのスキルを持つ近くの人にだけ通知します(空欄なら誰でも)')

    class Meta:
        model = HelpRequest
        fields = ('title', 'mode', 'preset_tags', 'description', 'duration_minutes',
                  'scheduled_for', 'address_detail', 'latitude', 'longitude')
        widgets = {
            'mode': forms.RadioSelect,
            'preset_tags': forms.CheckboxSelectMultiple,
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': '定型タグだけでもOKです'}),
            'scheduled_for': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'latitude': forms.HiddenInput,
            'longitude': forms.HiddenInput,
        }
        error_messages = {
            'latitude': {'required': LOCATION_REQUIRED},
            'longitude': {'required': LOCATION_REQUIRED},
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['preset_tags'].queryset = PresetTag.objects.all()
        self.fields['scheduled_for'].label = '手伝ってほしい日時(予約掲載)'
        self.fields['scheduled_for'].help_text = '今すぐではなく後日の依頼のときだけ指定します'

    def clean_duration_minutes(self):
        minutes = self.cleaned_data['duration_minutes']
        if not 5 <= minutes <= 60:
            raise forms.ValidationError('5〜60分の範囲で指定してください。')
        return minutes

    def clean_scheduled_for(self):
        when = self.cleaned_data.get('scheduled_for')
        if when is not None:
            now = timezone.now()
            if when < now + SCHEDULE_MIN_LEAD:
                raise forms.ValidationError('予約は30分以上先の日時を指定してください。')
            if when > now + SCHEDULE_MAX_LEAD:
                raise forms.ValidationError('予約は7日以内の日時を指定してください。')
        return when

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('description') and not cleaned.get('preset_tags'):
            self.add_error('description', '詳細を書くか、定型タグを1つ以上選んでください。')
        return cleaned

    def clean_skill_names(self):
        return validate_skill_limit(self.cleaned_data['skill_names'], self.user)

    def save(self, commit=True):
        """依頼者・掲載期限・スキルを設定して保存する。"""
        help_request = super().save(commit=False)
        help_request.requester = self.user
        help_request.expires_at = (help_request.scheduled_for
                                   or timezone.now() + timedelta(minutes=self.cleaned_data['listing_minutes']))
        help_request.save()
        self.save_m2m()
        help_request.required_skills.set(skill_service.resolve_names(self.cleaned_data['skill_names'], self.user))
        return help_request


class ApplicationForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ('message',)
        widgets = {'message': forms.TextInput(attrs={'placeholder': '例: 近所に住んでいます。子育て経験があります'})}


class CancelForm(forms.Form):
    reason = forms.CharField(label='キャンセル理由(任意)', max_length=200, required=False)


class ReviewForm(forms.Form):
    rating = forms.TypedChoiceField(label='評価', coerce=int, widget=forms.RadioSelect,
                                    choices=[(i, '★' * i) for i in range(5, 0, -1)])
    comment = forms.CharField(label='コメント', required=False, widget=forms.Textarea(attrs={'rows': 3}))


class ReportForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ('category', 'detail')
        widgets = {'category': forms.RadioSelect, 'detail': forms.Textarea(attrs={'rows': 4})}


class NotificationSettingForm(forms.ModelForm):
    class Meta:
        model = NotificationSetting
        fields = ('email_nearby', 'email_activity', 'quiet_start', 'quiet_end')
        widgets = {
            'quiet_start': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'quiet_end': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        }


class ActivityBaseForm(forms.ModelForm):
    class Meta:
        model = ActivityBase
        fields = ('label', 'latitude', 'longitude')
        widgets = {'latitude': forms.HiddenInput, 'longitude': forms.HiddenInput}
        error_messages = {
            'latitude': {'required': LOCATION_REQUIRED},
            'longitude': {'required': LOCATION_REQUIRED},
        }


class MessageForm(forms.Form):
    body = forms.CharField(label='メッセージ', max_length=1000, widget=forms.TextInput(
        attrs={'autocomplete': 'off', 'placeholder': 'メッセージを入力', 'aria-label': 'メッセージ'}))
