import json
import tempfile
from datetime import time as dtime
from datetime import timedelta
from io import BytesIO, StringIO
from unittest import mock

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import geocoding, images, line_chat, moderation, services, skills
from .models import (ActivityBase, Application, HelpRequest, LineChatState, Match, Message, Notification, PresetTag,
                     PushSubscription, Report, Review, Sanction, Skill, User, level_for_points, rank_for_level)
from .routing import websocket_urlpatterns
from .text import normalize_text

# 東京駅付近を基準点にする。緯度0.001度 ≒ 111m
LAT, LNG = 35.6812, 139.7671
NEAR = (LAT + 0.002, LNG)   # 約222m
FAR = (LAT + 0.01, LNG)     # 約1.1km


def make_user(name, loc=(LAT, LNG), skills_=(), **extra):
    # 既定は「顔写真の審査済み」ユーザー(写真ファイルは使わないテストが多いのでパスだけ入れる)
    extra.setdefault('avatar', 'avatars/test.jpg')
    extra.setdefault('avatar_status', User.PhotoStatus.APPROVED)
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
        self.assertEqual(response.json()['results'][0], {'name': 'スマホ操作', 'official': True, 'similar': False})


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
        User.objects.filter(pk=self.user.pk).update(points=1000)
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

    def test_chat_photo_post_and_view_permissions(self):
        self.client.force_login(self.requester)
        response = self.client.post(reverse('core:chat', args=[self.match.pk]),
                                    {'body': '', 'image': jpeg_upload('chat.jpg')})
        self.assertEqual(response.status_code, 302)
        message = Message.objects.get()
        self.assertTrue(message.image)
        self.assertIn('/image/', services.message_payload(message)['image_url'])
        # 参加者には見える
        self.client.force_login(self.helper)
        self.assertEqual(self.client.get(reverse('core:chat_image', args=[message.pk])).status_code, 200)
        # 第三者には見えない
        outsider = make_user('outsider-photo')
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse('core:chat_image', args=[message.pk])).status_code, 404)

    def test_no_posting_after_complete_and_history_kept_during_retention(self):
        services.post_message(self.match, self.helper, 'よろしく')
        services.complete(self.match.help_request, self.requester)
        self.match.refresh_from_db()
        self.assertFalse(services.chat_is_open(self.match))
        with self.assertRaises(services.ServiceError):
            services.post_message(self.match, self.helper, 'まだ送れる？')
        # 既定の保存期間(3日)の間は履歴を見られる
        self.assertTrue(services.chat_history_visible(self.match))
        self.assertIn(self.match, services.open_chats(self.requester))
        self.assertEqual(services.purge_expired_chat_messages(), 0)
        self.assertEqual(Message.objects.count(), 1)

    def test_purge_deletes_messages_after_retention(self):
        hr = make_request(make_user('req2'), chat_retention_days=1)
        match = services.accept_first_come(hr, make_user('helper2'))
        services.post_message(match, match.helper, 'ありがとう')
        services.complete(hr, hr.requester)
        Match.objects.filter(pk=match.pk).update(completed_at=timezone.now() - timedelta(days=2))
        match.refresh_from_db()
        self.assertFalse(services.chat_history_visible(match))
        self.assertEqual(services.purge_expired_chat_messages(), 1)
        self.assertEqual(Message.objects.filter(match=match).count(), 0)

    def test_immediate_retention_purges_right_after_complete(self):
        hr = make_request(make_user('req3'), chat_retention_days=0)
        match = services.accept_first_come(hr, make_user('helper3'))
        services.post_message(match, match.helper, 'hi')
        services.complete(hr, hr.requester)
        self.assertEqual(services.purge_expired_chat_messages(), 1)

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


