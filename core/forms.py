from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import HelpRequest, Review, User


class SignUpForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'skills')


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('skills', 'latitude', 'longitude')
        widgets = {
            'latitude': forms.NumberInput(attrs={'step': 'any'}),
            'longitude': forms.NumberInput(attrs={'step': 'any'}),
        }


class HelpRequestForm(forms.ModelForm):
    class Meta:
        model = HelpRequest
        fields = ('title', 'description', 'required_skill', 'duration_minutes', 'latitude', 'longitude')
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'latitude': forms.NumberInput(attrs={'step': 'any'}),
            'longitude': forms.NumberInput(attrs={'step': 'any'}),
        }

    def clean_duration_minutes(self):
        minutes = self.cleaned_data['duration_minutes']
        if not 5 <= minutes <= 60:
            raise forms.ValidationError('5〜60分の範囲で指定してください。')
        return minutes


class ReviewForm(forms.ModelForm):
    rating = forms.IntegerField(min_value=1, max_value=5, label='評価(1〜5)')

    class Meta:
        model = Review
        fields = ('rating', 'comment')
        widgets = {'comment': forms.Textarea(attrs={'rows': 3})}
