from datetime import time as dtime
from datetime import timedelta
from io import StringIO
from unittest import mock

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import geocoding, moderation, services, skills
from .models import (ActivityBase, Application, HelpRequest, Match, Message, Notification, PresetTag, Report,
                     Review, Sanction, Skill, User, level_for_points, rank_for_level)
from .routing import websocket_urlpatterns
from .text import normalize_text

# 東京駅付近を基準点にする。緯度0.001度 ≒ 111m
LAT, LNG = 35.6812, 139.7671
NEAR = (LAT + 0.002, LNG)   # 約222m
FAR = (LAT + 0.01, LNG)     # 約1.1km


def make_user(name, loc=(LAT, LNG), skills_=(), **extra):
    user = User.objects.create_user(name, email=f'{name}@example.com', password='pw-12345',
                                    latitude=loc[0] if loc else None, longitude=loc[1] if loc else None,
                                    location_updated_at=timezone.now() if loc else None, **extra)
    if skills_:
        user.skills.set(skills.resolve_names(skills_))
    return user


def make_request(requester, loc=(LAT, LNG), skill_names=(), **extra):
    extra.setdefault('description', '15分だけ')
    hr = HelpRequest.objects.create(requester=requester, title='見守り', latitude=loc[0], longitude=loc[1],
                                    address_detail='〇〇マンション3階', **extra)
    if skill_names:
        hr.required_skills.set(skills.resolve_names(skill_names))
    return hr


@override_settings(GEOCODER_ENABLED=False, MODERATION_AUTO_HIDE=False)
class BaseTestCase(TestCase):
    def setUp(self):
        cache.clear()


class PureFunctionTests(BaseTestCase):
    def test_distance_and_label(self):
        self.assertAlmostEqual(services.distance_m(LAT, LNG, LAT + 0.001, LNG), 111, delta=2)
        self.assertEqual(services.distance_label(150), '200m以内')
        self.assertEqual(services.distance_label(480), '500m以内')
        self.assertEqual(services.distance_label(5000), '1km以上')

    def test_level_and_rank(self):
        self.assertEqual(level_for_points(0), 1)
        self.assertEqual(rank_for_level(level_for_points(25)), 'レギュラー')
        self.assertEqual(rank_for_level(level_for_points(100)), 'アクティブ')
        self.assertEqual(rank_for_level(level_for_points(225)), 'ベテラン')
        self.assertEqual(level_for_points(10 ** 6), 100)

    def test_normalize_text(self):
        self.assertEqual(normalize_text('ミマモリ'), normalize_text('みまもり'))
        self.assertEqual(normalize_text('ｽﾏﾎ 操作'), normalize_text('スマホ操作'))
        self.assertEqual(normalize_text('ＡＢＣ'), 'abc')

    def test_public_coordinates_are_rounded(self):
        hr = make_request(make_user('r'), loc=(35.68123, 139.76712))
        self.assertNotEqual((hr.public_latitude, hr.public_longitude), (hr.latitude, hr.longitude))
        self.assertLess(services.distance_m(hr.latitude, hr.longitude, hr.public_latitude, hr.public_longitude), 250)

    def test_moderation_rules(self):
        self.assertIn('contact_exchange', moderation.rule_score('LINE ID教えてください')[1])
        self.assertIn('contact_exchange', moderation.rule_score('090-1234-5678に電話して')[1])
        self.assertIn('dating', moderation.rule_score('今度デートしませんか')[1])
        self.assertEqual(moderation.rule_score('15分ほどで伺います。よろしくお願いします'), (0.0, []))

    def test_geocoding_builds_area_label(self):
        responses = {
            geocoding.MUNI_URL: 'GSI.MUNI_ARRAY["13117"] = \'13,東京都,13117,北区\';',
            'reverse': '{"results": {"muniCd": "13117", "lv01Nm": "赤羽一丁目"}}',
        }
        fetch = lambda url: responses[geocoding.MUNI_URL if url == geocoding.MUNI_URL else 'reverse']
        with override_settings(GEOCODER_ENABLED=True), mock.patch.object(geocoding, '_fetch', side_effect=fetch):
            self.assertEqual(geocoding.area_label_for(35.78, 139.72), '東京都北区赤羽')
        with override_settings(GEOCODER_ENABLED=True), mock.patch.object(geocoding, '_fetch', side_effect=OSError):
            self.assertEqual(geocoding.area_label_for(35.78, 139.72), '')