class MultiHelperTests(BaseTestCase):
    def test_first_come_fills_slots_then_closes(self):
        requester, h1, h2, h3 = make_user('mreq'), make_user('mh1'), make_user('mh2'), make_user('mh3')
        hr = make_request(requester, headcount=2)
        services.accept_first_come(hr, h1)
        hr.refresh_from_db()
        self.assertEqual(hr.status, HelpRequest.Status.IN_PROGRESS)
        self.assertTrue(hr.accepting)
        self.assertEqual(hr.slots_left, 1)
        with self.assertRaises(services.ServiceError):  # 同じ人は二重参加できない
            services.accept_first_come(hr, h1)
        services.accept_first_come(hr, h2)
        hr.refresh_from_db()
        self.assertEqual(hr.slots_left, 0)
        self.assertFalse(hr.accepting)
        with self.assertRaises(services.ServiceError):  # 満員
            services.accept_first_come(hr, h3)

    def test_group_chat_shared_and_read_counts(self):
        requester, h1, h2 = make_user('greq2'), make_user('gh12'), make_user('gh22')
        hr = make_request(requester, headcount=2)
        m1 = services.accept_first_come(hr, h1)
        services.accept_first_come(hr, h2)
        message = services.post_message(m1, h1, '集合場所に着きました')
        self.assertEqual(message.help_request_id, hr.pk)
        self.assertEqual(list(hr.messages.all()), [message])  # 依頼単位で同じ会話
        self.assertEqual(services.mark_chat_read(hr, requester), 1)
        self.assertEqual(services.message_payload(message)['read_count'], 1)
        services.mark_chat_read(hr, h2)
        self.assertEqual(services.message_payload(message)['read_count'], 2)
        outsider = make_user('goutsider')
        with self.assertRaises(services.ServiceError):
            services.post_message(m1, outsider, '部外者です')

    def test_complete_gives_points_to_all_helpers(self):
        requester, h1, h2 = make_user('creq'), make_user('ch1'), make_user('ch2')
        hr = make_request(requester, headcount=2)
        services.accept_first_come(hr, h1)
        services.accept_first_come(hr, h2)
        services.complete(hr, requester)
        h1.refresh_from_db()
        h2.refresh_from_db()
        self.assertEqual(h1.points, services.HELPER_POINTS)
        self.assertEqual(h2.points, services.HELPER_POINTS)
        self.assertEqual(hr.matches.filter(status=Match.Status.DONE).count(), 2)

    def test_helper_cancel_frees_slot(self):
        requester, h1, h2, h3 = make_user('xreq'), make_user('xh1'), make_user('xh2'), make_user('xh3')
        hr = make_request(requester, headcount=2)
        services.accept_first_come(hr, h1)
        services.accept_first_come(hr, h2)
        self.assertFalse(hr.accepting)
        services.cancel(hr, h1)
        hr.refresh_from_db()
        self.assertEqual(hr.slots_left, 1)
        self.assertTrue(hr.accepting)
        services.accept_first_come(hr, h3)  # 空いた枠に入れる
        self.assertEqual(hr.slots_left, 0)

    def test_form_headcount_choice_and_custom(self):
        freq = make_user('freq')
        User.objects.filter(pk=freq.pk).update(points=1000)
        self.client.force_login(freq)
        payload = {'title': '荷物', 'mode': 'first_come', 'description': '重い荷物です', 'duration_minutes': 15,
                   'listing_minutes': 30, 'latitude': LAT, 'longitude': LNG, 'skill_names': '',
                   'headcount_choice': 'custom', 'headcount_custom': 6}
        response = self.client.post(reverse('core:help_request_create'), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(HelpRequest.objects.get().headcount, 6)
        payload['headcount_choice'] = '3'
        payload.pop('headcount_custom')
        self.client.post(reverse('core:help_request_create'), payload)
        self.assertEqual(HelpRequest.objects.order_by('pk').last().headcount, 3)


class ImprovementBatchTests(BaseTestCase):
    def test_iniad_verified_email_exempts_photo_gate(self):
        from allauth.account.models import EmailAddress
        user = make_user('iniadok', avatar='', avatar_status=User.PhotoStatus.NONE)
        self.assertNotEqual(services.photo_block_reason(user), '')  # 未認証ならブロック
        EmailAddress.objects.create(user=user, email='iniadok@iniad.org', verified=False, primary=True)
        self.assertNotEqual(services.photo_block_reason(user), '')
        EmailAddress.objects.filter(user=user).update(verified=True)
        self.assertEqual(services.photo_block_reason(user), '')  # 認証済みなら写真なしで使える

    def test_other_domain_verified_still_blocked(self):
        from allauth.account.models import EmailAddress
        user = make_user('gmailok', avatar='', avatar_status=User.PhotoStatus.NONE)
        EmailAddress.objects.create(user=user, email='gmailok@gmail.com', verified=True, primary=True)
        self.assertNotEqual(services.photo_block_reason(user), '')

    def test_reaction_toggle_and_summary(self):
        requester, h1 = make_user('rreq'), make_user('rh1')
        hr = make_request(requester, headcount=2)
        m1 = services.accept_first_come(hr, h1)
        message = services.post_message(m1, requester, 'ありがとう')
        self.assertTrue(services.toggle_reaction(message, h1, '❤️'))
        summary = services.reaction_summary(message)
        self.assertEqual(summary, [{'emoji': '❤️', 'count': 1, 'user_ids': [h1.pk]}])
        self.assertFalse(services.toggle_reaction(message, h1, '❤️'))  # もう一度押すと外れる
        self.assertEqual(services.reaction_summary(message), [])
        outsider = make_user('rout')
        with self.assertRaises(services.ServiceError):
            services.toggle_reaction(message, outsider, '👍')
        with self.assertRaises(services.ServiceError):
            services.toggle_reaction(message, h1, '🔥')

    def test_message_react_endpoint(self):
        requester, h1 = make_user('ereq'), make_user('eh1')
        hr = make_request(requester)
        m1 = services.accept_first_come(hr, h1)
        message = services.post_message(m1, requester, 'hi')
        self.client.force_login(h1)
        response = self.client.post(reverse('core:message_react', args=[message.pk]),
                                    data=json.dumps({'emoji': '👍'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reactions'][0]['emoji'], '👍')
        self.assertTrue(response.json()['reactions'][0]['mine'])

    def test_forced_off_blocks_external_notifications(self):
        user = make_user('foff')
        setting = services.notification_setting(user)
        setting.forced_off = True
        setting.save()
        with mock.patch.object(services.line_api, 'maybe_notify') as line_mock,                 mock.patch.object(services, 'send_web_push') as push_mock:
            services.notify(user, 'message', 'テスト通知', '/')
        line_mock.assert_not_called()
        push_mock.assert_not_called()
        self.assertEqual(user.notifications.count(), 1)  # アプリ内履歴は残る

    def test_push_subscribe_endpoints(self):
        user = make_user('pushy')
        self.client.force_login(user)
        payload = {'endpoint': 'https://push.example.com/abc', 'keys': {'p256dh': 'key1', 'auth': 'auth1'}}
        response = self.client.post(reverse('core:push_subscribe'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        sub = PushSubscription.objects.get(user=user)
        self.assertEqual(sub.p256dh, 'key1')
        response = self.client.post(reverse('core:push_unsubscribe'),
                                    data=json.dumps({'endpoint': payload['endpoint']}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PushSubscription.objects.filter(user=user).count(), 0)

    def test_admin_force_notifications_off_action(self):
        admin_user = get_user_model().objects.create_superuser(
            username='boss', email='boss@example.com', password='pw')
        target = make_user('target1')
        self.client.force_login(admin_user)
        url = reverse('admin:core_user_changelist')
        response = self.client.post(url, {'action': 'force_notifications_off',
                                          '_selected_action': [str(target.pk)]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(services.notification_setting(target).forced_off)
        response = self.client.post(url, {'action': 'force_notifications_on',
                                          '_selected_action': [str(target.pk)]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(services.notification_setting(target).forced_off)


class LinePostRetentionTests(BaseTestCase):
    def test_post_flow_asks_retention_and_applies_it(self):
        user = make_user('lineuser')
        User.objects.filter(pk=user.pk).update(points=1000)
        user.refresh_from_db()
        with mock.patch.object(line_chat.line_api, 'reply_messages') as reply:
            line_chat.handle_message(user, '依頼する', 't1')
            line_chat.handle_message(user, '荷物の移動', 't2')
            line_chat.handle_message(user, 'なし', 't3')
            line_chat.handle_message(user, '先着順', 't4')
            line_chat.handle_message(user, '15分', 't5')
            line_chat.handle_message(user, '今すぐ', 't6')
            # 保存期間ステップが出ていること
            state = LineChatState.objects.get(user=user)
            self.assertEqual(state.step, 'post_retention')
            line_chat.handle_message(user, '完了後7日', 't7')
            line_chat.handle_message(user, '登録エリアを使う', 't8')
            self.assertEqual(LineChatState.objects.get(user=user).step, 'post_confirm')
            line_chat.handle_postback(user, 'action=post_confirm', 't9')
            self.assertTrue(reply.called)
        hr = HelpRequest.objects.get()
        self.assertEqual(hr.chat_retention_days, 7)


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

    def test_skips_online_users(self):
        online, target = make_user('online'), make_user('target')
        services.set_online(online.pk, 1)
        services.notify(online, 'matched', 'x')
        services.notify(target, 'matched', 'y')
        services.send_email_digests()
        self.assertEqual(len(mail.outbox), 1)  # オンライン中のユーザーには送らない
        self.assertEqual(mail.outbox[0].to, [target.email])

    def test_periodic_command(self):
        make_request(make_user('req'), expires_at=timezone.now() - timedelta(minutes=1))
        call_command('run_periodic_tasks', stdout=StringIO())
        self.assertEqual(HelpRequest.objects.get().status, HelpRequest.Status.EXPIRED)


@override_settings(GEOCODER_ENABLED=False, ACCOUNT_EMAIL_VERIFICATION='none')
class SignupTests(TestCase):
    def test_signup_with_email(self):
        response = self.client.post(reverse('account_signup'), {
            'email': 'new@example.com', 'username': 'newbie', 'birth_date': '2000-04-01',
            'password1': 'complex-pass-99', 'password2': 'complex-pass-99'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='newbie', email='new@example.com')
        self.assertEqual(str(user.birth_date), '2000-04-01')
        self.assertFalse(user.simple_ui)  # 50歳未満は通常UI

    def test_signup_requires_birth_date_and_senior_gets_simple_ui(self):
        response = self.client.post(reverse('account_signup'), {
            'email': 'nobd@example.com', 'username': 'nobd',
            'password1': 'complex-pass-99', 'password2': 'complex-pass-99'})
        self.assertEqual(response.status_code, 200)  # 生年月日が必須なので作られない
        self.assertFalse(User.objects.filter(username='nobd').exists())
        response = self.client.post(reverse('account_signup'), {
            'email': 'senior@example.com', 'username': 'senior', 'birth_date': '1960-04-01',
            'password1': 'complex-pass-99', 'password2': 'complex-pass-99'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username='senior').simple_ui)  # 50歳以上はシンプルUI


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

    async def test_group_chat_shared_across_matches(self):
        requester = await sync_to_async(make_user)('greq')
        h1 = await sync_to_async(make_user)('gh1')
        h2 = await sync_to_async(make_user)('gh2')
        hr = await sync_to_async(make_request)(requester, headcount=2)
        m1 = await sync_to_async(services.accept_first_come)(hr, h1)
        m2 = await sync_to_async(services.accept_first_come)(hr, h2)

        a = self._communicator(f'/ws/chat/{m1.pk}/', requester)
        b = self._communicator(f'/ws/chat/{m1.pk}/', h1)
        c = self._communicator(f'/ws/chat/{m2.pk}/', h2)
        for comm in (a, b, c):
            self.assertTrue((await comm.connect())[0])
        await b.send_json_to({'body': 'グループへの一斉連絡です'})
        got_a = await a.receive_json_from()
        got_c = await c.receive_json_from()
        self.assertEqual(got_a['message']['body'], 'グループへの一斉連絡です')
        self.assertEqual(got_c['message']['body'], 'グループへの一斉連絡です')
        self.assertEqual(got_c['message']['sender'], h1.username)
        for comm in (a, b, c):
            await comm.disconnect()

    async def test_reaction_broadcast(self):
        requester = await sync_to_async(make_user)('wreq')
        h1 = await sync_to_async(make_user)('wh1')
        hr = await sync_to_async(make_request)(requester)
        m1 = await sync_to_async(services.accept_first_come)(hr, h1)
        message = await sync_to_async(services.post_message)(m1, requester, 'hello')

        a = self._communicator(f'/ws/chat/{m1.pk}/', requester)
        b = self._communicator(f'/ws/chat/{m1.pk}/', h1)
        for comm in (a, b):
            self.assertTrue((await comm.connect())[0])
        await b.send_json_to({'type': 'reaction', 'message_id': message.pk, 'emoji': '👍'})
        got = {}
        for _ in range(3):  # 接続時の既読イベントが先に届くことがある
            got = await a.receive_json_from()
            if got.get('kind') == 'reaction_update':
                break
        self.assertEqual(got['kind'], 'reaction_update')
        self.assertEqual(got['reactions'][0]['emoji'], '👍')
        self.assertEqual(got['reactions'][0]['user_ids'], [h1.pk])
        for comm in (a, b):
            await comm.disconnect()

    async def test_chat_read_event_realtime(self):
        requester = await sync_to_async(make_user)('rreq')
        helper = await sync_to_async(make_user)('hhelper')
        hr = await sync_to_async(make_request)(requester)
        match = await sync_to_async(services.accept_first_come)(hr, helper)

        a = self._communicator(f'/ws/chat/{match.pk}/', requester)
        b = self._communicator(f'/ws/chat/{match.pk}/', helper)
        self.assertTrue((await a.connect())[0])
        self.assertTrue((await b.connect())[0])
        await a.send_json_to({'body': '写真を送ります'})
        received = await b.receive_json_from()
        self.assertEqual(received['message']['body'], '写真を送ります')
        await a.receive_json_from()  # 自分の送信エコーを読み捨てる
        await b.send_json_to({'type': 'read'})
        event = await a.receive_json_from()
        self.assertEqual(event['kind'], 'read')
        self.assertEqual(event['reader_id'], helper.pk)
        message = await Message.objects.aget()
        self.assertIsNotNone(message.read_at)
        await a.disconnect()
        await b.disconnect()


def jpeg_upload(name='face.jpg', color=(200, 150, 120), size=(1200, 900), exif=None):
    """テスト用の画像(必要ならEXIFつき)。"""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image
    buffer = BytesIO()
    Image.new('RGB', size, color).save(buffer, format='JPEG', **({'exif': exif} if exif else {}))
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/jpeg')


@override_settings(GEOCODER_ENABLED=False, MEDIA_ROOT=tempfile.mkdtemp(prefix='suki-test-media-'))
class PhotoTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_photo_required_before_requesting_or_helping(self):
        user = make_user('nophoto', avatar='', avatar_status=User.PhotoStatus.NONE)
        hr = make_request(make_user('req'))
        self.assertIn('顔写真の登録が必要', services.accept_block_reason(user))
        self.assertIn('顔写真', services.post_block_reason(user))
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, user)
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse('core:help_request_create')), reverse('core:profile'))

    def test_profile_upload_strips_exif_and_waits_for_review(self):
        from PIL import Image
        user = make_user('uploader', avatar='', avatar_status=User.PhotoStatus.NONE)
        self.client.force_login(user)
        exif = Image.Exif()
        exif[0x010F] = 'TestCamera'  # Make
        response = self.client.post(reverse('core:profile_edit'), {
            'motivation': '', 'bio': '', 'skill_names': '', 'photo': jpeg_upload(exif=exif.tobytes())})
        self.assertRedirects(response, reverse('core:profile'))
        user.refresh_from_db()
        self.assertEqual(user.avatar_status, User.PhotoStatus.PENDING)
        with user.avatar.open('rb') as f:
            saved = Image.open(f)
            self.assertEqual(len(saved.getexif()), 0)
            self.assertLessEqual(max(saved.size), 800)
        self.assertIn('審査中', services.accept_block_reason(user))

        services.review_photo(user, approve=True)
        self.assertEqual(services.accept_block_reason(user), '')
        self.assertTrue(Notification.objects.filter(user=user, kind='photo').exists())

    def test_profile_requires_photo_when_missing(self):
        user = make_user('need', avatar='', avatar_status=User.PhotoStatus.NONE)
        self.client.force_login(user)
        response = self.client.post(reverse('core:profile_edit'), {'motivation': 'x', 'bio': '', 'skill_names': ''})
        self.assertContains(response, '顔写真は必須です')

    def test_mosaic_until_matched(self):
        requester = make_user('req', avatar='', avatar_status=User.PhotoStatus.NONE)
        services.set_photo(requester, images.normalize_photo(jpeg_upload()))
        services.review_photo(requester, approve=True)
        helper, stranger = make_user('helper'), make_user('stranger')
        url = reverse('core:user_photo', args=[requester.pk])

        with requester.avatar.open('rb') as f:
            original = f.read()
        self.client.force_login(stranger)
        mosaic = b''.join(self.client.get(url).streaming_content)
        self.assertNotEqual(mosaic, original)

        services.accept_first_come(make_request(requester), helper)
        self.client.force_login(helper)
        self.assertEqual(b''.join(self.client.get(url).streaming_content), original)

    def test_unreviewed_photo_hidden_from_others(self):
        user = make_user('pending', avatar='', avatar_status=User.PhotoStatus.NONE)
        services.set_photo(user, images.normalize_photo(jpeg_upload()))
        url = reverse('core:user_photo', args=[user.pk])
        self.client.force_login(make_user('other'))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_rejected_photo_explains_reason(self):
        user = make_user('ng')
        services.review_photo(user, approve=False, reason='顔が写っていません')
        self.assertIn('顔が写っていません', services.photo_block_reason(user))


class ListingAndMessageTests(BaseTestCase):
    def test_recommended_requests_come_first(self):
        requester = make_user('req')
        plain = make_request(requester, loc=(LAT + 0.0005, LNG))
        matched = make_request(requester, loc=(LAT + 0.003, LNG), skill_names=['見守り'])
        other_skill = make_request(requester, loc=(LAT + 0.001, LNG), skill_names=['料理'])
        self.client.force_login(make_user('helper', skills_=['見守り']))
        context = self.client.get(reverse('core:help_request_list')).context
        self.assertEqual(context['recommended'], [matched])
        self.assertEqual(context['others'], [plain, other_skill])  # 登録外スキルでも近ければ表示

    def test_listing_time_is_optional_and_long(self):
        user = make_user('me')
        User.objects.filter(pk=user.pk).update(points=1000)
        self.client.force_login(user)
        base = {'title': '荷物', 'mode': 'first_come', 'description': '重い', 'duration_minutes': 15,
                'latitude': LAT, 'longitude': LNG, 'skill_names': ''}
        self.client.post(reverse('core:help_request_create'), {**base, 'listing_minutes': ''})
        self.client.post(reverse('core:help_request_create'), {**base, 'listing_minutes': 4320})
        first, second = HelpRequest.objects.order_by('pk')
        self.assertAlmostEqual((first.expires_at - first.created_at).total_seconds(), 180 * 60, delta=10)
        self.assertAlmostEqual((second.expires_at - second.created_at).total_seconds(), 3 * 24 * 3600, delta=10)

    def test_now_ignores_scheduled_time(self):
        me = make_user('me')
        User.objects.filter(pk=me.pk).update(points=1000)
        self.client.force_login(me)
        later = (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('core:help_request_create'), {
            'title': '荷物', 'mode': 'first_come', 'description': '重い', 'duration_minutes': 15, 'when': 'now',
            'scheduled_for': later, 'latitude': LAT, 'longitude': LNG, 'skill_names': ''})
        self.assertIsNone(HelpRequest.objects.get().scheduled_for)

    def test_message_tab_is_blocked_without_match(self):
        user = make_user('lonely')
        self.client.force_login(user)
        response = self.client.get(reverse('core:help_request_list'))
        self.assertContains(response, 'data-toast="現在はマッチングしていないため、メッセージ機能は使用できません。"')
        self.assertRedirects(self.client.get(reverse('core:message_list')), reverse('core:help_request_list'))

        match = services.accept_first_come(make_request(make_user('req')), user)
        response = self.client.get(reverse('core:help_request_list'))
        self.assertContains(response, reverse('core:message_list'))
        self.assertRedirects(self.client.get(reverse('core:message_list')), reverse('core:chat', args=[match.pk]))


class SimilarityTests(BaseTestCase):
    def test_metrics(self):
        self.assertEqual(skills.jaccard('みまもり', 'みまもり'), 1.0)
        self.assertGreater(skills.jaccard('みまもり', 'こどものみまもり'), 0.3)
        self.assertAlmostEqual(skills.levenshtein_ratio('すまほ', 'すまほそうさ'), 0.5)
        self.assertGreaterEqual(skills.similarity('見守り', 'みまもり', '子どもの見守り', 'こどものみまもり'), 0.8)

    def test_search_by_reading_and_similar(self):
        self.assertEqual([s.name for s in skills.search('みま')][:1], ['見守り'])
        results = skills.search('子守り')
        self.assertIn('見守り', [s.name for s in results if s.is_similar])


@override_settings(GEOCODER_ENABLED=False, ACCOUNT_EMAIL_VERIFICATION='mandatory',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EmailCodeTests(TestCase):
    def test_signup_sends_six_digit_code_from_suki_match(self):
        import re
        response = self.client.post(reverse('account_signup'), {
            'email': 'code@example.com', 'username': 'coder', 'birth_date': '1995-05-05',
            'password1': 'complex-pass-99', 'password2': 'complex-pass-99'})
        self.assertRedirects(response, reverse('account_email_verification_sent'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertTrue(message.subject.startswith('[suki-match]'))
        self.assertNotIn('127.0.0.1', message.subject + message.body)
        self.assertNotIn('testserver', message.subject + message.body)
        code = re.search(r'\b(\d{6})\b', message.body).group(1)

        page = self.client.get(reverse('account_email_verification_sent'))
        self.assertContains(page, 'data-resend-seconds')
        self.client.post(reverse('account_email_verification_sent'), {'code': code})
        from allauth.account.models import EmailAddress
        self.assertTrue(EmailAddress.objects.get(email='code@example.com').verified)


DB_STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


@override_settings(GEOCODER_ENABLED=False, STORAGES=DB_STORAGES)
class DatabaseStorageTests(TestCase):
    """本番(Render)と同じく、顔写真をDBに保存する構成での動作。"""

    def test_photo_flow_with_database_storage(self):
        from .models import StoredFile
        requester = make_user('req', avatar='', avatar_status=User.PhotoStatus.NONE)
        services.set_photo(requester, images.normalize_photo(jpeg_upload()))
        services.review_photo(requester, approve=True)
        self.assertEqual(StoredFile.objects.count(), 2)  # 元写真とモザイク

        url = reverse('core:user_photo', args=[requester.pk])
        self.client.force_login(make_user('stranger'))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')

        old = requester.avatar.name
        services.set_photo(requester, images.normalize_photo(jpeg_upload(color=(10, 20, 30))))
        self.assertFalse(StoredFile.objects.filter(name=old).exists())  # 古い写真は消える


class OperationsTests(BaseTestCase):
    def test_healthz(self):
        self.assertEqual(self.client.get(reverse('core:healthz')).content, b'ok')

    @override_settings(TASKS_TOKEN='secret-token')
    def test_run_tasks_requires_token(self):
        make_request(make_user('req'), expires_at=timezone.now() - timedelta(minutes=1))
        url = reverse('core:run_tasks')
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(self.client.post(url, HTTP_X_TASKS_TOKEN='wrong').status_code, 403)
        response = self.client.post(url, HTTP_X_TASKS_TOKEN='secret-token')
        self.assertEqual(response.json()['expired'], 1)

    def test_run_tasks_disabled_without_token(self):
        self.assertEqual(self.client.post(reverse('core:run_tasks'), HTTP_X_TASKS_TOKEN='').status_code, 403)
class ShadowBanTests(BaseTestCase):
    """ゴーストBan: 本人には通常どおり、他のユーザーからは存在しないように見える。"""

    def test_banned_requester_hidden_from_list_and_detail(self):
        banned = make_user('ghost', is_shadow_banned=True)
        hr = make_request(banned)
        helper = make_user('helper')
        self.client.force_login(helper)
        listed = self.client.get(reverse('core:help_request_list'), {'all': '1'}).context['help_requests']
        self.assertNotIn(hr, listed)
        self.assertEqual(self.client.get(reverse('core:help_request_detail', args=[hr.pk])).status_code, 404)
        self.client.force_login(banned)
        self.assertEqual(self.client.get(reverse('core:help_request_detail', args=[hr.pk])).status_code, 200)

    def test_banned_requester_sends_no_nearby_notifications(self):
        banned = make_user('ghost', is_shadow_banned=True)
        make_user('near', NEAR, ['見守り'])
        hr = make_request(banned, skill_names=['見守り'])
        self.assertEqual(services.notify_nearby_helpers(hr), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_banned_helper_cannot_match(self):
        requester = make_user('req')
        hr = make_request(requester)
        ghost = make_user('ghost', is_shadow_banned=True)
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, ghost)
        approval = make_request(requester, mode=HelpRequest.Mode.APPROVAL)
        with self.assertRaises(services.ServiceError):
            services.apply(approval, ghost)
        self.assertEqual(Match.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_banned_applicant_hidden_and_cannot_be_approved(self):
        requester = make_user('req')
        hr = make_request(requester, mode=HelpRequest.Mode.APPROVAL)
        applicant = make_user('applicant')
        application = services.apply(hr, applicant)
        User.objects.filter(pk=applicant.pk).update(is_shadow_banned=True)
        applicant.refresh_from_db()
        with self.assertRaises(services.ServiceError):
            services.approve(application, requester)
        self.client.force_login(requester)
        context = self.client.get(reverse('core:help_request_detail', args=[hr.pk])).context
        self.assertEqual(list(context['applications']), [])
class PointsEconomyTests(BaseTestCase):
    """ポイント制(振替モデル): 投稿で預かり、完了でヘルパーへ、キャンセル・期限切れで返金。"""

    def _funded(self, name, points):
        user = make_user(name)
        User.objects.filter(pk=user.pk).update(points=points)
        user.refresh_from_db()
        return user

    def test_starter_points_on_signup(self):
        from unittest import mock as _mock
        from .adapters import AccountAdapter, STARTER_POINTS
        adapter = AccountAdapter()
        form = _mock.Mock()
        form.cleaned_data = {'username': 'newbie', 'email': 'newbie@example.com'}
        user = adapter.save_user(None, User(), form, commit=True)
        self.assertEqual(user.points, STARTER_POINTS)

    def test_charge_and_payout_single_helper(self):
        requester = self._funded('preq', 100)
        hr = make_request(requester, duration_minutes=30)
        services.publish_request(hr)
        requester.refresh_from_db()
        self.assertEqual((requester.points, hr.points_cost), (70, 30))
        helper = make_user('phelp')
        services.accept_first_come(hr, helper)
        services.complete(hr, requester)
        helper.refresh_from_db()
        self.assertEqual((helper.points, helper.total_earned), (30, 30))
        self.assertEqual(helper.level, level_for_points(30))

    def test_payout_per_helper_full_posted_time(self):
        # 実績ベース精算: タイマーも申告もなければ各自が投稿時間ぶんを受け取り、
        # 預かり分を超えた分は依頼者の残高から払われる
        requester = self._funded('sreq', 100)
        hr = make_request(requester, duration_minutes=31, headcount=2)
        services.publish_request(hr)
        h1, h2 = make_user('sh1'), make_user('sh2')
        services.accept_first_come(hr, h1)
        services.accept_first_come(hr, h2)
        services.complete(hr, requester)
        h1.refresh_from_db()
        h2.refresh_from_db()
        requester.refresh_from_db()
        self.assertEqual((h1.points, h2.points), (31, 31))
        self.assertEqual(requester.points, 100 - 31 - 31)

    def test_insufficient_points_blocks_publish(self):
        requester = self._funded('breq', 10)
        hr = make_request(requester, duration_minutes=30)
        with self.assertRaises(services.ServiceError):
            services.publish_request(hr)
        requester.refresh_from_db()
        hr.refresh_from_db()
        self.assertEqual((requester.points, hr.points_cost), (10, 0))

    def test_refund_on_requester_cancel(self):
        requester = self._funded('creq', 100)
        hr = make_request(requester, duration_minutes=30)
        services.publish_request(hr)
        services.cancel(hr, requester)
        requester.refresh_from_db()
        hr.refresh_from_db()
        self.assertEqual(requester.points, 100)
        self.assertTrue(hr.points_settled)

    def test_refund_on_expire(self):
        requester = self._funded('ereq', 100)
        hr = make_request(requester, duration_minutes=30)
        services.publish_request(hr)
        HelpRequest.objects.filter(pk=hr.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        services.expire_requests()
        requester.refresh_from_db()
        self.assertEqual(requester.points, 100)

    def test_spending_does_not_lower_level(self):
        user = make_user('lvl')
        user.add_points(100)
        self.assertEqual(user.level, level_for_points(100))
        hr = make_request(user, duration_minutes=60)
        services.publish_request(hr)
        user.refresh_from_db()
        self.assertEqual((user.points, user.total_earned), (40, 100))
        self.assertEqual(user.level, level_for_points(100))

    def test_form_rejects_insufficient_balance(self):
        user = self._funded('freq2', 5)
        self.client.force_login(user)
        response = self.client.post(reverse('core:help_request_create'), {
            'title': '荷物', 'mode': 'first_come', 'description': '重い', 'duration_minutes': 30,
            'latitude': LAT, 'longitude': LNG, 'skill_names': ''})
        self.assertContains(response, 'ポイントが不足しています')
        self.assertEqual(HelpRequest.objects.count(), 0)


@override_settings(GEOCODER_ENABLED=True)
class PlaceSearchTests(BaseTestCase):
    """場所検索の統合エンジン: 郵便番号・住所・駅名が引けて、近い順に並ぶこと。"""

    GSI_AKABANE = [
        {'geometry': {'coordinates': [142.522018, 43.617619]},
         'properties': {'title': '北海道美瑛町赤羽'}},
        {'geometry': {'coordinates': [139.720612, 35.780739]},
         'properties': {'title': '東京都北区赤羽'}},
    ]
    GSI_SHIMO = [
        {'geometry': {'coordinates': [139.748, 35.795]},
         'properties': {'title': '東京都北区志茂'}},
    ]

    def _fake_fetch(self, url):
        from urllib.parse import unquote
        url = unquote(url)
        if 'zipcloud' in url:
            return json.dumps({'status': 200, 'results': [
                {'address1': '東京都', 'address2': '北区', 'address3': '志茂'}]})
        if 'nominatim' in url:
            if '武蔵村山' in url:
                return json.dumps([{'lat': '35.7549', 'lon': '139.3874',
                                    'display_name': '武蔵村山市役所, 武蔵村山市, 東京都, 日本'}])
            if '赤羽' in url:
                return json.dumps([{'lat': '35.778926', 'lon': '139.720245',
                                    'display_name': '赤羽駅, 赤羽一丁目, 北区, 東京都, 115-0045, 日本'}])
            return '[]'
        if 'AddressSearch' in url:
            if '山岡家' in url:
                # 店名検索に紛れる GSI の部分一致ノイズ(実例)
                return json.dumps([
                    {'geometry': {'coordinates': [142.0, 44.0]},
                     'properties': {'title': '北海道広尾町ラッコベツ'}},
                    {'geometry': {'coordinates': [131.0, 33.0]},
                     'properties': {'title': '沖縄県北中城村ライカム'}}])
            if '志茂' in url:
                return json.dumps(self.GSI_SHIMO)
            if '赤羽' in url:
                return json.dumps(self.GSI_AKABANE)
            return '[]'
        raise AssertionError(f'unexpected url: {url}')

    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(geocoding, '_fetch', side_effect=self._fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_zipcode_with_hyphen(self):
        results = geocoding.search_places('115-0042')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], '〒115-0042 東京都北区志茂')
        self.assertAlmostEqual(results[0]['lat'], 35.795)

    def test_zipcode_fullwidth_digits(self):
        results = geocoding.search_places('１１５００４２')
        self.assertEqual(len(results), 1)
        self.assertIn('志茂', results[0]['name'])

    def test_akabane_sorted_by_distance_and_includes_station(self):
        # 基準点は北区赤羽のすぐ近く: 北海道の赤羽より北区が先頭に来ること
        results = geocoding.search_places('赤羽', base_lat=35.779, base_lng=139.721)
        names = [r['name'] for r in results]
        # 同名の遠方候補(北海道)より、基準点に近い北区の赤羽が先に来ること
        self.assertLess(names.index('東京都北区赤羽'), names.index('北海道美瑛町赤羽'))
        self.assertEqual(names[-1], '北海道美瑛町赤羽')
        self.assertTrue(any('赤羽駅' in n for n in names))

    def test_shop_query_filters_gsi_noise_and_falls_back_to_area(self):
        # 「ラ」でしか一致しない無関係候補は除外され、末尾の地名でエリアが引ける
        results = geocoding.search_places('ラーメン山岡家武蔵村山店')
        self.assertTrue(results)
        self.assertTrue(all('武蔵村山' in r['name'] for r in results))
        self.assertFalse(any('ラッコベツ' in r['name'] or 'ライカム' in r['name'] for r in results))

    def test_long_vowel_mark_is_preserved(self):
        self.assertEqual(geocoding._normalize_query('ラーメン'), 'ラーメン')

    def test_place_search_api(self):
        user = make_user('searcher')
        response = self.client.get(reverse('core:place_search_api'), {'q': '赤羽'})
        self.assertEqual(response.status_code, 302)  # 未ログインはログイン画面へ
        self.client.force_login(user)
        response = self.client.get(reverse('core:place_search_api'), {'q': '115-0042'})
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body['results'][0]['name'], '〒115-0042 東京都北区志茂')


class PushPromptTests(BaseTestCase):
    """通知オプトインカード: ログイン中ユーザーにだけ表示されること。"""

    def test_prompt_shown_to_logged_in_user(self):
        user = make_user('promptuser')
        self.client.force_login(user)
        response = self.client.get(reverse('core:help_request_list'))
        self.assertContains(response, 'id="push-prompt"')
        self.assertContains(response, 'data-push-enable')

    def test_prompt_hidden_from_anonymous(self):
        response = self.client.get(reverse('core:privacy'))
        self.assertNotContains(response, 'id="push-prompt"')

    def test_viewport_locks_maximum_scale(self):
        # iOS の入力時自動ズーム(拡大バグ)防止が外れていないこと
        response = self.client.get(reverse('core:privacy'))
        self.assertContains(response, 'maximum-scale=1.0')


class ReplyTests(BaseTestCase):
    """メッセージへの返信: 保存・payload・他チャットのメッセージは返信先にできないこと。"""

    def setUp(self):
        super().setUp()
        self.requester, self.helper = make_user('rreq'), make_user('rhelper')
        self.match = services.accept_first_come(make_request(self.requester), self.helper)

    def test_reply_saved_and_in_payload(self):
        first = services.post_message(self.match, self.helper, '集合は駅前で')
        reply = services.post_message(self.match, self.requester, '了解です', reply_to_id=first.pk)
        self.assertEqual(reply.reply_to, first)
        payload = services.message_payload(reply)
        self.assertEqual(payload['reply_to']['id'], first.pk)
        self.assertEqual(payload['reply_to']['sender'], self.helper.username)
        self.assertEqual(payload['reply_to']['excerpt'], '集合は駅前で')
        plain = services.post_message(self.match, self.helper, '通常メッセージ')
        self.assertIsNone(services.message_payload(plain)['reply_to'])

    def test_reply_to_other_chat_or_missing_is_ignored(self):
        other_requester, other_helper = make_user('oreq'), make_user('ohelper')
        other_match = services.accept_first_come(make_request(other_requester), other_helper)
        foreign = services.post_message(other_match, other_helper, '別のチャット')
        reply = services.post_message(self.match, self.requester, '返信のつもり', reply_to_id=foreign.pk)
        self.assertIsNone(reply.reply_to)
        reply2 = services.post_message(self.match, self.requester, '存在しないID', reply_to_id=999999)
        self.assertIsNone(reply2.reply_to)

    def test_reply_via_chat_page_post_and_quote_rendered(self):
        first = services.post_message(self.match, self.helper, '先に着いてます')
        self.client.force_login(self.requester)
        self.client.post(reverse('core:chat', args=[self.match.pk]),
                         {'body': '今向かってます', 'reply_to': str(first.pk)})
        reply = Message.objects.exclude(pk=first.pk).get()
        self.assertEqual(reply.reply_to, first)
        response = self.client.get(reverse('core:chat', args=[self.match.pk]))
        self.assertContains(response, 'bubble-quote')
        self.assertContains(response, '先に着いてます')


class WorkTimerTests(BaseTestCase):
    """作業タイマー実測 + 申告のハイブリッド精算。"""

    def _setup(self, name, requester_points=100, duration=15):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=requester_points)
        requester.refresh_from_db()
        hr = make_request(requester, duration_minutes=duration)
        services.publish_request(hr)
        helper = make_user(f'{name}help')
        match = services.accept_first_come(hr, helper)
        return requester, hr, helper, match

    def _set_timer(self, match, minutes_ago_start, minutes_ago_end=0):
        now = timezone.now()
        Match.objects.filter(pk=match.pk).update(
            work_started_at=now - timedelta(minutes=minutes_ago_start),
            work_ended_at=now - timedelta(minutes=minutes_ago_end) if minutes_ago_end is not None else None)
        match.refresh_from_db()

    def test_start_stop_rules(self):
        _, _, helper, match = self._setup('w1')
        with self.assertRaises(services.ServiceError):
            services.stop_work(match, helper)
        services.start_work(match, helper)
        self.assertIsNotNone(match.work_started_at)
        with self.assertRaises(services.ServiceError):
            services.start_work(match, helper)
        services.stop_work(match, helper)
        self.assertIsNotNone(match.work_ended_at)
        with self.assertRaises(services.ServiceError):
            services.start_work(match, make_user('w1other'))

    def test_measured_rounding_and_cap(self):
        _, hr, _, match = self._setup('w2', duration=15)
        self._set_timer(match, 27)  # 27分作業 → 5分切り上げで30分
        self.assertEqual(services.measured_minutes(match, hr), 30)
        self._set_timer(match, 50)  # 上限は投稿(15分)の2倍=30分
        self.assertEqual(services.measured_minutes(match, hr), 30)

    def test_overtime_charged_to_requester(self):
        # 本件の発端: 15分の依頼で30分掛かったら、差額15ptが依頼者から引かれる
        requester, hr, helper, match = self._setup('w3', requester_points=100, duration=15)
        self._set_timer(match, 27)
        services.complete(hr, requester)
        helper.refresh_from_db()
        requester.refresh_from_db()
        self.assertEqual(helper.points, 30)
        self.assertEqual(requester.points, 100 - 15 - 15)

    def test_shorter_work_refunds_requester(self):
        requester, hr, helper, match = self._setup('w4', requester_points=100, duration=30)
        self._set_timer(match, 8)  # 8分 → 切り上げ10分
        services.complete(hr, requester)
        helper.refresh_from_db()
        requester.refresh_from_db()
        self.assertEqual(helper.points, 10)
        self.assertEqual(requester.points, 100 - 10)

    def test_insufficient_balance_pays_what_is_available(self):
        requester, hr, helper, match = self._setup('w5', requester_points=15, duration=15)
        self._set_timer(match, 27)  # 30分ぶん発生するが残高は預かり15のみ
        services.complete(hr, requester)
        helper.refresh_from_db()
        requester.refresh_from_db()
        self.assertEqual(helper.points, 15)
        self.assertEqual(requester.points, 0)

    def test_declaration_without_timer_is_used(self):
        requester, hr, helper, match = self._setup('w6', requester_points=100, duration=15)
        services.declare_minutes(match, helper, 40)  # 上限2倍で30分に丸められる
        self.assertTrue(match.declaration_approved)
        self.assertEqual(services.effective_minutes(match, hr), 30)
        services.complete(hr, requester)
        helper.refresh_from_db()
        self.assertEqual(helper.points, 30)

    def test_correction_declaration_needs_requester_approval(self):
        requester, hr, helper, match = self._setup('w7', requester_points=100, duration=30)
        self._set_timer(match, 27)  # 実測30分
        services.declare_minutes(match, helper, 20)  # 訂正申告20分
        match.refresh_from_db()
        self.assertFalse(match.declaration_approved)
        self.assertEqual(services.effective_minutes(match, hr), 30)  # 承認までは実測
        services.review_declaration(match, requester, approve=True)
        self.assertEqual(services.effective_minutes(match, hr), 20)
        services.review_declaration(match, requester, approve=False)
        match.refresh_from_db()
        self.assertIsNone(match.declared_minutes)
        self.assertEqual(services.effective_minutes(match, hr), 30)


class PointHistoryTests(BaseTestCase):
    """ポイント台帳: 主要な増減が記録され、履歴ページで自分の分だけ見えること。"""

    def _funded(self, name, points):
        user = make_user(name)
        User.objects.filter(pk=user.pk).update(points=points)
        user.refresh_from_db()
        return user

    def test_escrow_earned_and_settle_extra_recorded(self):
        from .models import PointTransaction
        requester = self._funded('hreq', 100)
        hr = make_request(requester, duration_minutes=15)
        services.publish_request(hr)
        escrow = PointTransaction.objects.get(user=requester, kind='escrow')
        self.assertEqual((escrow.amount, escrow.balance_after), (-15, 85))
        helper = make_user('hhelp')
        match = services.accept_first_come(hr, helper)
        now = timezone.now()
        Match.objects.filter(pk=match.pk).update(
            work_started_at=now - timedelta(minutes=27), work_ended_at=now)
        services.complete(hr, requester)
        earned = PointTransaction.objects.get(user=helper, kind='earned')
        self.assertEqual((earned.amount, earned.counterparty), (30, requester))
        extra = PointTransaction.objects.get(user=requester, kind='settle_extra')
        self.assertEqual(extra.amount, -15)
        # 履歴ページ: 自分の分が見え、他人の分は見えない
        self.client.force_login(requester)
        response = self.client.get(reverse('core:points_history'))
        self.assertContains(response, 'ポイント預かり')
        self.assertNotContains(response, 'お手伝い報酬')
        self.client.force_login(helper)
        response = self.client.get(reverse('core:points_history'))
        self.assertContains(response, 'お手伝い報酬')
        self.assertContains(response, 'hreq')

    def test_refund_recorded_on_cancel(self):
        from .models import PointTransaction
        requester = self._funded('rreq', 50)
        hr = make_request(requester, duration_minutes=20)
        services.publish_request(hr)
        services.cancel(hr, requester)
        refund = PointTransaction.objects.get(user=requester, kind='refund')
        self.assertEqual((refund.amount, refund.balance_after), (20, 50))

    def test_history_requires_login(self):
        response = self.client.get(reverse('core:points_history'))
        self.assertEqual(response.status_code, 302)
