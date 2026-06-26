from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from unittest.mock import patch

from core.models import Camera, Company, Image, Site, UserRole


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
