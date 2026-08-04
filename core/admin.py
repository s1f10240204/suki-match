from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, HelpRequest, Match, Review

admin.site.register(User, UserAdmin)
admin.site.register(HelpRequest)
admin.site.register(Match)
admin.site.register(Review)