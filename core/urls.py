from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.help_request_list, name='help_request_list'),
    path('new/', views.help_request_create, name='help_request_create'),
    path('profile/', views.profile, name='profile'),
    path('<int:pk>/', views.help_request_detail, name='help_request_detail'),
    path('<int:pk>/accept/', views.accept_help_request, name='accept_help_request'),
    path('<int:pk>/complete/', views.complete_help_request, name='complete_help_request'),
    path('<int:pk>/cancel/', views.cancel_help_request, name='cancel_help_request'),
    path('<int:pk>/review/', views.submit_review, name='submit_review'),
]
