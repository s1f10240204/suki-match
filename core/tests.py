from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import HelpRequest, Match, Review, User
from .routing import websocket_urlpatterns
from .services import distance_m, is_suspicious, nearby_helpers, skill_matches

# 東京駅付近を基準点にする
LAT, LNG = 35.6812, 139.7671
# 緯度0.001度 ≒ 111m
NEAR = (LAT + 0.002, LNG)   # 約222m
FAR = (LAT + 0.01, LNG)     # 約1.1km


def make_user(name, loc=(LAT, LNG), skills=''):
    return User.objects.create_user(name, password='pw-12345', latitude=loc[0], longitude=loc[1], skills=skills)


def make_request(requester, skill='', loc=(LAT, LNG)):
    return HelpRequest.objects.create(
        requester=requester, title='見守り', description='15分だけ', required_skill=skill,
        latitude=loc[0], longitude=loc[1])


class ServiceTests(TestCase):
    def test_distance(self):
        self.assertAlmostEqual(distance_m(LAT, LNG, LAT + 0.001, LNG), 111, delta=2)

    def test_skill_matches(self):
        user = make_user('a', skills='見守り, 買い物')
        self.assertTrue(skill_matches(user, ''))
        self.assertTrue(skill_matches(user, '買い物'))
        self.assertFalse(skill_matches(user, '荷物運び'))

    def test_nearby_helpers_filters_radius_skill_and_requester(self):
        requester = make_user('req')
        near = make_user('near', NEAR, '見守り')
        make_user('far', FAR, '見守り')
        make_user('noskill', NEAR, '買い物')
        hr = make_request(requester, skill='見守り')
        self.assertEqual(nearby_helpers(hr), [near])

    def test_anomaly_detection(self):
        helper, requester = make_user('h'), make_user('r')
        for i in range(5):
            Match.objects.create(help_request=make_request(requester), helper=helper,
                                 status=Match.CANCELED if i < 3 else Match.DONE)
        self.assertTrue(is_suspicious(helper))
        self.assertFalse(is_suspicious(requester))

    def test_rank_up(self):
        user = make_user('u')
        user.add_points(30)
        self.assertEqual(user.rank, 'レギュラー')


class FlowTests(TestCase):
    def setUp(self):
        self.requester = make_user('req')
        self.helper = make_user('helper')
        self.hr = make_request(self.requester)

    def test_list_is_public_and_filters_by_radius(self):
        far = make_request(self.requester, loc=FAR)
        self.assertContains(self.client.get(reverse('core:help_request_list')), '見守り')
        self.client.force_login(self.helper)
        response = self.client.get(reverse('core:help_request_list'))
        self.assertIn(self.hr, response.context['help_requests'])
        self.assertNotIn(far, response.context['help_requests'])

    def test_accept_requires_login(self):
        response = self.client.post(reverse('core:accept_help_request', args=[self.hr.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Match.objects.count(), 0)

    def test_first_come_first_served(self):
        other = make_user('other')
        url = reverse('core:accept_help_request', args=[self.hr.pk])
        self.client.force_login(self.helper)
        self.client.post(url)
        self.client.force_login(other)
        self.client.post(url)
        self.assertEqual(Match.objects.count(), 1)

    def test_cannot_accept_own_request(self):
        self.client.force_login(self.requester)
        self.client.post(reverse('core:accept_help_request', args=[self.hr.pk]))
        self.assertEqual(Match.objects.count(), 0)

    def test_complete_awards_points_and_review_once(self):
        self.client.force_login(self.helper)
        self.client.post(reverse('core:accept_help_request', args=[self.hr.pk]))
        self.client.force_login(self.requester)
        self.client.post(reverse('core:complete_help_request', args=[self.hr.pk]))
        self.helper.refresh_from_db()
        self.assertEqual(self.helper.points, 10)

        review_url = reverse('core:submit_review', args=[self.hr.pk])
        self.client.post(review_url, {'rating': 5, 'comment': 'ありがとう'})
        self.client.post(review_url, {'rating': 1})
        self.assertEqual(Review.objects.count(), 1)
        self.helper.refresh_from_db()
        self.assertEqual(self.helper.points, 12)

    def test_helper_cancel_reopens_request(self):
        self.client.force_login(self.helper)
        self.client.post(reverse('core:accept_help_request', args=[self.hr.pk]))
        self.client.post(reverse('core:cancel_help_request', args=[self.hr.pk]))
        self.hr.refresh_from_db()
        self.assertEqual(self.hr.status, HelpRequest.OPEN)

    def test_create_request(self):
        self.client.force_login(self.requester)
        response = self.client.post(reverse('core:help_request_create'), {
            'title': '荷物', 'description': '重い', 'required_skill': '',
            'duration_minutes': 15, 'latitude': LAT, 'longitude': LNG})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(HelpRequest.objects.filter(title='荷物').exists())

    def test_signup(self):
        response = self.client.post(reverse('signup'), {
            'username': 'new', 'password1': 'complex-pass-99', 'password2': 'complex-pass-99', 'skills': '見守り'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username='new').exists())


class WebSocketTests(TransactionTestCase):
    async def test_receives_notification_and_saves_location(self):
        from asgiref.sync import sync_to_async
        from channels.layers import get_channel_layer

        user = await sync_to_async(make_user)('ws', (0, 0))
        app = URLRouter(websocket_urlpatterns)
        communicator = WebsocketCommunicator(app, '/ws/notifications/')
        communicator.scope['user'] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await get_channel_layer().group_send(
            f'user_{user.pk}', {'type': 'notify', 'message': 'hello', 'url': '/1/'})
        self.assertEqual(await communicator.receive_json_from(), {'message': 'hello', 'url': '/1/'})

        await communicator.send_json_to({'type': 'location', 'lat': 35.0, 'lng': 139.0})
        await communicator.disconnect()
        await user.arefresh_from_db()
        self.assertEqual((user.latitude, user.longitude), (35.0, 139.0))

    async def test_anonymous_rejected(self):
        from django.contrib.auth.models import AnonymousUser
        communicator = WebsocketCommunicator(URLRouter(websocket_urlpatterns), '/ws/notifications/')
        communicator.scope['user'] = AnonymousUser()
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
