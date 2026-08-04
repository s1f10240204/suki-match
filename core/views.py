from django.shortcuts import render, get_object_or_404
from .models import HelpRequest

def help_request_list(request):
    help_requests = HelpRequest.objects.all().order_by('-created_at')
    return render(request, 'core/help_request_list.html', {'help_requests': help_requests})

def help_request_detail(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    return render(request, 'core/help_request_detail.html', {'help_request': help_request})