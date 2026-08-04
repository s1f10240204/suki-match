from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('', views.help_request_list, name='help_request_list'),
    path('<int:pk>/', views.help_request_detail, name='help_request_detail'),
]