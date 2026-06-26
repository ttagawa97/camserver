from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from requests.exceptions import ConnectionError
from unittest.mock import patch
import os
import shutil
import tempfile

from core.models import Camera, Company, Image, Site, UserRole
from tasks.camera import test_camera_connection


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
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.data['data']['camera_name'], '新規カメラ')
        self.assertTrue(Camera.objects.filter(site=self.site, name='新規カメラ', code='camera_000001').exists())

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
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        camera.refresh_from_db()
        self.assertEqual(camera.name, '更新後カメラ')
        self.assertEqual(camera.url, 'http://example.com/new.jpg')
        self.assertEqual(camera.username, 'new_user')
        self.assertEqual(camera.save_days, 60)

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
        item = thumbnails_response.data['data']['images'][0]
        self.assertEqual(item['image_id'], str(image.id))
        self.assertTrue(item['thumbnail_url'].endswith('/media/company/site/camera/thumb.jpg'))
        self.assertTrue(item['image_url'].endswith('/media/company/site/camera/original.jpg'))

        latest_response = self.client.post(
            reverse('image-latest-bulk'),
            {'camera_ids': [str(camera.id)]},
            format='json',
        )
        self.assertEqual(latest_response.status_code, status.HTTP_200_OK)
        self.assertTrue(latest_response.data['data']['cameras'][0]['thumbnail_url'].endswith('/media/company/site/camera/thumb.jpg'))

    def test_delete_company_removes_related_images_and_files(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        with self.settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(user=self.system_admin)
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

    def test_delete_site_removes_related_images_and_files(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))

        with self.settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(user=self.company_admin)
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
