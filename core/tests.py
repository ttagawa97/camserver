from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from requests.exceptions import ConnectionError
from unittest.mock import patch
from io import BytesIO
from datetime import timedelta
import os
import shutil
import tempfile

from core.models import Camera, Company, Image, Site, UserRole
from camserver.scheduler import CameraSchedulerManager
from PIL import Image as PILImage
from tasks.camera import capture_camera_image, process_pending_ai_analysis, test_camera_connection


class CompanySiteCreateApiTests(APITestCase):
    def setUp(self):
        self.system_admin = User.objects.create_user(username='admin', password='password')
        UserRole.objects.create(user=self.system_admin, role='system_admin')

        self.company = Company.objects.create(code='company_000001', name='既存企業')
        self.other_company = Company.objects.create(code='company_000002', name='別企業')
        self.site = Site.objects.create(company=self.company, code='site_000001', name='既存現場')

        self.company_admin = User.objects.create_user(username='company_admin', password='password')
        UserRole.objects.create(
            user=self.company_admin,
            role='company_admin',
            company=self.company,
        )

    def test_system_admin_can_create_company_with_spec_payload(self):
        self.client.force_authenticate(user=self.system_admin)

        response = self.client.post(
            reverse('company-list'),
            {'company_name': '新規企業'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['company_name'], '新規企業')
        self.assertEqual(response.data['data']['status'], 'active')
        self.assertTrue(Company.objects.filter(name='新規企業', code='company_000003').exists())

    def test_login_establishes_session_for_company_create(self):
        login_response = self.client.post(
            reverse('auth-login'),
            {'username': 'admin', 'password': 'password'},
            format='json',
        )
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)
        self.assertEqual(login_response.data['success'], True)

        response = self.client.post(
            reverse('company-list'),
            {'company_name': 'セッション企業'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['company_name'], 'セッション企業')

    def test_login_returns_csrf_token_for_session_write_apis(self):
        csrf_client = APIClient(enforce_csrf_checks=True)

        login_response = csrf_client.post(
            reverse('auth-login'),
            {'username': 'admin', 'password': 'password'},
            format='json',
        )
        csrf_token = login_response.data['csrf_token']

        response = csrf_client.post(
            reverse('company-list'),
            {'company_name': 'CSRF企業'},
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)

    def test_company_admin_cannot_create_company(self):
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.post(
            reverse('company-list'),
            {'company_name': '権限外企業'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['code'], 'FORBIDDEN')

    def test_company_admin_can_create_site_for_own_company(self):
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.post(
            reverse('site-list'),
            {
                'company_id': str(self.company.id),
                'site_name': '新規現場',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['company_id'], str(self.company.id))
        self.assertEqual(response.data['data']['site_name'], '新規現場')
        self.assertEqual(response.data['data']['status'], 'active')
        self.assertTrue(Site.objects.filter(company=self.company, name='新規現場', code='site_000002').exists())

    def test_company_admin_cannot_create_site_for_other_company(self):
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.post(
            reverse('site-list'),
            {
                'company_id': str(self.other_company.id),
                'site_name': '権限外現場',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['code'], 'FORBIDDEN')

    def test_session_user_can_create_camera_with_spec_payload(self):
        self.client.post(
            reverse('auth-login'),
            {'username': 'admin', 'password': 'password'},
            format='json',
        )

        response = self.client.post(
            reverse('camera-list'),
            {
                'company_id': str(self.company.id),
                'site_id': str(self.site.id),
                'camera_name': '新規カメラ',
                'address': 'http://example.com/snapshot.jpg',
                'auth_method': 'basic',
                'login_id': 'camera_user',
                'password': 'camera_password',
                'capture_interval_minutes': 5,
                'image_quality': 'HD',
                'retention_days': 30,
                'ai_text': '画像を確認してください',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['camera_name'], '新規カメラ')
        camera = Camera.objects.get(site=self.site, name='新規カメラ', code='camera_000001')
        self.assertEqual(camera.ai_text, '画像を確認してください')

    def test_update_camera_accepts_spec_payload(self):
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='更新前カメラ',
            url='http://example.com/old.jpg',
            username='old_user',
            password='old_password',
        )

        response = self.client.put(
            reverse('camera-detail', args=[camera.id]),
            {
                'site_id': str(self.site.id),
                'camera_name': '更新後カメラ',
                'address': 'http://example.com/new.jpg',
                'auth_method': 'basic',
                'login_id': 'new_user',
                'password': 'new_password',
                'capture_interval_minutes': 10,
                'image_quality': 'FullHD',
                'retention_days': 60,
                'ai_text': '更新後のAIテキスト',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        camera.refresh_from_db()
        self.assertEqual(camera.name, '更新後カメラ')
        self.assertEqual(camera.url, 'http://example.com/new.jpg')
        self.assertEqual(camera.username, 'new_user')
        self.assertEqual(camera.capture_interval_minutes, 10)
        self.assertEqual(camera.save_days, 60)
        self.assertEqual(camera.ai_text, '更新後のAIテキスト')

    @patch('camserver.scheduler.scheduler_instance.schedule_camera')
    def test_update_camera_queues_existing_images_when_ai_text_is_added(self, schedule_camera):
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='AI追加前カメラ',
            url='http://example.com/old.jpg',
            username='old_user',
            password='old_password',
            ai_text='',
        )
        image = Image.objects.create(
            camera=camera,
            file_path='company/site/camera/original.jpg',
            thumbnail_path='company/site/camera/thumb.jpg',
            captured_at=timezone.now() - timedelta(minutes=1),
            ai_analysis_status=Image.AI_STATUS_NOT_REQUIRED,
        )

        response = self.client.put(
            reverse('camera-detail', args=[camera.id]),
            {
                'site_id': str(self.site.id),
                'camera_name': 'AI追加後カメラ',
                'address': 'http://example.com/new.jpg',
                'auth_method': 'basic',
                'login_id': 'new_user',
                'password': '',
                'capture_interval_minutes': 10,
                'image_quality': 'HD',
                'retention_days': 30,
                'ai_text': '画像を解析してください',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        image.refresh_from_db()
        self.assertEqual(image.ai_analysis_status, Image.AI_STATUS_PENDING)
        self.assertEqual(image.ai_response_text, '')
        self.assertIsNone(image.ai_requested_at)
        self.assertIsNone(image.ai_responded_at)
        schedule_camera.assert_called_once()

    @patch('camserver.scheduler.scheduler_instance.schedule_camera')
    def test_update_camera_reschedules_with_new_capture_interval(self, schedule_camera):
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='更新前カメラ',
            url='http://example.com/old.jpg',
            username='old_user',
            password='old_password',
            capture_interval_minutes=3,
        )

        response = self.client.put(
            reverse('camera-detail', args=[camera.id]),
            {
                'site_id': str(self.site.id),
                'camera_name': '更新後カメラ',
                'address': 'http://example.com/new.jpg',
                'auth_method': 'basic',
                'login_id': 'new_user',
                'password': '',
                'capture_interval_minutes': 12,
                'image_quality': 'HD',
                'retention_days': 30,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        camera.refresh_from_db()
        self.assertEqual(camera.capture_interval_minutes, 12)
        schedule_camera.assert_called_once()
        scheduled_camera = schedule_camera.call_args.args[0]
        self.assertEqual(scheduled_camera.id, camera.id)
        self.assertEqual(scheduled_camera.capture_interval_minutes, 12)

    def test_update_camera_keeps_existing_password_when_blank(self):
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='更新前カメラ',
            url='http://example.com/old.jpg',
            username='old_user',
            password='old_password',
        )

        response = self.client.put(
            reverse('camera-detail', args=[camera.id]),
            {
                'site_id': str(self.site.id),
                'camera_name': '更新後カメラ',
                'address': 'http://example.com/new.jpg',
                'auth_method': 'basic',
                'login_id': 'new_user',
                'password': '',
                'capture_interval_minutes': 10,
                'image_quality': 'FullHD',
                'retention_days': 60,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        camera.refresh_from_db()
        self.assertEqual(camera.password, 'old_password')
        self.assertEqual(camera.username, 'new_user')

    @patch('core.views.test_camera_connection')
    def test_connection_test_accepts_input_payload(self, test_connection):
        test_connection.return_value = {
            'success': True,
            'message': 'カメラへの接続に成功しました',
            'status_code': 200,
        }
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='既存カメラ',
            url='http://example.com/current.jpg',
        )

        response = self.client.post(
            reverse('camera-test-connection', args=[camera.id]),
            {
                'site_id': str(self.site.id),
                'address': 'http://example.com/test.jpg',
                'auth_method': 'basic',
                'login_id': 'test_user',
                'password': 'test_password',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['result'], 'success')
        tested_camera = test_connection.call_args.args[0]
        self.assertEqual(tested_camera.url, 'http://example.com/test.jpg')

    def test_latest_images_accepts_post_camera_ids(self):
        self.client.force_authenticate(user=self.system_admin)
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='最新画像カメラ',
            url='http://example.com/current.jpg',
        )
        Image.objects.create(
            camera=camera,
            file_path='original.jpg',
            thumbnail_path='thumb.jpg',
            captured_at=timezone.now(),
        )

        response = self.client.post(
            reverse('image-latest-images'),
            {'camera_ids': [str(camera.id)]},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['cameras'][0]['camera_name'], '最新画像カメラ')

    def test_frontend_image_apis_return_displayable_urls(self):
        self.client.force_authenticate(user=self.system_admin)
        captured_at = timezone.now()
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='画像表示カメラ',
            url='http://example.com/current.jpg',
        )
        image = Image.objects.create(
            camera=camera,
            file_path='company/site/camera/original.jpg',
            thumbnail_path='company/site/camera/thumb.jpg',
            captured_at=captured_at,
            file_size=1234,
            width=1920,
            height=1080,
        )

        dates_response = self.client.get(
            reverse('image-available-dates'),
            {'camera_id': str(camera.id)},
        )
        self.assertEqual(dates_response.status_code, status.HTTP_200_OK)
        self.assertEqual(dates_response.data['success'], True)
        selected_date = dates_response.data['data']['default_date']
        self.assertTrue(selected_date)

        thumbnails_response = self.client.get(
            reverse('image-thumbnails'),
            {
                'camera_id': str(camera.id),
                'date': selected_date,
            },
        )
        self.assertEqual(thumbnails_response.status_code, status.HTTP_200_OK)
        self.assertEqual(thumbnails_response.data['data']['summary']['image_count'], 1)
        self.assertEqual(thumbnails_response.data['data']['summary']['total_file_size_bytes'], 1234)
        item = thumbnails_response.data['data']['images'][0]
        self.assertEqual(item['image_id'], str(image.id))
        self.assertTrue(item['thumbnail_url'].endswith('/media/company/site/camera/thumb.jpg'))
        self.assertTrue(item['image_url'].endswith('/media/company/site/camera/original.jpg'))
        self.assertEqual(item['ai_analysis_status'], 'not_required')
        self.assertIsNone(item['ai_response_text'])

        latest_response = self.client.post(
            reverse('image-latest-bulk'),
            {'camera_ids': [str(camera.id)]},
            format='json',
        )
        self.assertEqual(latest_response.status_code, status.HTTP_200_OK)
        self.assertTrue(latest_response.data['data']['cameras'][0]['thumbnail_url'].endswith('/media/company/site/camera/thumb.jpg'))

    def test_by_date_range_returns_thumbnail_payload_with_pagination(self):
        self.client.force_authenticate(user=self.system_admin)
        captured_at = timezone.now()
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='ページングカメラ',
            url='http://example.com/current.jpg',
        )
        older_image = Image.objects.create(
            camera=camera,
            file_path='company/site/camera/older.jpg',
            thumbnail_path='company/site/camera/older_thumb.jpg',
            captured_at=captured_at - timedelta(minutes=1),
            file_size=100,
        )
        newer_image = Image.objects.create(
            camera=camera,
            file_path='company/site/camera/newer.jpg',
            thumbnail_path='company/site/camera/newer_thumb.jpg',
            captured_at=captured_at,
            file_size=250,
            ai_analysis_status=Image.AI_STATUS_COMPLETED,
            ai_response_text='異常なし',
        )

        response = self.client.get(
            reverse('image-by-date-range'),
            {
                'camera_id': str(camera.id),
                'date': captured_at.date().isoformat(),
                'page': 1,
                'page_size': 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['summary']['image_count'], 2)
        self.assertEqual(response.data['data']['summary']['total_file_size_bytes'], 350)
        self.assertEqual(response.data['data']['pagination']['total_count'], 2)
        self.assertEqual(response.data['data']['pagination']['total_pages'], 2)
        self.assertEqual(len(response.data['data']['images']), 1)
        item = response.data['data']['images'][0]
        self.assertEqual(item['image_id'], str(newer_image.id))
        self.assertNotEqual(item['image_id'], str(older_image.id))
        self.assertEqual(item['ai_response_text'], '異常なし')

    def test_delete_camera_images_removes_images_and_keeps_camera(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        with self.settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(user=self.system_admin)
            camera = Camera.objects.create(
                site=self.site,
                code='camera_000001',
                name='画像削除対象カメラ',
                url='http://example.com/current.jpg',
            )
            paths = [
                ('company/site/camera/original1.jpg', 'company/site/camera/thumb1.jpg'),
                ('company/site/camera/original2.jpg', 'company/site/camera/thumb2.jpg'),
            ]
            os.makedirs(os.path.join(media_root, 'company/site/camera'), exist_ok=True)
            for image_path, thumb_path in paths:
                with open(os.path.join(media_root, image_path), 'wb') as f:
                    f.write(b'original')
                with open(os.path.join(media_root, thumb_path), 'wb') as f:
                    f.write(b'thumb')
                Image.objects.create(
                    camera=camera,
                    file_path=image_path,
                    thumbnail_path=thumb_path,
                    captured_at=timezone.now(),
                )

            response = self.client.delete(reverse('camera-delete-images', args=[camera.id]))

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['success'], True)
            self.assertEqual(response.data['data']['camera_id'], str(camera.id))
            self.assertEqual(response.data['data']['deleted_image_count'], 2)
            self.assertEqual(response.data['data']['deleted_file_count'], 4)
            self.assertTrue(Camera.objects.filter(id=camera.id).exists())
            self.assertFalse(Image.objects.filter(camera=camera).exists())
            for image_path, thumb_path in paths:
                self.assertFalse(os.path.exists(os.path.join(media_root, image_path)))
                self.assertFalse(os.path.exists(os.path.join(media_root, thumb_path)))

    def test_delete_camera_images_rejects_general_user(self):
        general_user = User.objects.create_user(username='general', password='password')
        UserRole.objects.create(
            user=general_user,
            role='general_user',
            company=self.company,
            site=self.site,
        )
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='権限外画像削除カメラ',
            url='http://example.com/current.jpg',
        )
        Image.objects.create(
            camera=camera,
            file_path='company/site/camera/original.jpg',
            captured_at=timezone.now(),
        )

        self.client.force_authenticate(user=general_user)
        response = self.client.delete(reverse('camera-delete-images', args=[camera.id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Image.objects.filter(camera=camera).exists())

    def test_delete_company_removes_related_images_and_files(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        with self.settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(user=self.system_admin)
            target_company_admin = User.objects.create_user(username='target_company_admin', password='password')
            UserRole.objects.create(
                user=target_company_admin,
                role='company_admin',
                company=self.company,
            )
            target_site_admin = User.objects.create_user(username='target_site_admin', password='password')
            UserRole.objects.create(
                user=target_site_admin,
                role='site_admin',
                company=self.company,
                site=self.site,
            )
            target_general_user = User.objects.create_user(username='target_general_user', password='password')
            UserRole.objects.create(
                user=target_general_user,
                role='general_user',
                company=self.company,
                site=self.site,
            )
            other_company_user = User.objects.create_user(username='other_company_user', password='password')
            UserRole.objects.create(
                user=other_company_user,
                role='general_user',
                company=self.other_company,
            )
            camera = Camera.objects.create(
                site=self.site,
                code='camera_000001',
                name='削除対象カメラ',
                url='http://example.com/current.jpg',
            )
            image_path = 'company/site/camera/original.jpg'
            thumb_path = 'company/site/camera/thumb.jpg'
            os.makedirs(os.path.join(media_root, 'company/site/camera'), exist_ok=True)
            with open(os.path.join(media_root, image_path), 'wb') as f:
                f.write(b'original')
            with open(os.path.join(media_root, thumb_path), 'wb') as f:
                f.write(b'thumb')

            Image.objects.create(
                camera=camera,
                file_path=image_path,
                thumbnail_path=thumb_path,
                captured_at=timezone.now(),
            )

            response = self.client.delete(reverse('company-detail', args=[self.company.id]))

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['success'], True)
            self.assertFalse(Company.objects.filter(id=self.company.id).exists())
            self.assertFalse(Site.objects.filter(id=self.site.id).exists())
            self.assertFalse(Camera.objects.filter(id=camera.id).exists())
            self.assertFalse(Image.objects.filter(camera_id=camera.id).exists())
            self.assertFalse(os.path.exists(os.path.join(media_root, image_path)))
            self.assertFalse(os.path.exists(os.path.join(media_root, thumb_path)))
            self.assertEqual(response.data['data']['deleted_users'], 4)
            target_company_admin.refresh_from_db()
            target_site_admin.refresh_from_db()
            target_general_user.refresh_from_db()
            other_company_user.refresh_from_db()
            self.system_admin.refresh_from_db()
            self.company_admin.refresh_from_db()
            self.assertFalse(target_company_admin.is_active)
            self.assertFalse(target_site_admin.is_active)
            self.assertFalse(target_general_user.is_active)
            self.assertFalse(self.company_admin.is_active)
            self.assertTrue(other_company_user.is_active)
            self.assertTrue(self.system_admin.is_active)

    def test_delete_site_removes_related_images_and_files(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        with self.settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(user=self.company_admin)
            target_site_admin = User.objects.create_user(username='delete_site_admin', password='password')
            UserRole.objects.create(
                user=target_site_admin,
                role='site_admin',
                company=self.company,
                site=self.site,
            )
            target_general_user = User.objects.create_user(username='delete_general_user', password='password')
            UserRole.objects.create(
                user=target_general_user,
                role='general_user',
                company=self.company,
                site=self.site,
            )
            camera = Camera.objects.create(
                site=self.site,
                code='camera_000001',
                name='削除対象カメラ',
                url='http://example.com/current.jpg',
            )
            image_path = 'company/site/camera/original.jpg'
            os.makedirs(os.path.join(media_root, 'company/site/camera'), exist_ok=True)
            with open(os.path.join(media_root, image_path), 'wb') as f:
                f.write(b'original')
            Image.objects.create(
                camera=camera,
                file_path=image_path,
                captured_at=timezone.now(),
            )

            response = self.client.delete(reverse('site-detail', args=[self.site.id]))

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['success'], True)
            self.assertTrue(Company.objects.filter(id=self.company.id).exists())
            self.assertFalse(Site.objects.filter(id=self.site.id).exists())
            self.assertFalse(Camera.objects.filter(id=camera.id).exists())
            self.assertFalse(os.path.exists(os.path.join(media_root, image_path)))
            self.assertEqual(response.data['data']['deleted_users'], 2)
            target_site_admin.refresh_from_db()
            target_general_user.refresh_from_db()
            self.company_admin.refresh_from_db()
            self.assertFalse(target_site_admin.is_active)
            self.assertFalse(target_general_user.is_active)
            self.assertTrue(self.company_admin.is_active)

    def test_system_admin_can_create_user_with_spec_payload(self):
        self.client.force_authenticate(user=self.system_admin)

        response = self.client.post(
            reverse('user-list'),
            {
                'login_id': 'site-admin-001',
                'user_name': '札幌現場管理者',
                'password': 'password123',
                'role': 'site_admin',
                'company_id': str(self.company.id),
                'site_id': str(self.site.id),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['login_id'], 'site-admin-001')
        self.assertEqual(response.data['data']['user_name'], '札幌現場管理者')
        self.assertEqual(response.data['data']['role'], 'site_admin')
        self.assertEqual(response.data['data']['company_id'], str(self.company.id))
        created_user = User.objects.get(username='site-admin-001')
        self.assertTrue(created_user.check_password('password123'))
        self.assertEqual(created_user.role.site, self.site)

    def test_company_admin_can_only_list_own_company_site_users(self):
        own_site_user = User.objects.create_user(username='own_site_user', password='password')
        UserRole.objects.create(
            user=own_site_user,
            role='general_user',
            company=self.company,
            site=self.site,
        )
        other_site = Site.objects.create(company=self.other_company, code='site_000001', name='別企業現場')
        other_site_user = User.objects.create_user(username='other_site_user', password='password')
        UserRole.objects.create(
            user=other_site_user,
            role='general_user',
            company=self.other_company,
            site=other_site,
        )
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.get(reverse('user-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        login_ids = {user['login_id'] for user in response.data['data']['users']}
        self.assertIn('own_site_user', login_ids)
        self.assertNotIn('other_site_user', login_ids)
        self.assertNotIn('admin', login_ids)

    def test_company_admin_cannot_create_company_admin(self):
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.post(
            reverse('user-list'),
            {
                'login_id': 'new_company_admin',
                'user_name': '新企業管理者',
                'password': 'password123',
                'role': 'company_admin',
                'company_id': str(self.company.id),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['code'], 'FORBIDDEN')

    def test_create_user_duplicate_login_id_returns_conflict(self):
        self.client.force_authenticate(user=self.system_admin)

        response = self.client.post(
            reverse('user-list'),
            {
                'login_id': 'admin',
                'user_name': '重複ユーザー',
                'password': 'password123',
                'role': 'system_admin',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'DUPLICATE_ERROR')

    def test_update_user_keeps_password_when_blank(self):
        self.client.force_authenticate(user=self.system_admin)
        managed_user = User.objects.create_user(username='managed', password='oldpassword123')
        UserRole.objects.create(
            user=managed_user,
            role='general_user',
            company=self.company,
            site=self.site,
        )

        response = self.client.put(
            reverse('user-detail', args=[managed_user.id]),
            {
                'login_id': 'managed-renamed',
                'user_name': '管理対象ユーザー',
                'password': '',
                'role': 'general_user',
                'company_id': str(self.company.id),
                'site_id': str(self.site.id),
                'status': 'inactive',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        managed_user.refresh_from_db()
        self.assertEqual(managed_user.username, 'managed-renamed')
        self.assertEqual(managed_user.first_name, '管理対象ユーザー')
        self.assertFalse(managed_user.is_active)
        self.assertTrue(managed_user.check_password('oldpassword123'))

    def test_delete_user_logically_deactivates_target(self):
        self.client.force_authenticate(user=self.system_admin)
        managed_user = User.objects.create_user(username='delete_target', password='password')
        UserRole.objects.create(
            user=managed_user,
            role='general_user',
            company=self.company,
            site=self.site,
        )

        response = self.client.delete(reverse('user-detail', args=[managed_user.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        managed_user.refresh_from_db()
        self.assertFalse(managed_user.is_active)

    def test_delete_self_is_rejected(self):
        self.client.force_authenticate(user=self.system_admin)

        response = self.client.delete(reverse('user-detail', args=[self.system_admin.id]))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'CONFLICT')


class CameraConnectionTests(SimpleTestCase):
    @override_settings(CAMERA_LOCALHOST_FALLBACK_HOSTS=['172.23.48.1'])
    @patch('tasks.camera._is_wsl', return_value=False)
    @patch('tasks.camera.requests.get')
    def test_localhost_camera_retries_configured_fallback_host(self, requests_get, _is_wsl):
        class DummyCamera:
            id = 'camera-id'
            name = 'ローカルPC'
            url = 'http://localhost:8080/snapshot.jpg'
            username = 'mockuser'
            password = 'mockpass'

        class DummyResponse:
            status_code = 200
            content = b'jpeg-bytes'

        requests_get.side_effect = [
            ConnectionError('connection refused'),
            DummyResponse(),
        ]

        result = test_camera_connection(DummyCamera())

        self.assertEqual(result['success'], True)
        self.assertEqual(result['effective_url'], 'http://172.23.48.1:8080/snapshot.jpg')
        self.assertEqual(requests_get.call_args_list[0].args[0], 'http://localhost:8080/snapshot.jpg')
        self.assertEqual(requests_get.call_args_list[1].args[0], 'http://172.23.48.1:8080/snapshot.jpg')


class CameraCaptureTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code='company_000001', name='企業')
        self.site = Site.objects.create(company=self.company, code='site_000001', name='現場')

    @patch('tasks.camera.requests.get')
    def test_capture_converts_image_to_configured_quality_before_saving(self, requests_get):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        source = PILImage.new('RGB', (1920, 1080), color='blue')
        source_bytes = BytesIO()
        source.save(source_bytes, 'JPEG')

        class DummyResponse:
            status_code = 200
            content = source_bytes.getvalue()

        requests_get.return_value = DummyResponse()

        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='HD保存カメラ',
            url='http://example.com/snapshot.jpg',
            save_quality=85,
            ai_text='状況を要約してください',
        )

        with self.settings(MEDIA_ROOT=media_root):
            image = capture_camera_image(camera.id)

            self.assertIsNotNone(image)
            self.assertEqual(image.width, 1280)
            self.assertEqual(image.height, 720)
            self.assertEqual(image.ai_analysis_status, Image.AI_STATUS_PENDING)
            self.assertEqual(image.file_size, os.path.getsize(os.path.join(media_root, image.file_path)))

            with PILImage.open(os.path.join(media_root, image.file_path)) as saved_image:
                self.assertEqual(saved_image.size, (1280, 720))

    @patch('tasks.camera.requests.get')
    def test_capture_skips_ai_analysis_when_ai_text_is_blank(self, requests_get):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        source = PILImage.new('RGB', (640, 480), color='blue')
        source_bytes = BytesIO()
        source.save(source_bytes, 'JPEG')

        class DummyResponse:
            status_code = 200
            content = source_bytes.getvalue()

        requests_get.return_value = DummyResponse()

        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='AIなしカメラ',
            url='http://example.com/snapshot.jpg',
            ai_text='',
        )

        with self.settings(MEDIA_ROOT=media_root):
            image = capture_camera_image(camera)

        self.assertIsNotNone(image)
        self.assertEqual(image.ai_analysis_status, Image.AI_STATUS_NOT_REQUIRED)

    @patch('tasks.camera._call_openai_image_analysis')
    def test_pending_ai_analysis_processes_latest_image_first(self, call_openai):
        call_openai.side_effect = ['latest result', 'old result']
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='AIカメラ',
            url='http://example.com/snapshot.jpg',
            ai_text='解析してください',
        )
        old_image = Image.objects.create(
            camera=camera,
            file_path='old.jpg',
            captured_at=timezone.now() - timedelta(minutes=5),
            ai_analysis_status=Image.AI_STATUS_PENDING,
        )
        latest_image = Image.objects.create(
            camera=camera,
            file_path='latest.jpg',
            captured_at=timezone.now(),
            ai_analysis_status=Image.AI_STATUS_PENDING,
        )

        processed = process_pending_ai_analysis(limit=1)

        self.assertEqual(processed, 1)
        latest_image.refresh_from_db()
        old_image.refresh_from_db()
        self.assertEqual(latest_image.ai_analysis_status, Image.AI_STATUS_COMPLETED)
        self.assertEqual(latest_image.ai_response_text, 'latest result')
        self.assertEqual(old_image.ai_analysis_status, Image.AI_STATUS_PENDING)

    @patch('tasks.camera._call_openai_image_analysis', side_effect=Exception('boom'))
    def test_ai_analysis_failure_saves_api_error(self, _call_openai):
        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='AIカメラ',
            url='http://example.com/snapshot.jpg',
            ai_text='解析してください',
        )
        image = Image.objects.create(
            camera=camera,
            file_path='latest.jpg',
            captured_at=timezone.now(),
            ai_analysis_status=Image.AI_STATUS_PENDING,
        )

        processed = process_pending_ai_analysis(limit=1)

        self.assertEqual(processed, 1)
        image.refresh_from_db()
        self.assertEqual(image.ai_analysis_status, Image.AI_STATUS_ERROR)
        self.assertEqual(image.ai_response_text, 'API ERROR')


class CameraSchedulerTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code='company_000001', name='企業')
        self.site = Site.objects.create(company=self.company, code='site_000001', name='現場')

    def test_schedule_camera_replaces_existing_job_with_new_interval(self):
        class DummyJob:
            next_run_time = None

        class DummyScheduler:
            running = True

            def __init__(self):
                self.jobs = {}
                self.removed_job_ids = []
                self.added_jobs = []

            def remove_job(self, job_id):
                self.removed_job_ids.append(job_id)
                if job_id not in self.jobs:
                    raise Exception('job not found')
                del self.jobs[job_id]

            def add_job(self, **kwargs):
                self.jobs[kwargs['id']] = kwargs
                self.added_jobs.append(kwargs)
                return DummyJob()

        camera = Camera.objects.create(
            site=self.site,
            code='camera_000001',
            name='スケジュールカメラ',
            url='http://example.com/snapshot.jpg',
            capture_interval_minutes=3,
        )
        manager = CameraSchedulerManager()
        manager.scheduler = DummyScheduler()

        manager.schedule_camera(camera)
        camera.capture_interval_minutes = 12
        camera.save(update_fields=['capture_interval_minutes'])
        manager.schedule_camera(camera)

        job_id = f'camera_{camera.id}'
        self.assertEqual(manager.scheduler.removed_job_ids, [job_id, job_id])
        self.assertEqual(len(manager.scheduler.added_jobs), 2)
        self.assertEqual(manager.scheduler.added_jobs[0]['trigger'].interval.total_seconds(), 180)
        self.assertEqual(manager.scheduler.added_jobs[1]['trigger'].interval.total_seconds(), 720)
        self.assertEqual(manager.scheduler.added_jobs[1]['args'], [camera.id])
