from django.shortcuts import render
from .models import HelpRequest

def help_request_list(request):
    help_requests = HelpRequest.objects.all().order_by('-created_at')
    return render(request, 'core/help_request_list.html', {'help_requests': help_requests})