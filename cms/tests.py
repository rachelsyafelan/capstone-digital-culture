from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attribute, Event


def _tiny_image():
    # 1×1 transparent GIF — smallest valid image payload for upload tests.
    content = (
        b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04'
        b'\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;'
    )
    return SimpleUploadedFile('tiny.gif', content, content_type='image/gif')


def _login_as_admin(client):
    session = client.session
    session['is_admin'] = True
    session.save()


class EventPublishLifecycleGateTests(TestCase):
    """
    Event can only go to Published once every required field is filled in
    (Capacity & PIC excluded — always optional). If the admin explicitly
    requests Published while something required is missing, that's now a
    hard validation error: nothing is saved/changed, and the admin is told
    what to fill in. Leaving status as Draft never triggers this check.
    """

    def _complete_payload(self, **overrides):
        payload = {
            'event_name': 'Digital Culture Summit',
            'description': 'Deskripsi lengkap event.',
            'location': 'Jakarta',
            'event_date': date.today().isoformat(),
            'event_time': '09:00',
            'end_time': '17:00',
            'status': Event.STATUS_PUBLISHED,
        }
        payload.update(overrides)
        return payload

    def test_missing_required_field_blocks_publish_on_create(self):
        _login_as_admin(self.client)
        payload = self._complete_payload()
        del payload['location']  # location left blank -> incomplete
        payload['image'] = _tiny_image()

        response = self.client.post(reverse('admin_add_event'), payload)

        # Nothing gets created at all — Publish is rejected outright.
        self.assertFalse(Event.objects.filter(event_name='Digital Culture Summit').exists())
        self.assertContains(response, 'tidak bisa dipublish')
        self.assertContains(response, 'Lokasi')

    def test_complete_fields_allow_publish_on_create(self):
        _login_as_admin(self.client)
        payload = self._complete_payload()
        payload['image'] = _tiny_image()

        self.client.post(reverse('admin_add_event'), payload)

        event = Event.objects.get(event_name='Digital Culture Summit')
        self.assertEqual(event.status, Event.STATUS_PUBLISHED)

    def test_incomplete_event_can_still_be_saved_as_draft(self):
        _login_as_admin(self.client)
        payload = self._complete_payload(status=Event.STATUS_DRAFT)
        del payload['location']

        self.client.post(reverse('admin_add_event'), payload)

        event = Event.objects.get(event_name='Digital Culture Summit')
        self.assertEqual(event.status, Event.STATUS_DRAFT)

    def test_capacity_and_pic_blank_does_not_block_publish(self):
        _login_as_admin(self.client)
        payload = self._complete_payload()
        payload['image'] = _tiny_image()
        # Explicitly NOT setting capacity / person_in_charge.
        self.assertNotIn('capacity', payload)
        self.assertNotIn('person_in_charge', payload)

        self.client.post(reverse('admin_add_event'), payload)

        event = Event.objects.get(event_name='Digital Culture Summit')
        self.assertEqual(event.status, Event.STATUS_PUBLISHED)
        self.assertIsNone(event.capacity)
        self.assertIsNone(event.person_in_charge)

    def test_completed_event_can_be_republished_when_complete(self):
        _login_as_admin(self.client)
        event = Event.objects.create(
            event_name='Old Completed Event',
            description='Sudah selesai.',
            location='Bandung',
            event_date=date.today() - timedelta(days=5),
            event_time='09:00:00',
            end_time='17:00:00',
            image_url='events/old.jpg',
            status=Event.STATUS_COMPLETED,
        )

        payload = self._complete_payload(event_name='Old Completed Event')
        self.client.post(reverse('admin_edit_event', args=[event.event_id]), payload)

        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_PUBLISHED)

    def test_missing_field_blocks_publish_on_edit_and_leaves_event_unchanged(self):
        _login_as_admin(self.client)
        event = Event.objects.create(
            event_name='Incomplete Event',
            description='',  # description still missing
            location='Bandung',
            event_date=date.today(),
            event_time='09:00:00',
            end_time='17:00:00',
            image_url=None,  # image still missing
            status=Event.STATUS_DRAFT,
        )

        payload = {
            'event_name': 'Incomplete Event',
            'location': 'Bandung',
            'event_date': date.today().isoformat(),
            'event_time': '09:00',
            'end_time': '17:00',
            'status': Event.STATUS_PUBLISHED,
            # description & image intentionally omitted
        }
        response = self.client.post(reverse('admin_edit_event', args=[event.event_id]), payload)

        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_DRAFT)
        self.assertContains(response, 'tidak bisa dipublish')

    def test_quick_status_change_to_published_blocked_when_incomplete(self):
        _login_as_admin(self.client)
        event = Event.objects.create(
            event_name='Quick Toggle Event',
            description='',  # missing
            location='Jakarta',
            event_date=date.today(),
            event_time='09:00:00',
            end_time='17:00:00',
            image_url='events/x.jpg',
            status=Event.STATUS_DRAFT,
        )

        response = self.client.post(
            reverse('admin_change_event_status', args=[event.event_id]),
            {'status': Event.STATUS_PUBLISHED},
        )

        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_DRAFT)
        self.assertNotEqual(response.status_code, 200)  # redirect (302) after messages.error

    def test_archived_allowed_from_any_status(self):
        _login_as_admin(self.client)
        event = Event.objects.create(
            event_name='To Be Hidden',
            description='desc', location='Loc',
            event_date=date.today(), event_time='09:00:00', end_time='17:00:00',
            image_url='events/x.jpg', status=Event.STATUS_COMPLETED,
        )

        self.client.post(reverse('admin_change_event_status', args=[event.event_id]), {'status': Event.STATUS_ARCHIVED})

        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_ARCHIVED)