class SkillTests(BaseTestCase):
    def test_resolve_reuses_existing_spelling_variants(self):
        first = skills.resolve_names(['スマホ操作'])[0]
        self.assertEqual(skills.resolve_names(['ｽﾏﾎ操作', 'すまほ操作'])[0], first)
        self.assertEqual(Skill.objects.filter(name__contains='操作').count(), 1)

    def test_new_skill_limit(self):
        user = make_user('u')
        skills.resolve_names([f'特技{i}' for i in range(skills.MAX_NEW_SKILLS_PER_DAY)], user)
        with self.assertRaises(skills.SkillLimitError):
            skills.check_new_skill_limit(['さらに新しい特技'], user)
        skills.check_new_skill_limit(['特技1'], user)  # 既存タグの選択は制限しない

    def test_merge_and_similarity(self):
        user = make_user('u', skills_=['子守り'])
        target, alias = Skill.objects.get(name='見守り'), Skill.objects.get(name='子守り')  # 見守りは初期データ
        Skill.objects.create(name='子どもの見守り')
        pairs = [{a.name, b.name} for a, b, _ in skills.similar_pairs()]
        self.assertIn({'見守り', '子どもの見守り'}, pairs)

        skills.merge(target, [alias])
        self.assertEqual(list(user.skills.all()), [target])
        self.assertEqual(skills.resolve_names(['子守り']), [target])
        self.assertNotIn(alias, skills.search('守り'))

    def test_skill_api(self):
        self.client.force_login(make_user('u'))
        self.assertTrue(Skill.objects.get(name='スマホ操作').is_official)  # 初期データ
        response = self.client.get(reverse('core:skill_search'), {'q': 'ｽﾏﾎ'})
        self.assertEqual(response.json()['results'][0], {'name': 'スマホ操作', 'official': True})


