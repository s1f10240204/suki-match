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
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import (ActivityBase, Application, ChatHide, DuplicatePhotoFlag, HelpRequest, Inquiry, InquiryReply, LineChatState, Match, Message, Notification, PresetTag, SupportQuestion,
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


class PeriodicCommandTests(BaseTestCase):
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

    def test_publish_blocked_beyond_credit_limit(self):
        # ツケ導入後は「残高不足」ではなく「ツケ上限(-50pt)を超える」ときだけ弾く
        requester = self._funded('breq', 10)
        hr = make_request(requester, duration_minutes=30)
        services.publish_request(hr)  # 10 - 30 = -20 はツケの範囲内
        requester.refresh_from_db()
        self.assertEqual(requester.points, -20)
        hr2 = make_request(requester, duration_minutes=45)
        with self.assertRaises(services.ServiceError):
            services.publish_request(hr2)  # -20 - 45 = -65 で上限超過
        requester.refresh_from_db()
        hr2.refresh_from_db()
        self.assertEqual((requester.points, hr2.points_cost), (-20, 0))

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


def _tiny_jpeg():
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new('RGB', (10, 10), (200, 180, 160)).save(buf, 'JPEG')
    return buf.getvalue()


@override_settings(PHOTO_REVIEW_TOKEN='test' + '-token')
class PhotoReviewApiTests(BaseTestCase):
    TOKEN = {'HTTP_X_REVIEW_TOKEN': 'test' + '-token'}

    def _pending_user(self, username):
        from django.core.files.uploadedfile import SimpleUploadedFile
        user = make_user(username)
        services.set_photo(user, SimpleUploadedFile('face.jpg', _tiny_jpeg(), content_type='image/jpeg'))
        return user

    def test_digest_counts_pending(self):
        resp = self.client.get(reverse('core:photo_review_digest'))
        self.assertEqual(resp.json()['count'], 0)
        self._pending_user('rev1')
        resp = self.client.get(reverse('core:photo_review_digest'))
        self.assertEqual(resp.json()['count'], 1)
        self.assertEqual(len(resp.json()['digest']), 64)

    def test_pending_requires_token(self):
        self._pending_user('rev2')
        self.assertEqual(self.client.get(reverse('core:photo_review_pending')).status_code, 403)
        self.assertEqual(self.client.get(reverse('core:photo_review_pending'),
                                         HTTP_X_REVIEW_TOKEN='wrong').status_code, 403)
        resp = self.client.get(reverse('core:photo_review_pending'), **self.TOKEN)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([p['username'] for p in resp.json()['pending']], ['rev2'])

    def test_photo_and_decisions(self):
        user = self._pending_user('rev3')
        photo_url = reverse('core:photo_review_photo', args=[user.pk])
        self.assertEqual(self.client.get(photo_url).status_code, 403)
        self.assertEqual(self.client.get(photo_url, **self.TOKEN).content, _tiny_jpeg())
        decision_url = reverse('core:photo_review_decision', args=[user.pk])
        bad = self.client.post(decision_url, data=json.dumps({'decision': 'maybe'}),
                               content_type='application/json', **self.TOKEN)
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(decision_url, data=json.dumps({'decision': 'approve'}),
                              content_type='application/json', **self.TOKEN)
        self.assertEqual(ok.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.avatar_status, User.PhotoStatus.APPROVED)
        # 承認済みの写真も同一人物チェック用に配信される(仕様)。対象外の人は404。
        self.assertEqual(self.client.get(photo_url, **self.TOKEN).status_code, 200)
        outsider = make_user('revoutsider')
        outsider.avatar_status = User.PhotoStatus.NONE
        outsider.save(update_fields=['avatar_status'])
        outsider_url = reverse('core:photo_review_photo', args=[outsider.pk])
        self.assertEqual(self.client.get(outsider_url, **self.TOKEN).status_code, 404)

    def test_reject_with_reason(self):
        user = self._pending_user('rev4')
        resp = self.client.post(reverse('core:photo_review_decision', args=[user.pk]),
                                data=json.dumps({'decision': 'reject', 'reason': '顔が写っていません'}),
                                content_type='application/json', **self.TOKEN)
        self.assertEqual(resp.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.avatar_status, User.PhotoStatus.REJECTED)
        self.assertEqual(user.avatar_reject_reason, '顔が写っていません')

    @override_settings(PHOTO_REVIEW_TOKEN='test' + '-token')
    def test_disabled_without_configured_token(self):
        self._pending_user('rev5')
        resp = self.client.get(reverse('core:photo_review_pending'),
                               HTTP_X_REVIEW_TOKEN='anything')
        self.assertEqual(resp.status_code, 403)


class SecurityFixTests(BaseTestCase):
    """2026-10-31 セキュリティ監査の修正に対する回帰テスト。"""

    def _funded_request(self, name, points=100, duration=15, **extra):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=points)
        requester.refresh_from_db()
        hr = make_request(requester, duration_minutes=duration, **extra)
        services.publish_request(hr)
        requester.refresh_from_db()
        return requester, hr

    def test_complete_twice_does_not_double_pay(self):
        from .models import PointTransaction
        requester, hr = self._funded_request('s1')
        helper = make_user('s1help')
        services.accept_first_come(hr, helper)
        stale = HelpRequest.objects.get(pk=hr.pk)  # 精算前に読み込んだ古いインスタンス
        services.complete(hr, requester)
        helper.refresh_from_db()
        self.assertEqual(helper.points, 15)
        with self.assertRaises(services.ServiceError):
            services.complete(stale, requester)
        helper.refresh_from_db()
        self.assertEqual(helper.points, 15)  # 二重支払いなし
        self.assertEqual(
            PointTransaction.objects.filter(user=helper, kind=PointTransaction.Kind.EARNED).count(), 1)

    def test_refund_only_once(self):
        requester, hr = self._funded_request('s2')
        self.assertEqual(requester.points, 85)
        services.cancel(hr, requester)
        requester.refresh_from_db()
        self.assertEqual(requester.points, 100)
        fresh = HelpRequest.objects.get(pk=hr.pk)
        services._refund_escrow(fresh)  # 別インスタンスから再度返金しても二重にならない
        requester.refresh_from_db()
        self.assertEqual(requester.points, 100)

    def test_charge_is_idempotent(self):
        from .models import PointTransaction
        requester = make_user('s3req')
        User.objects.filter(pk=requester.pk).update(points=100)
        requester.refresh_from_db()
        hr = make_request(requester, duration_minutes=15)
        self.assertEqual(services.charge_for_request(hr), 15)
        self.assertEqual(services.charge_for_request(hr), 15)
        requester.refresh_from_db()
        self.assertEqual(requester.points, 85)
        self.assertEqual(
            PointTransaction.objects.filter(help_request=hr, kind=PointTransaction.Kind.ESCROW).count(), 1)

    def test_helper_cancel_after_expiry_keeps_remaining_work(self):
        requester, hr = self._funded_request('s4', headcount=2)
        helper1, helper2 = make_user('s4h1'), make_user('s4h2')
        services.accept_first_come(hr, helper1)
        services.accept_first_come(hr, helper2)
        HelpRequest.objects.filter(pk=hr.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        hr.refresh_from_db()
        services.cancel(hr, helper1)
        hr.refresh_from_db()
        self.assertEqual(hr.status, HelpRequest.Status.IN_PROGRESS)  # EXPIRED にならない
        requester.refresh_from_db()
        self.assertEqual(requester.points, 85)  # 預かり分は返金されない
        services.complete(hr, requester)
        helper2.refresh_from_db()
        self.assertEqual(helper2.points, 15)  # 残ったヘルパーには預かり分から支払われる

    def test_purge_includes_expired_requests(self):
        requester, hr = self._funded_request('s5', chat_retention_days=0)
        helper = make_user('s5help')
        match = services.accept_first_come(hr, helper)
        services.post_message(match, helper, 'テストメッセージ')
        self.assertEqual(Message.objects.filter(help_request=hr).count(), 1)
        Match.objects.filter(pk=match.pk).update(
            status=Match.Status.CANCELED, canceled_at=timezone.now() - timedelta(days=1))
        HelpRequest.objects.filter(pk=hr.pk).update(status=HelpRequest.Status.EXPIRED)
        services.purge_expired_chat_messages()
        self.assertEqual(Message.objects.filter(help_request=hr).count(), 0)

    def test_auto_sanction_needs_trusted_reporters(self):
        target = make_user('s6victim')
        reporters = [make_user(f's6r{i}', avatar='', avatar_status=User.PhotoStatus.NONE)
                     for i in range(3)]
        for reporter in reporters:
            services.file_report(reporter, target, 'harassment')
        self.assertFalse(Sanction.objects.filter(user=target).exists())  # 未承認の通報だけでは発動しない
        User.objects.filter(pk__in=[r.pk for r in reporters]).update(
            avatar_status=User.PhotoStatus.APPROVED)
        services.file_report(reporters[0], target, 'harassment')
        self.assertTrue(Sanction.objects.filter(user=target).exists())

    def test_oversized_image_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from core import images
        upload = SimpleUploadedFile('big.jpg', _tiny_jpeg(), content_type='image/jpeg')
        with mock.patch.object(images, 'MAX_PIXELS', 50):  # 10x10=100px でも上限超過にする
            with self.assertRaises(images.InvalidImage):
                images.normalize_photo(upload)

    def test_report_page_bad_params_do_not_500(self):
        user = make_user('s8user')
        target = make_user('s8target')
        self.client.force_login(user)
        resp = self.client.get(reverse('core:report_user', args=[target.username]),
                               {'request': 'abc', 'message': 'xyz'})
        self.assertEqual(resp.status_code, 200)

    def test_review_rating_range_and_group_reviewee(self):
        requester, hr = self._funded_request('s9', headcount=2)
        helper1, helper2 = make_user('s9h1'), make_user('s9h2')
        services.accept_first_come(hr, helper1)
        services.accept_first_come(hr, helper2)
        services.complete(hr, requester)
        hr.refresh_from_db()
        with self.assertRaises(services.ServiceError):
            services.submit_review(hr, requester, 100)
        review = services.submit_review(hr, helper2, 5)  # 先頭でないマッチのヘルパーでもレビューできる
        self.assertEqual(review.reviewee, requester)

    def test_reaction_blocked_after_completion(self):
        requester, hr = self._funded_request('s10')
        helper = make_user('s10help')
        match = services.accept_first_come(hr, helper)
        message = services.post_message(match, helper, '写真なし本文')
        services.complete(hr, requester)
        with self.assertRaises(services.ServiceError):
            services.toggle_reaction(message, helper, '❤️')

    def test_push_subscribe_cannot_steal_endpoint(self):
        user_a, user_b = make_user('s11a'), make_user('s11b')
        payload = json.dumps({'endpoint': 'https://push.example/abc',
                              'keys': {'p256dh': 'x', 'auth': 'y'}})
        self.client.force_login(user_a)
        resp = self.client.post(reverse('core:push_subscribe'), data=payload,
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(user_b)
        resp = self.client.post(reverse('core:push_subscribe'), data=payload,
                                content_type='application/json')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(PushSubscription.objects.get(endpoint='https://push.example/abc').user, user_a)

    def test_chat_image_visible_to_group_members_only(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        requester, hr = self._funded_request('s12', headcount=2)
        helper1, helper2 = make_user('s12h1'), make_user('s12h2')
        match1 = services.accept_first_come(hr, helper1)
        services.accept_first_come(hr, helper2)
        message = services.post_message(
            match1, helper1, '', image_file=SimpleUploadedFile('p.jpg', _tiny_jpeg(), content_type='image/jpeg'))
        url = reverse('core:chat_image', args=[message.pk])
        self.client.force_login(helper2)
        self.assertEqual(self.client.get(url).status_code, 200)  # 同じ依頼の別ヘルパーも見られる
        stranger = make_user('s12stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_photo_review_token_non_ascii_header_is_403(self):
        from django.test import RequestFactory
        from core import views
        request = RequestFactory().get('/api/photo-review/pending/',
                                       HTTP_X_REVIEW_TOKEN='ああああ')
        with self.settings(PHOTO_REVIEW_TOKEN='some-token'):
            resp = views.photo_review_pending(request)
        self.assertEqual(resp.status_code, 403)

    def test_work_back_rejects_backslash_redirect(self):
        requester, hr = self._funded_request('s14')
        helper = make_user('s14help')
        match = services.accept_first_come(hr, helper)
        self.client.force_login(helper)
        resp = self.client.post(reverse('core:work_start', args=[match.pk]),
                                {'next': '/\\evil.example'})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil', resp.url)
from .models import PointTransaction, UserBlock


class ScheduleReminderTests(BaseTestCase):
    """日時指定の依頼のリマインダー(前日・1時間前)。"""

    def _setup(self, name, hours_ahead):
        requester = make_user(f'{name}req')
        helper = make_user(f'{name}help')
        hr = make_request(requester, scheduled_for=timezone.now() + timedelta(hours=hours_ahead))
        Match.objects.create(help_request=hr, helper=helper)
        hr.status = HelpRequest.Status.IN_PROGRESS
        hr.save(update_fields=['status'])
        return requester, helper, hr

    def test_day_before_reminder_sent_once(self):
        requester, helper, hr = self._setup('rem1', 23)
        sent = services.send_schedule_reminders()
        self.assertEqual(sent, 2)
        for user in (requester, helper):
            self.assertEqual(
                Notification.objects.filter(user=user, kind='schedule_reminder').count(), 1)
        hr.refresh_from_db()
        self.assertIsNotNone(hr.reminded_day_before_at)
        self.assertIsNone(hr.reminded_hour_before_at)
        # 2回目は送らない
        self.assertEqual(services.send_schedule_reminders(), 0)
        self.assertEqual(Notification.objects.filter(kind='schedule_reminder').count(), 2)

    def test_hour_before_reminder(self):
        requester, helper, hr = self._setup('rem2', 0.5)
        sent = services.send_schedule_reminders()
        self.assertEqual(sent, 2)
        hr.refresh_from_db()
        self.assertIsNotNone(hr.reminded_hour_before_at)
        self.assertIsNone(hr.reminded_day_before_at)  # 1時間を切ったら前日分は送らない
        note = Notification.objects.get(user=requester, kind='schedule_reminder')
        self.assertIn('まもなく', note.text)

    def test_no_reminder_for_immediate_or_canceled(self):
        requester = make_user('rem3req')
        make_request(requester)  # 日時指定なし
        canceled = make_request(requester, scheduled_for=timezone.now() + timedelta(hours=5))
        canceled.status = HelpRequest.Status.CANCELED
        canceled.save(update_fields=['status'])
        self.assertEqual(services.send_schedule_reminders(), 0)
        self.assertEqual(Notification.objects.filter(kind='schedule_reminder').count(), 0)


class TipTests(BaseTestCase):
    """完了後のお礼チップ。"""

    def _completed(self, name, requester_points=100):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=requester_points)
        requester.refresh_from_db()
        hr = make_request(requester, duration_minutes=15)
        services.publish_request(hr)
        helper = make_user(f'{name}help')
        match = services.accept_first_come(hr, helper)
        services.complete(hr, requester)
        requester.refresh_from_db()
        helper.refresh_from_db()
        match.refresh_from_db()
        return requester, hr, helper, match

    def test_tip_moves_points_and_records(self):
        requester, hr, helper, match = self._completed('tip1')
        self.assertEqual((requester.points, helper.points), (85, 15))
        services.tip_helper(match, requester, 10)
        requester.refresh_from_db()
        helper.refresh_from_db()
        self.assertEqual((requester.points, helper.points), (75, 25))
        sent = PointTransaction.objects.get(user=requester, kind='tip_sent')
        self.assertEqual((sent.amount, sent.counterparty_id), (-10, helper.pk))
        got = PointTransaction.objects.get(user=helper, kind='tip_received')
        self.assertEqual((got.amount, got.counterparty_id), (10, requester.pk))
        self.assertTrue(Notification.objects.filter(user=helper, kind='tip').exists())

    def test_tip_rules(self):
        requester, hr, helper, match = self._completed('tip2')
        with self.assertRaises(services.ServiceError):
            services.tip_helper(match, helper, 10)       # 依頼者以外は渡せない
        with self.assertRaises(services.ServiceError):
            services.tip_helper(match, requester, 0)     # 下限
        with self.assertRaises(services.ServiceError):
            services.tip_helper(match, requester, 201)   # 上限
        User.objects.filter(pk=requester.pk).update(points=5)
        with self.assertRaises(services.ServiceError):
            services.tip_helper(match, requester, 10)    # 残高不足
        helper.refresh_from_db()
        self.assertEqual(helper.points, 15)              # 失敗時は動かない

    def test_tip_view(self):
        requester, hr, helper, match = self._completed('tip3')
        self.client.force_login(helper)  # ログインでログインボーナス +2 が入る
        self.client.post(reverse('core:tip_match', args=[match.pk]), {'amount': '10'})
        helper.refresh_from_db()
        self.assertEqual(helper.points, 17)  # 他人のチップは無効(15 + ボーナス2)
        self.client.force_login(requester)
        resp = self.client.post(reverse('core:tip_match', args=[match.pk]), {'amount': '20'})
        self.assertEqual(resp.status_code, 302)
        helper.refresh_from_db()
        self.assertEqual(helper.points, 37)  # 17 + チップ20


class BlockTests(BaseTestCase):
    """ユーザーブロック: マッチング不可・一覧非表示・設定で管理。"""

    def test_block_prevents_matching_both_directions(self):
        requester = make_user('blk1req')
        User.objects.filter(pk=requester.pk).update(points=100)
        helper = make_user('blk1help')
        hr = make_request(requester)
        services.publish_request(hr)
        services.block_user(requester, helper)
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, helper)
        services.unblock_user(requester, helper)
        services.block_user(helper, requester)  # 逆方向でも不可
        with self.assertRaises(services.ServiceError):
            services.accept_first_come(hr, helper)
        services.unblock_user(helper, requester)
        match = services.accept_first_come(hr, helper)  # 解除後は可能
        self.assertIsNotNone(match.pk)

    def test_block_hides_requests_from_list(self):
        requester = make_user('blk2req')
        User.objects.filter(pk=requester.pk).update(points=100)
        hr = make_request(requester)
        hr.title = 'ブロック確認タイトル'
        hr.save(update_fields=['title'])
        services.publish_request(hr)
        viewer = make_user('blk2view')
        self.client.force_login(viewer)
        self.assertContains(self.client.get(reverse('core:help_request_list')), 'ブロック確認タイトル')
        services.block_user(viewer, requester)
        self.assertNotContains(self.client.get(reverse('core:help_request_list')), 'ブロック確認タイトル')

    def test_block_ui_and_settings(self):
        user = make_user('blk3me')
        other = make_user('blk3you')
        self.client.force_login(user)
        resp = self.client.post(reverse('core:block_toggle', args=[other.username]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(UserBlock.objects.filter(blocker=user, blocked=other).exists())
        profile = self.client.get(reverse('core:public_profile', args=[other.username]))
        self.assertContains(profile, 'ブロックを解除する')
        settings_page = self.client.get(reverse('core:account_settings'))
        self.assertContains(settings_page, 'blk3you')
        self.client.post(reverse('core:unblock_user', args=[other.username]))
        self.assertFalse(UserBlock.objects.filter(blocker=user, blocked=other).exists())


class RepostTests(BaseTestCase):
    """過去の依頼の再投稿(内容プリフィル)と投稿フォームの下書き要素。"""

    def test_repost_prefills_form(self):
        user = make_user('repo1')
        hr = make_request(user, duration_minutes=30)
        hr.title = '再投稿テストタイトル'
        hr.save(update_fields=['title'])
        self.client.force_login(user)
        resp = self.client.get(reverse('core:help_request_create') + f'?from={hr.pk}')
        self.assertContains(resp, '再投稿テストタイトル')
        self.assertContains(resp, '前回の依頼内容をコピーしました')
        self.assertContains(resp, 'data-draft-note')

    def test_repost_ignores_others_requests(self):
        owner = make_user('repo2owner')
        hr = make_request(owner)
        hr.title = '他人の依頼タイトル'
        hr.save(update_fields=['title'])
        other = make_user('repo2other')
        self.client.force_login(other)
        resp = self.client.get(reverse('core:help_request_create') + f'?from={hr.pk}')
        self.assertNotContains(resp, '他人の依頼タイトル')


class RatingVisibilityTests(BaseTestCase):
    """評価の平均・件数が一覧カードと詳細に出る。"""

    def _reviewed(self, name):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=100)
        requester.refresh_from_db()
        hr = make_request(requester, duration_minutes=15)
        services.publish_request(hr)
        helper = make_user(f'{name}help')
        match = services.accept_first_come(hr, helper)
        services.complete(hr, requester)
        hr.refresh_from_db()
        services.submit_review(hr, helper, 5)     # ヘルパー→依頼者 ★5
        services.submit_review(hr, requester, 4)  # 依頼者→ヘルパー ★4
        return requester, hr, helper

    def test_rating_on_detail(self):
        requester, hr, helper = self._reviewed('rate1')
        viewer = make_user('rate1view')
        self.client.force_login(viewer)
        resp = self.client.get(reverse('core:help_request_detail', args=[hr.pk]))
        self.assertContains(resp, '★5.0（1件の評価）')  # 依頼者
        self.assertContains(resp, '★4.0（1件）')        # ヘルパー

    def test_rating_on_list_card(self):
        requester, hr, helper = self._reviewed('rate2')
        new_hr = make_request(requester)
        services.publish_request(new_hr)
        viewer = make_user('rate2view')
        self.client.force_login(viewer)
        resp = self.client.get(reverse('core:help_request_list'))
        self.assertContains(resp, '★5.0(1)')
class StampTests(BaseTestCase):
    """チャットのスタンプ送信(LINEスタンプ風)。"""

    def _setup(self, name):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=100)
        requester.refresh_from_db()
        hr = make_request(requester)
        services.publish_request(hr)
        helper = make_user(f'{name}help')
        match = services.accept_first_come(hr, helper)
        return requester, hr, helper, match

    def test_post_stamp_message(self):
        requester, hr, helper, match = self._setup('st1')
        message = services.post_message(match, helper, '', stamp='thanks')
        self.assertEqual(message.stamp, 'thanks')
        self.assertEqual(message.body, '')
        payload = services.message_payload(message)
        self.assertEqual(payload['stamp'], 'thanks')
        self.assertEqual(payload['stamp_emoji'], '🙏')
        self.assertEqual(payload['stamp_label'], 'ありがとう')
        self.assertEqual(services.reply_excerpt(message), '🙏 スタンプ')

    def test_invalid_stamp_rejected(self):
        requester, hr, helper, match = self._setup('st2')
        with self.assertRaises(services.ServiceError):
            services.post_message(match, helper, '', stamp='not-a-stamp')
        with self.assertRaises(services.ServiceError):
            services.post_message(match, helper, '')  # 本文もスタンプも無いのは不可

    def test_stamp_via_chat_page(self):
        requester, hr, helper, match = self._setup('st3')
        self.client.force_login(helper)
        resp = self.client.post(reverse('core:chat', args=[match.pk]),
                                {'body': '', 'stamp': 'party'})
        self.assertEqual(resp.status_code, 302)
        message = hr.messages.get()
        self.assertEqual(message.stamp, 'party')
        page = self.client.get(reverse('core:chat', args=[match.pk]))
        self.assertContains(page, 'data-stamp="thanks"')   # スタンプパネル
        self.assertContains(page, '🎉')                    # 送信済みスタンプの描画
        self.assertContains(page, 'おめでとう')
class MessageGroupingTests(BaseTestCase):
    """メッセージ一覧を相手ごとにまとめる(案A)。"""

    def _two_chats(self, name):
        requester = make_user(f'{name}req')
        User.objects.filter(pk=requester.pk).update(points=200)
        requester.refresh_from_db()
        hr1 = make_request(requester)
        hr1.title = '買い物代行'
        hr1.save(update_fields=['title'])
        services.publish_request(hr1)
        hr2 = make_request(requester)
        hr2.title = '水戸に一緒に行きたい'
        hr2.save(update_fields=['title'])
        services.publish_request(hr2)
        helper = make_user(f'{name}help')
        m1 = services.accept_first_come(hr1, helper)
        m2 = services.accept_first_come(hr2, helper)
        return requester, hr1, hr2, helper, m1, m2

    def test_list_groups_by_person(self):
        requester, hr1, hr2, helper, m1, m2 = self._two_chats('grp1')
        services.post_message(m1, requester, 'よろしくお願いします')
        self.client.force_login(helper)
        resp = self.client.get(reverse('core:message_list'))
        self.assertContains(resp, '2件のトーク')
        self.assertContains(resp, reverse('core:message_person', args=[requester.username]))
        self.assertContains(resp, '<span class="unread-badge">1</span>')
        # 相手別ページに依頼ごとのトークが並ぶ
        page = self.client.get(reverse('core:message_person', args=[requester.username]))
        self.assertContains(page, '買い物代行')
        self.assertContains(page, '水戸に一緒に行きたい')
        self.assertContains(page, reverse('core:chat', args=[m1.pk]))
        self.assertContains(page, reverse('core:chat', args=[m2.pk]))

    def test_requester_side_also_grouped(self):
        requester, hr1, hr2, helper, m1, m2 = self._two_chats('grp2')
        self.client.force_login(requester)
        resp = self.client.get(reverse('core:message_list'))
        self.assertContains(resp, '2件のトーク')
        page = self.client.get(reverse('core:message_person', args=[helper.username]))
        self.assertContains(page, '買い物代行')

    def test_person_page_404_without_chats(self):
        requester, hr1, hr2, helper, m1, m2 = self._two_chats('grp3')
        stranger = make_user('grp3stranger')
        self.client.force_login(helper)
        resp = self.client.get(reverse('core:message_person', args=[stranger.username]))
        self.assertEqual(resp.status_code, 404)

    def test_single_chat_redirects_straight_to_chat(self):
        requester = make_user('grp4req')
        User.objects.filter(pk=requester.pk).update(points=100)
        requester.refresh_from_db()
        hr = make_request(requester)
        services.publish_request(hr)
        helper = make_user('grp4help')
        match = services.accept_first_come(hr, helper)
        self.client.force_login(helper)
        resp = self.client.get(reverse('core:message_list'))
        self.assertRedirects(resp, reverse('core:chat', args=[match.pk]), fetch_redirect_response=False)

    def test_group_chat_stays_as_request_row(self):
        requester = make_user('grp5req')
        User.objects.filter(pk=requester.pk).update(points=300)
        requester.refresh_from_db()
        group_hr = make_request(requester, headcount=2)
        group_hr.title = '引っ越し手伝い'
        group_hr.save(update_fields=['title'])
        services.publish_request(group_hr)
        solo_hr = make_request(requester)
        services.publish_request(solo_hr)
        h1, h2 = make_user('grp5h1'), make_user('grp5h2')
        services.accept_first_come(group_hr, h1)
        services.accept_first_come(group_hr, h2)
        services.accept_first_come(solo_hr, h1)
        self.client.force_login(requester)
        resp = self.client.get(reverse('core:message_list'))
        self.assertContains(resp, 'グループ（2人募集）')
        self.assertContains(resp, '引っ越し手伝い')
class PointsEarnTests(BaseTestCase):
    """ツケ・招待ボーナス・ミッション・ログインボーナス。"""

    # ---- ツケ ----
    def test_credit_allows_posting_below_zero(self):
        user = make_user('crd1')
        User.objects.filter(pk=user.pk).update(points=30)
        user.refresh_from_db()
        hr = make_request(user, duration_minutes=45)
        services.publish_request(hr)  # 45pt 使って -15pt になる
        user.refresh_from_db()
        self.assertEqual(user.points, -15)

    def test_credit_limit_blocks_beyond_minus_50(self):
        user = make_user('crd2')
        User.objects.filter(pk=user.pk).update(points=0)
        user.refresh_from_db()
        hr = make_request(user, duration_minutes=60)
        with self.assertRaises(services.ServiceError):
            services.publish_request(hr)  # -60 で上限超過
        hr2 = make_request(user, duration_minutes=45)
        services.publish_request(hr2)  # -45 は OK
        user.refresh_from_db()
        self.assertEqual(user.points, -45)
        hr3 = make_request(user, duration_minutes=15)
        with self.assertRaises(services.ServiceError):
            services.publish_request(hr3)  # -60 になるので不可

    # ---- 招待ボーナス ----
    def _invitee(self, name, referrer):
        invitee = make_user(name)
        User.objects.filter(pk=invitee.pk).update(referred_by=referrer)
        invitee.refresh_from_db()
        return invitee

    def test_referral_reward_after_photo_approval(self):
        referrer = make_user('ref1src')
        User.objects.filter(pk=referrer.pk).update(points=100)
        referrer.refresh_from_db()
        invitee = self._invitee('ref1dst', referrer)
        # 写真が審査中(make_user の既定は承認済みなので明示的に戻す)では付与されない
        User.objects.filter(pk=invitee.pk).update(avatar_status=User.PhotoStatus.PENDING)
        invitee.refresh_from_db()
        self.assertEqual(services.process_referral_rewards(), 0)
        User.objects.filter(pk=invitee.pk).update(avatar='avatars/x.jpg',
                                                  avatar_status=User.PhotoStatus.APPROVED)
        invitee.refresh_from_db()
        self.assertEqual(services.process_referral_rewards(), 1)
        referrer.refresh_from_db()
        self.assertEqual(referrer.points, 130)
        tx = referrer.point_transactions.get(kind=PointTransaction.Kind.REFERRAL)
        self.assertEqual(tx.counterparty, invitee)
        # 二重付与なし
        self.assertEqual(services.process_referral_rewards(), 0)

    def test_referral_reward_for_exempt_user(self):
        from allauth.account.models import EmailAddress
        referrer = make_user('ref2src')
        invitee = self._invitee('ref2dst', referrer)
        EmailAddress.objects.create(user=invitee, email='s1f999999999@iniad.org',
                                    verified=True, primary=True)
        self.assertTrue(services.grant_referral_reward(invitee))
        referrer.refresh_from_db()
        self.assertEqual(referrer.points, 30)

    def test_signup_records_referrer(self):
        referrer = make_user('ref3src')
        self.client.get(reverse('account_signup') + f'?ref={referrer.username}')
        resp = self.client.post(reverse('account_signup'), {
            'username': 'ref3new', 'email': 'ref3new@example.com',
            'password1': 'V3ry-Str0ng-Pass!', 'password2': 'V3ry-Str0ng-Pass!',
            'birth_date': '2000-01-01',
        })
        new_user = User.objects.get(username='ref3new')
        self.assertEqual(new_user.referred_by, referrer)

    # ---- ミッション ----
    def test_profile_mission_claim(self):
        user = make_user('mis1')
        skill = Skill.objects.create(name='ミッション用スキル')
        user.skills.add(skill)
        User.objects.filter(pk=user.pk).update(bio='よろしくお願いします')
        user.refresh_from_db()
        self.assertEqual(services.claim_mission(user, 'profile'), 10)
        user.refresh_from_db()
        self.assertEqual(user.points, 10)
        with self.assertRaises(services.ServiceError):
            services.claim_mission(user, 'profile')  # 二重受け取り不可

    def test_unachieved_mission_cannot_claim(self):
        user = make_user('mis2')
        with self.assertRaises(services.ServiceError):
            services.claim_mission(user, 'first_help')

    def test_first_help_mission_after_completion(self):
        requester = make_user('mis3req')
        User.objects.filter(pk=requester.pk).update(points=100)
        requester.refresh_from_db()
        hr = make_request(requester)
        services.publish_request(hr)
        helper = make_user('mis3help')
        match = services.accept_first_come(hr, helper)
        services.complete(hr, requester)
        self.assertEqual(services.claim_mission(helper, 'first_help'), 10)
        self.assertEqual(services.claim_mission(requester, 'first_request'), 10)

    # ---- ログインボーナス ----
    def test_login_bonus_once_per_day(self):
        user = make_user('lgb1')
        self.assertTrue(services.grant_login_bonus(user))
        self.assertFalse(services.grant_login_bonus(user))
        user.refresh_from_db()
        self.assertEqual(user.points, 2)

    def test_login_signal_grants_bonus(self):
        user = User.objects.create_user(username='lgb2', password='V3ry-Str0ng-Pass!')
        self.assertTrue(self.client.login(username='lgb2', password='V3ry-Str0ng-Pass!'))
        user.refresh_from_db()
        self.assertEqual(user.points, 2)
        self.assertEqual(user.last_login_bonus_date, timezone.localdate())
        self.client.logout()
        self.assertTrue(self.client.login(username='lgb2', password='V3ry-Str0ng-Pass!'))
        user.refresh_from_db()
        self.assertEqual(user.points, 2)  # 同日2回目は付与されない

    # ---- ポイントページ ----
    def test_points_page_shows_earn_hub(self):
        user = make_user('hub1')
        self.client.force_login(user)
        resp = self.client.get(reverse('core:points_history'))
        self.assertContains(resp, 'ポイントを貯める')
        user.refresh_from_db()
        self.assertContains(resp, f'?ref={user.invite_code}')
        self.assertContains(resp, 'ミッション')
        self.assertContains(resp, 'ログインボーナス')
class InviteCodeTests(BaseTestCase):
    """招待コード(1人1コード、登録から48時間以内に入力で紹介成立)。"""

    def test_code_generated_once_and_unique(self):
        u1, u2 = make_user('inv1a'), make_user('inv1b')
        c1 = services.ensure_invite_code(u1)
        self.assertEqual(len(c1), 8)
        self.assertEqual(services.ensure_invite_code(u1), c1)  # 変わらない
        self.assertNotEqual(services.ensure_invite_code(u2), c1)

    def test_register_within_48h_grants_when_gate_passed(self):
        referrer = make_user('inv2src')
        code = services.ensure_invite_code(referrer)
        invitee = make_user('inv2dst')  # make_user は写真承認済み＝参加条件クリア
        got = services.register_invite_code(invitee, code.lower())  # 小文字でもOK
        self.assertEqual(got, referrer)
        invitee.refresh_from_db()
        self.assertEqual(invitee.referred_by, referrer)
        referrer.refresh_from_db()
        self.assertEqual(referrer.points, 30)  # その場で +30

    def test_register_after_48h_rejected(self):
        referrer = make_user('inv3src')
        code = services.ensure_invite_code(referrer)
        invitee = make_user('inv3dst')
        User.objects.filter(pk=invitee.pk).update(
            date_joined=timezone.now() - timedelta(days=3))
        invitee.refresh_from_db()
        with self.assertRaises(services.ServiceError):
            services.register_invite_code(invitee, code)

    def test_register_rejects_own_unknown_and_duplicate(self):
        referrer = make_user('inv4src')
        code = services.ensure_invite_code(referrer)
        with self.assertRaises(services.ServiceError):
            services.register_invite_code(referrer, code)       # 自分のコード
        invitee = make_user('inv4dst')
        with self.assertRaises(services.ServiceError):
            services.register_invite_code(invitee, 'ZZZZ9999')  # 存在しない
        services.register_invite_code(invitee, code)
        other = make_user('inv4oth')
        services.ensure_invite_code(other)
        with self.assertRaises(services.ServiceError):
            services.register_invite_code(invitee, other.invite_code)  # 二重登録

    def test_signup_link_with_code_records_referrer(self):
        referrer = make_user('inv5src')
        code = services.ensure_invite_code(referrer)
        self.client.get(reverse('account_signup') + f'?ref={code}')
        self.client.post(reverse('account_signup'), {
            'username': 'inv5new', 'email': 'inv5new@example.com',
            'password1': 'V3ry-Str0ng-Pass!', 'password2': 'V3ry-Str0ng-Pass!',
            'birth_date': '2000-01-01',
        })
        new_user = User.objects.get(username='inv5new')
        self.assertEqual(new_user.referred_by, referrer)

    def test_profile_shows_code_and_entry_form(self):
        referrer = make_user('inv6src')
        code = services.ensure_invite_code(referrer)
        invitee = make_user('inv6dst')
        self.client.force_login(invitee)
        resp = self.client.get(reverse('core:profile'))
        self.assertContains(resp, '友達を招待して')
        self.assertContains(resp, '招待コードをお持ちの方はこちら')
        resp = self.client.post(reverse('core:register_invite_code'), {'code': code})
        self.assertEqual(resp.status_code, 302)
        invitee.refresh_from_db()
        self.assertEqual(invitee.referred_by, referrer)
        resp = self.client.get(reverse('core:profile'))
        self.assertNotContains(resp, '招待コードをお持ちの方はこちら')
        self.assertContains(resp, 'さんの招待で参加しました')
def _structured_photo(kind):
    """指紋テスト用の模様つき画像(JPEG bytes)。"""
    from PIL import Image, ImageDraw
    if kind == 'a':
        img = Image.new('RGB', (64, 64), (30, 30, 30))
        d = ImageDraw.Draw(img)
        d.rectangle([32, 0, 63, 63], fill=(220, 220, 220))
        d.ellipse([8, 8, 24, 24], fill=(255, 255, 255))
    else:
        img = Image.new('RGB', (64, 64), (220, 220, 220))
        d = ImageDraw.Draw(img)
        d.rectangle([32, 0, 63, 63], fill=(30, 30, 30))
        d.rectangle([0, 40, 63, 63], fill=(120, 120, 120))
        d.ellipse([40, 8, 56, 24], fill=(0, 0, 0))
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=92)
    return buf.getvalue()


def _reencode(data, quality=55, scale=0.7):
    from PIL import Image
    img = Image.open(BytesIO(data))
    if scale != 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return buf.getvalue()


def _upload(data):
    return images.normalize_photo(
        SimpleUploadedFile('face.jpg', data, content_type='image/jpeg'))


class PhotoDuplicateTests(BaseTestCase):
    """同じ写真の別アカウント流用を指紋で検知して自動却下+フラグ。"""

    def test_signature_near_for_same_far_for_different(self):
        a = _structured_photo('a')
        sig_a = images.photo_signature(a)
        self.assertTrue(sig_a)
        d, a_dist = images.signature_distance(sig_a, images.photo_signature(_reencode(a)))
        self.assertLessEqual(d, services.DUPLICATE_DHASH_MAX)
        self.assertLessEqual(a_dist, services.DUPLICATE_AHASH_MAX)
        d2, a2 = images.signature_distance(sig_a, images.photo_signature(_structured_photo('b')))
        self.assertTrue(d2 > services.DUPLICATE_DHASH_MAX or a2 > services.DUPLICATE_AHASH_MAX)

    def test_duplicate_upload_auto_rejected_and_flagged(self):
        admin = User.objects.create_superuser('dupadmin', password='x')
        user_a = make_user('dup1a')
        result = services.set_photo(user_a, _upload(_structured_photo('a')))
        self.assertEqual(result, 'pending')
        user_b = make_user('dup1b')
        result = services.set_photo(user_b, _upload(_reencode(_structured_photo('a'))))
        self.assertEqual(result, 'duplicate')
        user_b.refresh_from_db()
        self.assertEqual(user_b.avatar_status, User.PhotoStatus.REJECTED)
        self.assertIn('別のアカウント', user_b.avatar_reject_reason)
        flag = DuplicatePhotoFlag.objects.get(user=user_b)
        self.assertEqual(flag.matched_user, user_a)
        self.assertTrue(Notification.objects.filter(user=user_b, kind='photo').exists())
        self.assertTrue(Notification.objects.filter(user=admin, kind='photo_duplicate').exists())
        # 本人は影響なし / 別の写真なら審査待ちに戻る
        user_a.refresh_from_db()
        self.assertEqual(user_a.avatar_status, User.PhotoStatus.PENDING)
        result = services.set_photo(user_b, _upload(_structured_photo('b')))
        self.assertEqual(result, 'pending')

    def test_own_reupload_is_not_duplicate(self):
        user = make_user('dup2a')
        services.set_photo(user, _upload(_structured_photo('a')))
        result = services.set_photo(user, _upload(_reencode(_structured_photo('a'))))
        self.assertEqual(result, 'pending')
        self.assertFalse(DuplicatePhotoFlag.objects.exists())

    @override_settings(PHOTO_REVIEW_TOKEN='test' + '-token')
    def test_flags_api_and_digest(self):
        user_a = make_user('dup3a')
        services.set_photo(user_a, _upload(_structured_photo('a')))
        user_b = make_user('dup3b')
        services.set_photo(user_b, _upload(_structured_photo('a')))
        flag = DuplicatePhotoFlag.objects.get(user=user_b)
        headers = {'HTTP_X_REVIEW_TOKEN': 'test' + '-token'}
        resp = self.client.get(reverse('core:photo_review_digest'))
        self.assertEqual(resp.json()['flags'], 1)
        resp = self.client.get(reverse('core:photo_review_flags'), **headers)
        self.assertEqual(resp.json()['flags'][0]['flag_id'], flag.pk)
        self.assertEqual(resp.json()['flags'][0]['matched_username'], user_a.username)
        # フラグ対象者の写真は報告確認用に取得できる
        resp = self.client.get(reverse('core:photo_review_photo', args=[user_b.pk]), **headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse('core:photo_review_flag_ack', args=[flag.pk]), **headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(reverse('core:photo_review_digest'))
        self.assertEqual(resp.json()['flags'], 0)
        # 承認済み一覧(同一人物チェック用)
        services.review_photo(user_a, True)
        resp = self.client.get(reverse('core:photo_review_approved'), **headers)
        self.assertEqual([u['username'] for u in resp.json()['approved']], [user_a.username])
class StaffSwitchTests(BaseTestCase):
    """運営のアカウント切替(管理バー/テスト表示)。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('boss', password='x')
        self.target = make_user('switchtarget')

    def test_picker_forbidden_for_normal_user(self):
        self.client.force_login(self.target)
        resp = self.client.get(reverse('core:staff_switch'))
        self.assertEqual(resp.status_code, 403)

    def test_switch_roundtrip(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('core:staff_switch'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'switchtarget')
        points_before = User.objects.get(pk=self.target.pk).points
        resp = self.client.post(reverse('core:staff_switch_to', args=[self.target.pk]))
        self.assertRedirects(resp, reverse('core:help_request_list'))
        # テスト表示中バナーが出て、切替先として振る舞う
        resp = self.client.get(reverse('core:profile'))
        self.assertContains(resp, 'テスト表示中')
        self.assertContains(resp, 'switchtarget')
        # 切替中は一覧を開けない/再切替できない
        self.assertEqual(self.client.get(reverse('core:staff_switch')).status_code, 403)
        # ログイン扱いにならないのでログインボーナスは付かない
        self.assertEqual(User.objects.get(pk=self.target.pk).points, points_before)
        # 運営への通知が残る
        self.assertTrue(Notification.objects.filter(user=self.admin, kind='staff_switch').exists())
        # 戻ると管理者に戻る
        self.client.post(reverse('core:staff_switch_back'))
        resp = self.client.get(reverse('core:staff_switch'))
        self.assertEqual(resp.status_code, 200)

    def test_admin_bar_only_for_staff(self):
        self.client.force_login(self.target)
        resp = self.client.get(reverse('core:help_request_list'))
        self.assertNotContains(resp, 'アカウント切替')
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('core:help_request_list'))
        self.assertContains(resp, 'アカウント切替')
        self.assertContains(resp, '管理画面')

    def test_cannot_switch_to_superuser(self):
        other_admin = User.objects.create_superuser('boss2', password='x')
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('core:staff_switch_to', args=[other_admin.pk]))
        self.assertRedirects(resp, reverse('core:staff_switch'))
        self.assertEqual(self.client.get(reverse('core:staff_switch')).status_code, 200)

    def test_switch_requires_post(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('core:staff_switch_to', args=[self.target.pk]))
        self.assertEqual(resp.status_code, 405)
class RequestPhotoTests(BaseTestCase):
    """依頼への写真添付。"""

    def setUp(self):
        super().setUp()
        self.user = make_user('photoreq')
        User.objects.filter(pk=self.user.pk).update(points=1000)
        self.client.force_login(self.user)

    def _payload(self, **data):
        payload = {'title': '蛇口の水漏れ', 'mode': 'first_come', 'description': '直してほしい',
                   'duration_minutes': 30, 'listing_minutes': 30,
                   'latitude': LAT, 'longitude': LNG, 'skill_names': ''}
        payload.update(data)
        return payload

    def test_create_with_photo_and_display(self):
        resp = self.client.post(reverse('core:help_request_create'),
                                self._payload(image=jpeg_upload()))
        hr = HelpRequest.objects.get()
        self.assertRedirects(resp, reverse('core:help_request_detail', args=[hr.pk]))
        self.assertTrue(hr.image)
        detail = self.client.get(reverse('core:help_request_detail', args=[hr.pk]))
        self.assertContains(detail, 'detail-photo')
        listing = self.client.get(reverse('core:help_request_list'))
        self.assertContains(listing, 'card-photo')
        img = self.client.get(reverse('core:request_image', args=[hr.pk]))
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img['Content-Type'], 'image/jpeg')

    def test_create_without_photo(self):
        resp = self.client.post(reverse('core:help_request_create'), self._payload())
        hr = HelpRequest.objects.get()
        self.assertFalse(hr.image)
        detail = self.client.get(reverse('core:help_request_detail', args=[hr.pk]))
        self.assertNotContains(detail, 'detail-photo')
        img = self.client.get(reverse('core:request_image', args=[hr.pk]))
        self.assertEqual(img.status_code, 404)

    def test_image_endpoint_requires_login(self):
        self.client.post(reverse('core:help_request_create'), self._payload(image=jpeg_upload()))
        hr = HelpRequest.objects.get()
        self.client.logout()
        resp = self.client.get(reverse('core:request_image', args=[hr.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_invalid_image_rejected(self):
        bad = SimpleUploadedFile('memo.txt', b'not an image', content_type='text/plain')
        resp = self.client.post(reverse('core:help_request_create'), self._payload(image=bad))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(HelpRequest.objects.exists())
class SupportTests(BaseTestCase):
    """FAQ / サポートbot / お問い合わせ / 自動応答API。"""

    def setUp(self):
        super().setUp()
        self.user = make_user('supporter')
        self.client.force_login(self.user)

    def test_faq_page_public(self):
        self.client.logout()
        resp = self.client.get(reverse('core:faq'))
        self.assertContains(resp, 'ポイントの仕組みを教えてください')

    def test_inquiry_link_always_visible(self):
        url = reverse('core:inquiry_create')
        resp = self.client.get(reverse('core:support'))
        self.assertContains(resp, url)
        self.assertContains(resp, 'inquiry-cta')
        self.client.logout()
        resp = self.client.get(reverse('core:faq'))
        self.assertContains(resp, url)

    def test_bot_answers_points_question(self):
        resp = self.client.post(reverse('core:support_ask'),
                                data=json.dumps({'question': 'ポイントが足りなくて困ってます'}),
                                content_type='application/json')
        data = resp.json()
        self.assertTrue(data['matched'])
        self.assertEqual(data['faq_id'], 'points-not-enough')
        self.assertTrue(SupportQuestion.objects.filter(matched_faq='points-not-enough').exists())

    def test_bot_fallback_logs_unmatched(self):
        resp = self.client.post(reverse('core:support_ask'),
                                data=json.dumps({'question': 'あいうえおZZZ'}),
                                content_type='application/json')
        self.assertFalse(resp.json()['matched'])
        log = SupportQuestion.objects.get()
        self.assertEqual(log.matched_faq, '')

    def test_inquiry_flow_and_privacy(self):
        resp = self.client.post(reverse('core:inquiry_create'), {'body': '依頼の出し方が分かりません'})
        inquiry = Inquiry.objects.get()
        self.assertRedirects(resp, reverse('core:inquiry_detail', args=[inquiry.pk]))
        detail = self.client.get(reverse('core:inquiry_detail', args=[inquiry.pk]))
        self.assertContains(detail, '未対応')
        other = make_user('supportother')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('core:inquiry_detail', args=[inquiry.pk])).status_code, 404)

    @override_settings(PHOTO_REVIEW_TOKEN='test' + '-token')
    def test_inquiry_api_reply_and_escalate(self):
        headers = {'HTTP_X_REVIEW_TOKEN': 'test' + '-token'}
        q1 = Inquiry.objects.create(user=self.user, body='ポイントって何ですか')
        q2 = Inquiry.objects.create(user=self.user, body='個別の調査が要る話')
        resp = self.client.get(reverse('core:support_inquiries_pending'))
        self.assertEqual(resp.status_code, 403)
        resp = self.client.get(reverse('core:support_inquiries_pending'), **headers)
        self.assertEqual(len(resp.json()['inquiries']), 2)
        resp = self.client.post(reverse('core:support_inquiry_reply', args=[q1.pk]),
                                data=json.dumps({'body': 'ポイントはありがとうの印です'}),
                                content_type='application/json', **headers)
        self.assertEqual(resp.status_code, 200)
        q1.refresh_from_db()
        self.assertEqual(q1.status, Inquiry.Status.ANSWERED)
        self.assertEqual(q1.replies.get().author_kind, InquiryReply.Author.BOT)
        self.assertTrue(Notification.objects.filter(user=self.user, kind='inquiry_reply').exists())
        resp = self.client.post(reverse('core:support_inquiry_escalate', args=[q2.pk]), **headers)
        self.assertEqual(resp.status_code, 200)
        q2.refresh_from_db()
        self.assertEqual(q2.status, Inquiry.Status.ESCALATED)
        digest = self.client.get(reverse('core:photo_review_digest')).json()
        self.assertEqual(digest['inquiries'], 0)

    def test_reply_badges_distinguish_ai_and_operator(self):
        inquiry = Inquiry.objects.create(user=self.user, body='質問です')
        InquiryReply.objects.create(inquiry=inquiry, author_kind=InquiryReply.Author.BOT, body='AIの回答')
        InquiryReply.objects.create(inquiry=inquiry, author_kind=InquiryReply.Author.OPERATOR, body='運営の回答')
        resp = self.client.get(reverse('core:inquiry_detail', args=[inquiry.pk]))
        self.assertContains(resp, 'AIが確認して返信')
        self.assertContains(resp, '運営が確認して返信')
        self.assertContains(resp, 'reply-badge-ai')
        self.assertContains(resp, 'reply-badge-op')

    def test_user_followup_reopens_inquiry(self):
        inquiry = Inquiry.objects.create(user=self.user, body='質問です', status=Inquiry.Status.ANSWERED)
        self.client.post(reverse('core:inquiry_detail', args=[inquiry.pk]), {'body': '追加の質問です'})
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, Inquiry.Status.OPEN)
        self.assertEqual(inquiry.replies.get().author_kind, InquiryReply.Author.USER)
class MessageHideTests(BaseTestCase):
    """チャットの「自分の画面から消す」(相手には残る)。"""

    def setUp(self):
        super().setUp()
        self.requester = make_user('hidereq')
        User.objects.filter(pk=self.requester.pk).update(points=100)
        self.requester.refresh_from_db()
        self.req = make_request(self.requester)
        services.publish_request(self.req)
        self.helper = make_user('hidehelp')
        self.match = services.accept_first_come(self.req, self.helper)
        self.msg = services.post_message(self.match, self.helper, '消したいメッセージです')

    def test_hide_for_self_only(self):
        self.client.force_login(self.requester)
        resp = self.client.post(reverse('core:message_hide', args=[self.msg.pk]))
        self.assertEqual(resp.status_code, 200)
        page = self.client.get(reverse('core:chat', args=[self.match.pk]))
        self.assertNotContains(page, '消したいメッセージです')
        self.client.force_login(self.helper)
        page = self.client.get(reverse('core:chat', args=[self.match.pk]))
        self.assertContains(page, '消したいメッセージです')

    def test_chat_page_shows_long_press_hint(self):
        self.client.force_login(self.requester)
        page = self.client.get(reverse('core:chat', args=[self.match.pk]))
        self.assertContains(page, 'id="chat-hint"')
        self.assertContains(page, '自分の画面から消す')

    def test_hide_requires_participant(self):
        outsider = make_user('hideoutsider')
        self.client.force_login(outsider)
        resp = self.client.post(reverse('core:message_hide', args=[self.msg.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_hidden_message_excluded_from_preview(self):
        services.post_message(self.match, self.helper, '最新のメッセージ')
        latest = services.post_message(self.match, self.requester, '消すとプレビューが変わる')
        latest.hidden_by.add(self.requester)
        chats = services.open_chats(self.requester)
        from .views import _annotate_chats
        _annotate_chats(self.requester, chats)
        target = next(c for c in chats if c.pk == self.match.pk)
        self.assertEqual(target.last_preview, '最新のメッセージ')
class TalkHideTests(BaseTestCase):
    """トーク単位の非表示(自分のメッセージ一覧からだけ消える)。"""

    def setUp(self):
        super().setUp()
        self.requester = make_user('talkreq')
        User.objects.filter(pk=self.requester.pk).update(points=100)
        self.requester.refresh_from_db()
        self.req = make_request(self.requester)
        services.publish_request(self.req)
        self.helper = make_user('talkhelp')
        self.match = services.accept_first_come(self.req, self.helper)
        services.post_message(self.match, self.helper, '最初のメッセージ')

    def test_hide_talk_for_self_only(self):
        self.client.force_login(self.requester)
        resp = self.client.post(reverse('core:talk_hide', args=[self.req.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(services.filter_visible_talks(self.requester, services.open_chats(self.requester)), [])
        self.assertEqual(len(services.filter_visible_talks(self.helper, services.open_chats(self.helper))), 1)
        # 一覧ページもチャット直行ではなくホームへ戻る
        resp = self.client.get(reverse('core:message_list'))
        self.assertEqual(resp.status_code, 302)

    def test_new_message_revives_talk(self):
        ChatHide.objects.create(user=self.requester, help_request=self.req)
        self.assertEqual(services.filter_visible_talks(self.requester, services.open_chats(self.requester)), [])
        services.post_message(self.match, self.helper, '新しいメッセージです')
        self.assertEqual(len(services.filter_visible_talks(self.requester, services.open_chats(self.requester))), 1)

    def test_hide_requires_participant(self):
        outsider = make_user('talkoutsider')
        self.client.force_login(outsider)
        resp = self.client.post(reverse('core:talk_hide', args=[self.req.pk]))
        self.assertEqual(resp.status_code, 404)