class ExplorePastAndArchivedVisibilityTests(TestCase):
    def test_completed_event_shows_under_past_and_archived_is_hidden(self):
        completed = Event.objects.create(
            event_name='Finished Event', description='d', location='l',
            event_date=date.today() - timedelta(days=1),
            event_time='09:00:00', end_time='10:00:00',
            image_url='events/f.jpg', status=Event.STATUS_COMPLETED,
        )
        archived = Event.objects.create(
            event_name='Hidden Event', description='d', location='l',
            event_date=date.today() - timedelta(days=1),
            event_time='09:00:00', end_time='10:00:00',
            image_url='events/h.jpg', status=Event.STATUS_ARCHIVED,
        )

        response = self.client.get(reverse('explore'))

        self.assertContains(response, completed.event_name)
        self.assertNotContains(response, archived.event_name)

    def test_archived_still_visible_to_admin(self):
        _login_as_admin(self.client)
        archived = Event.objects.create(
            event_name='Admin Only Event', description='d', location='l',
            event_date=date.today(), event_time='09:00:00', end_time='10:00:00',
            image_url='events/a.jpg', status=Event.STATUS_ARCHIVED,
        )

        response = self.client.get(reverse('admin_explore'))

        self.assertContains(response, archived.event_name)


class EventAttendanceLogicTests(TestCase):
    def test_attendance_closed_when_open_time_not_set(self):
        # Sesuai implementasi saat ini: tanpa attendance_open_time yang
        # diisi admin secara eksplisit, presensi TIDAK PERNAH terbuka
        # (tidak ada fallback otomatis 30 menit setelah event_time).
        event = Event.objects.create(
            event_name='Test Event',
            event_date=timezone.now().date(),
            event_time=(timezone.now() + timedelta(minutes=5)).time(),
        )

        with patch('cms.models.timezone.now', return_value=timezone.make_aware(datetime(2024, 1, 1, 10, 31, 0))):
            self.assertFalse(event.is_attendance_open)

    def test_attendance_open_within_admin_defined_window(self):
        # Presensi hanya terbuka di antara attendance_open_time dan
        # attendance_close_time yang diisi admin.
        event = Event.objects.create(
            event_name='Test Event',
            event_date=timezone.now().date(),
            attendance_open_time=timezone.make_aware(datetime(2024, 1, 1, 10, 0, 0)),
            attendance_close_time=timezone.make_aware(datetime(2024, 1, 1, 12, 0, 0)),
        )

        # Sebelum jendela dibuka -> tertutup
        with patch('cms.models.timezone.now', return_value=timezone.make_aware(datetime(2024, 1, 1, 9, 59, 0))):
            self.assertFalse(event.is_attendance_open)

        # Di dalam jendela -> terbuka
        with patch('cms.models.timezone.now', return_value=timezone.make_aware(datetime(2024, 1, 1, 10, 30, 0))):
            self.assertTrue(event.is_attendance_open)

        # Setelah jendela ditutup -> tertutup lagi
        with patch('cms.models.timezone.now', return_value=timezone.make_aware(datetime(2024, 1, 1, 12, 1, 0))):
            self.assertFalse(event.is_attendance_open)

    def test_status_label_uses_ongoing_text(self):
        # Sesuai STATUS_CHOICES saat ini, label untuk STATUS_ONGOING
        # adalah 'Ongoing', bukan 'In Progress'.
        event = Event(status=Event.STATUS_ONGOING)
        self.assertEqual(event.get_status_display(), 'Ongoing')


class ExploreAdminAttributeTests(TestCase):
    def test_admin_explore_renders_attribute_data_from_database(self):
        attribute = Attribute.objects.create(
            attribute_name='Poster Uji Coba',
            attribute_type='poster',
            file_url='https://example.com/poster.pdf',
        )

        session = self.client.session
        session['is_admin'] = True
        session.save()

        response = self.client.get(reverse('admin_explore'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, attribute.attribute_name)
        self.assertContains(response, attribute.attribute_type)
        self.assertContains(response, attribute.file_url)
        self.assertContains(response, 'data-gdrive="https://example.com/poster.pdf"')


class AdminForgotPasswordTests(TestCase):
    @patch('cms.views.send_mail')
    def test_admin_forgot_password_submits_registered_admin_email(self, mock_send_mail):
        admin_user = User.objects.create_user(
            username='admin-reset',
            email='admin-reset@example.com',
            password='secret123',
            is_staff=True,
            is_superuser=True,
        )

        response = self.client.post(
            reverse('admin_forgot_password'),
            {'email': admin_user.email},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Jika alamat email terdaftar')
        mock_send_mail.assert_called_once()