class MatchingTests(BaseTestCase):
    def test_nearby_helpers(self):
        requester = make_user('req')
        near = make_user('near', NEAR, ['見守り'])
        make_user('far', FAR, ['見守り'])
        make_user('noskill', NEAR, ['買い物'])
        stale = make_user('stale', NEAR, ['見守り'])
        User.objects.filter(pk=stale.pk).update(location_updated_at=timezone.now() - timedelta(days=2))
        based = make_user('based', None, ['見守り'])
        ActivityBase.objects.create(user=based, label='自宅', latitude=NEAR[0], longitude=NEAR[1])
        banned = make_user('banned', NEAR, ['見守り'])
        Sanction.objects.create(user=banned, level=Sanction.Level.NO_ACCEPT, reason='test',
                                ends_at=timezone.now() + timedelta(days=1))
        hr = make_request(requester, skill_names=['見守り'])
        self.assertEqual(services.nearby_helpers(hr), [near, based])

    def test_list_filters_by_radius_and_hides_expired(self):
        requester = make_user('req')
        near_hr = make_request(requester)
        far_hr = make_request(requester, loc=FAR)
        expired = make_request(requester, expires_at=timezone.now() - timedelta(minutes=1))
        self.client.force_login(make_user('helper'))
        listed = self.client.get(reverse('core:help_request_list')).context['help_requests']
        self.assertEqual(listed, [near_hr])
        listed_all = self.client.get(reverse('core:help_request_list'), {'all': '1'}).context['help_requests']
        self.assertIn(far_hr, listed_all)
        expired.refresh_from_db()
        self.assertEqual(expired.status, HelpRequest.Status.EXPIRED)

    def test_scheduled_tab(self):
        requester = make_user('req')
        later = timezone.now() + timedelta(days=1)
        scheduled = make_request(requester, scheduled_for=later, expires_at=later)
        make_request(requester)
        self.client.force_login(make_user('helper'))
        listed = self.client.get(reverse('core:help_request_list'), {'tab': 'scheduled'}).context['help_requests']
        self.assertEqual(listed, [scheduled])

    def test_first_come_first_served(self):
        hr = make_request(make_user('req'))
        url = reverse('core:accept_help_request', args=[hr.pk])
        self.client.force_login(make_user('first'))
        self.client.post(url)
        self.client.force_login(make_user('second'))
        self.client.post(url)
        self.assertEqual(Match.objects.get().helper.username, 'first')

    def test_cannot_accept_own_or_expired(self):
        requester = make_user('req')
        hr = make_request(requester)
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, requester)
        old = make_request(requester, expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(old, make_user('helper'))

    def test_approval_flow(self):
        requester = make_user('req')
        hr = make_request(requester, mode=HelpRequest.Mode.APPROVAL)
        a, b = make_user('a'), make_user('b')
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, a)

        self.client.force_login(a)
        self.client.post(reverse('core:apply_help_request', args=[hr.pk]), {'message': '近所です'})
        services.apply(hr, b)
        self.assertEqual(Match.objects.count(), 0)
        self.assertTrue(Notification.objects.filter(user=requester, kind='applied').exists())

        app_a = Application.objects.get(applicant=a)
        self.client.force_login(requester)
        self.client.post(reverse('core:approve_application', args=[app_a.pk]))
        hr.refresh_from_db()
        self.assertEqual(hr.status, HelpRequest.Status.IN_PROGRESS)
        self.assertEqual(Match.objects.get().helper, a)
        self.assertEqual(Application.objects.get(applicant=b).status, Application.Status.REJECTED)
        self.assertTrue(Notification.objects.filter(user=b, kind='rejected').exists())

    def test_only_requester_can_approve(self):
        hr = make_request(make_user('req'), mode=HelpRequest.Mode.APPROVAL)
        app = services.apply(hr, make_user('a'))
        with self.assertRaises(services.ServiceError):
            services.approve(app, make_user('stranger'))

    def test_complete_awards_points_and_review_once(self):
        requester, helper = make_user('req'), make_user('helper')
        hr = make_request(requester)
        services.accept_first_come(hr, helper)
        self.client.force_login(requester)
        self.client.post(reverse('core:complete_help_request', args=[hr.pk]))
        helper.refresh_from_db()
        self.assertEqual((helper.points, helper.level), (10, 4))

        url = reverse('core:submit_review', args=[hr.pk])
        self.client.post(url, {'rating': 5, 'comment': 'ありがとう'})
        self.client.post(url, {'rating': 1})
        self.assertEqual(Review.objects.count(), 1)
        helper.refresh_from_db()
        self.assertEqual(helper.points, 12)


class CancelPenaltyTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.requester, self.helper = make_user('req'), make_user('helper')

    def _matched_request(self, minutes_ago=0):
        hr = make_request(self.requester)
        match = services.accept_first_come(hr, self.helper)
        Match.objects.filter(pk=match.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return hr

    def test_cancel_within_grace_has_no_penalty_and_reopens(self):
        hr = self._matched_request()
        self.assertIsNone(services.cancel(hr, self.helper))
        hr.refresh_from_db()
        self.assertEqual(hr.status, HelpRequest.Status.OPEN)
        self.assertFalse(Sanction.objects.exists())

    def test_penalty_ladder(self):
        first = services.cancel(self._matched_request(minutes_ago=2), self.helper)
        self.assertEqual(first.level, Sanction.Level.WARNING)
        self.assertEqual(services.accept_block_reason(self.helper), '')

        second = services.cancel(self._matched_request(minutes_ago=2), self.helper)
        self.assertEqual(second.level, Sanction.Level.NO_ACCEPT)
        self.assertNotEqual(services.accept_block_reason(self.helper), '')
        self.assertEqual(services.post_block_reason(self.helper), '')

        Sanction.objects.update(ends_at=timezone.now())  # 制限を明けさせて3回目
        third = services.cancel(self._matched_request(minutes_ago=2), self.helper)
        self.assertEqual(third.level, Sanction.Level.NO_ACTIVITY)
        self.client.force_login(self.helper)
        response = self.client.get(reverse('core:help_request_create'))
        self.assertRedirects(response, reverse('core:profile'))

    def test_requester_withdraw_after_grace_is_penalized(self):
        hr = self._matched_request(minutes_ago=5)
        sanction = services.cancel(hr, self.requester)
        self.assertEqual(sanction.user, self.requester)
        hr.refresh_from_db()
        self.assertEqual(hr.status, HelpRequest.Status.CANCELED)

    def test_withdraw_open_request_is_free(self):
        hr = make_request(self.requester)
        self.assertIsNone(services.cancel(hr, self.requester))


class PrivacyTests(BaseTestCase):
    def test_exact_location_only_for_participants(self):
        requester, helper, stranger = make_user('req'), make_user('helper'), make_user('stranger')
        hr = make_request(requester)
        self.client.force_login(stranger)
        response = self.client.get(reverse('core:help_request_detail', args=[hr.pk]))
        self.assertNotContains(response, '〇〇マンション3階')
        self.assertNotContains(response, str(hr.latitude))

        services.accept_first_come(hr, helper)
        self.client.force_login(helper)
        response = self.client.get(reverse('core:help_request_detail', args=[hr.pk]))
        self.assertContains(response, '〇〇マンション3階')

    def test_public_profile_shows_area_not_coordinates(self):
        user = make_user('pub', motivation='地域の役に立ちたい', area_label='東京都千代田区丸の内')
        response = self.client.get(reverse('core:public_profile', args=['pub']))
        self.assertContains(response, '地域の役に立ちたい')
        self.assertContains(response, '東京都千代田区丸の内')
        self.assertNotContains(response, str(user.latitude))


class FormAndPageTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('me')
        self.client.force_login(self.user)

    def _post_request(self, **data):
        payload = {'title': '荷物', 'mode': 'first_come', 'description': '', 'duration_minutes': 15,
                   'listing_minutes': 30, 'latitude': LAT, 'longitude': LNG, 'skill_names': 'スマホ操作, ｽﾏﾎ操作'}
        payload.update(data)
        return self.client.post(reverse('core:help_request_create'), payload)

    def test_create_requires_description_or_preset_tag(self):
        response = self._post_request()
        self.assertContains(response, '定型タグを1つ以上')
        tag = PresetTag.objects.get(label='詳細はチャットで')  # 初期データ
        response = self._post_request(preset_tags=[tag.pk])
        hr = HelpRequest.objects.get()
        self.assertRedirects(response, reverse('core:help_request_detail', args=[hr.pk]))
        self.assertEqual([s.name for s in hr.required_skills.all()], ['スマホ操作'])
        self.assertAlmostEqual((hr.expires_at - timezone.now()).total_seconds(), 30 * 60, delta=10)

    def test_create_requires_location(self):
        response = self._post_request(description='重い', latitude='', longitude='')
        self.assertContains(response, '場所を指定してください')

    def test_scheduled_request_lists_until_scheduled_time(self):
        when = timezone.localtime() + timedelta(days=2)
        self._post_request(description='重い', scheduled_for=when.strftime('%Y-%m-%dT%H:%M'))
        hr = HelpRequest.objects.get()
        self.assertEqual(hr.expires_at, hr.scheduled_for)

    def test_profile_edit_updates_skills_and_location(self):
        with mock.patch.object(services, 'area_label_for', return_value='東京都千代田区丸の内'):
            self.client.post(reverse('core:profile_edit'), {
                'motivation': '子育てが落ち着いたので', 'bio': '', 'skill_names': '見守り, 料理',
                'latitude': NEAR[0], 'longitude': NEAR[1]})
        self.user.refresh_from_db()
        self.assertEqual(sorted(s.name for s in self.user.skills.all()), ['料理', '見守り'])
        self.assertEqual(self.user.area_label, '東京都千代田区丸の内')
        self.assertAlmostEqual(self.user.latitude, NEAR[0])

    def test_account_settings_changes_username(self):
        self.client.post(reverse('core:account_settings'), {'username': 'renamed', 'save_account': '1'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'renamed')

    def test_activity_base(self):
        self.client.post(reverse('core:add_base'), {'label': '職場', 'latitude': FAR[0], 'longitude': FAR[1]})
        self.assertEqual(self.user.bases.get().label, '職場')

    def test_pages_render(self):
        hr = make_request(make_user('other'), area_label='東京都千代田区丸の内')
        for name, args in [('core:help_request_list', []), ('core:help_request_create', []),
                           ('core:help_request_detail', [hr.pk]), ('core:profile', []),
                           ('core:profile_edit', []), ('core:account_settings', []),
                           ('core:notifications', []), ('core:stats', []),
                           ('core:public_profile', ['other']), ('core:report_user', ['other'])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse('core:stats'), {'area': '東京都'}).context['total'], 1)

    def test_active_bar_shows_in_progress_request(self):
        hr = make_request(make_user('other'))
        services.accept_first_come(hr, self.user)
        response = self.client.get(reverse('core:profile'))
        self.assertContains(response, 'active-bar')
        self.assertEqual(response.context['active_matches'][0].help_request, hr)


class ChatAndReportTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.requester, self.helper = make_user('req'), make_user('helper')
        self.match = services.accept_first_come(make_request(self.requester), self.helper)

    def test_participants_only(self):
        services.post_message(self.match, self.helper, 'こんにちは')
        with self.assertRaises(services.ServiceError):
            services.post_message(self.match, make_user('stranger'), 'やあ')
        self.client.force_login(make_user('outsider'))
        self.assertEqual(self.client.get(reverse('core:chat', args=[self.match.pk])).status_code, 404)

    def test_chat_page_fallback_post_and_read(self):
        self.client.force_login(self.requester)
        self.client.post(reverse('core:chat', args=[self.match.pk]), {'body': '5分後に着きます'})
        self.assertEqual(Message.objects.get().body, '5分後に着きます')
        self.assertTrue(Notification.objects.filter(user=self.helper, kind='message').exists())

    def test_moderation_records_only_by_default(self):
        message = services.post_message(self.match, self.helper, 'LINE ID教えて。090-1234-5678')
        self.assertGreaterEqual(message.risk_score, 0.8)
        self.assertFalse(message.is_hidden)

    @override_settings(MODERATION_AUTO_HIDE=True)
    def test_moderation_hides_when_enforced(self):
        message = services.post_message(self.match, self.helper, 'LINE ID教えて。090-1234-5678')
        self.assertTrue(message.is_hidden)
        self.assertFalse(Notification.objects.filter(user=self.requester, kind='message').exists())
        self.client.force_login(self.requester)
        response = self.client.get(reverse('core:chat', args=[self.match.pk]))
        self.assertNotContains(response, '090-1234-5678')

    def test_reports_from_three_users_trigger_auto_sanction(self):
        target = make_user('target')
        for i in range(services.REPORT_AUTO_SANCTION_REPORTERS):
            self.assertIsNone(services.strongest_sanction(target))
            services.file_report(make_user(f'r{i}'), target, Report.Category.DATING)
        self.assertEqual(services.strongest_sanction(target).level, Sanction.Level.NO_ACCEPT)
        with self.assertRaises(services.ServiceError):
            services.file_report(target, target, Report.Category.OTHER)


class EmailDigestTests(BaseTestCase):
    def test_sends_one_digest_for_offline_users(self):
        user = make_user('mailme')
        services.notify(user, 'matched', 'Aさんが引き受けました', '/1/')
        services.notify(user, 'message', 'Aさんから新しいメッセージ', '/matches/1/chat/')
        services.notify(user, 'nearby_request', '近くで依頼', '/2/')  # 既定では近くの依頼はメールしない
        self.assertEqual(services.send_email_digests(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('2件', mail.outbox[0].subject)
        self.assertEqual(services.send_email_digests(), 0)  # 処理済みは再送しない

    def test_skips_online_users_and_quiet_hours(self):
        online, quiet = make_user('online'), make_user('quiet')
        services.set_online(online.pk, 1)
        setting = services.notification_setting(quiet)
        setting.quiet_start, setting.quiet_end = dtime(0, 0), dtime(23, 59)
        setting.save()
        services.notify(online, 'matched', 'x')
        services.notify(quiet, 'matched', 'y')
        services.send_email_digests()
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(Notification.objects.filter(user=quiet, email_processed_at__isnull=True).exists())

    def test_periodic_command(self):
        make_request(make_user('req'), expires_at=timezone.now() - timedelta(minutes=1))
        call_command('run_periodic_tasks', stdout=StringIO())
        self.assertEqual(HelpRequest.objects.get().status, HelpRequest.Status.EXPIRED)


@override_settings(GEOCODER_ENABLED=False, ACCOUNT_EMAIL_VERIFICATION='none')
class SignupTests(TestCase):
    def test_signup_with_email(self):
        response = self.client.post(reverse('account_signup'), {
            'email': 'new@example.com', 'username': 'newbie',
            'password1': 'complex-pass-99', 'password2': 'complex-pass-99'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username='newbie', email='new@example.com').exists())


@override_settings(GEOCODER_ENABLED=False)
class WebSocketTests(TransactionTestCase):
    def _communicator(self, path, user):
        communicator = WebsocketCommunicator(URLRouter(websocket_urlpatterns), path)
        communicator.scope['user'] = user
        return communicator

    async def test_notifications_and_location(self):
        user = await sync_to_async(make_user)('ws', None)
        communicator = self._communicator('/ws/notifications/', user)
        self.assertTrue((await communicator.connect())[0])
        self.assertTrue(await sync_to_async(services.is_online)(user.pk))

        await get_channel_layer().group_send(
            f'user_{user.pk}', {'type': 'notify', 'message': 'hello', 'url': '/1/', 'kind': 'matched'})
        self.assertEqual(await communicator.receive_json_from(), {'message': 'hello', 'url': '/1/', 'kind': 'matched'})

        await communicator.send_json_to({'type': 'location', 'lat': 35.0, 'lng': 139.0})
        await communicator.disconnect()
        await user.arefresh_from_db()
        self.assertEqual((user.latitude, user.longitude), (35.0, 139.0))
        self.assertIsNotNone(user.location_updated_at)

    async def test_chat_roundtrip_and_outsider_rejected(self):
        requester = await sync_to_async(make_user)('req')
        helper = await sync_to_async(make_user)('helper')
        hr = await sync_to_async(make_request)(requester)
        match = await sync_to_async(services.accept_first_come)(hr, helper)

        a = self._communicator(f'/ws/chat/{match.pk}/', requester)
        b = self._communicator(f'/ws/chat/{match.pk}/', helper)
        self.assertTrue((await a.connect())[0])
        self.assertTrue((await b.connect())[0])
        await a.send_json_to({'body': '今向かっています'})
        received = await b.receive_json_from()
        self.assertEqual(received['message']['body'], '今向かっています')
        await a.disconnect()
        await b.disconnect()

        outsider = await sync_to_async(make_user)('outsider')
        self.assertFalse((await self._communicator(f'/ws/chat/{match.pk}/', outsider).connect())[0])
        self.assertFalse((await self._communicator('/ws/notifications/', AnonymousUser()).connect())[0])
