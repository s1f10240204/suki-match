"""招待リンクの紹介者名をセッションに保持する。"""


class ReferralMiddleware:
    """?ref=<ユーザー名> 付きで開かれたら、登録まで紹介者名をセッションに残す。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ref = (request.GET.get('ref') or '').strip()
        if ref and len(ref) <= 150:
            request.session['ref_username'] = ref
        return self.get_response(request)
