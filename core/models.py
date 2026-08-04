from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    points = models.IntegerField(default=0)
    rank = models.CharField(max_length=50, default='ビギナー')
    skills = models.TextField(blank=True)

class HelpRequest(models.Model):
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='requests')
    title = models.CharField(max_length=200)
    description = models.TextField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    status = models.CharField(max_length=20, default='募集中')
    created_at = models.DateTimeField(auto_now_add=True)

class Match(models.Model):
    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name='matches')
    helper = models.ForeignKey(User, on_delete=models.CASCADE, related_name='helps')
    status = models.CharField(max_length=20, default='進行中')
    created_at = models.DateTimeField(auto_now_add=True)

class Review(models.Model):
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='given_reviews')
    reviewee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_reviews')
    help_request = models.ForeignKey(HelpRequest, on_delete=models.CASCADE)
    rating = models.IntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)