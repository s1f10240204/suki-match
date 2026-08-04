from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import HelpRequest, Match

def help_request_list(request):
    help_requests = HelpRequest.objects.filter(status='募集中').order_by('-created_at')
    return render(request, 'core/help_request_list.html', {'help_requests': help_requests})

def help_request_detail(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    return render(request, 'core/help_request_detail.html', {'help_request': help_request})

@login_required
def accept_help_request(request, pk):
    help_request = get_object_or_404(HelpRequest, pk=pk)
    if request.method == 'POST' and help_request.status == '募集中':
        Match.objects.create(
            help_request=help_request,
            helper=request.user,
            status='進行中'
        )
        help_request.status = '進行中'
        help_request.save()
        return redirect('core:help_request_detail', pk=pk)
    return redirect('core:help_request_detail', pk=pk)