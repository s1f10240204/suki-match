from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.help_request_list, name='help_request_list'),
    path('new/', views.help_request_create, name='help_request_create'),
    path('<int:pk>/', views.help_request_detail, name='help_request_detail'),
    path('<int:pk>/accept/', views.accept_help_request, name='accept_help_request'),
    path('<int:pk>/apply/', views.apply_help_request, name='apply_help_request'),
    path('<int:pk>/complete/', views.complete_help_request, name='complete_help_request'),
    path('<int:pk>/cancel/', views.cancel_help_request, name='cancel_help_request'),
    path('<int:pk>/review/', views.submit_review, name='submit_review'),
    path('applications/<int:pk>/approve/', views.approve_application, name='approve_application'),
    path('applications/<int:pk>/withdraw/', views.withdraw_application, name='withdraw_application'),
    path('matches/<int:pk>/chat/', views.chat, name='chat'),
    path('messages/', views.message_list, name='message_list'),
    path('photos/<int:pk>/', views.user_photo, name='user_photo'),
    path('line/webhook/', views.line_webhook, name='line_webhook'),
    path('me/', views.profile, name='profile'),
    path('me/edit/', views.profile_edit, name='profile_edit'),
    path('me/settings/', views.account_settings, name='account_settings'),
    path('me/notifications/', views.notifications, name='notifications'),
    path('me/bases/', views.add_base, name='add_base'),
    path('me/bases/<int:pk>/delete/', views.delete_base, name='delete_base'),
    path('users/<str:username>/', views.public_profile, name='public_profile'),
    path('users/<str:username>/report/', views.report_user, name='report_user'),
    path('stats/', views.stats, name='stats'),
    path('manifest.webmanifest', views.pwa_manifest, name='pwa_manifest'),
    path('sw.js', views.service_worker, name='service_worker'),
    path('api/skills/', views.skill_search, name='skill_search'),
    path('healthz/', views.healthz, name='healthz'),
    path('tasks/run/', views.run_tasks, name='run_tasks'),
]
