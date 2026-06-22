"""
Management command to initialize sample data for development.
"""

import logging
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from core.models import Company, Site, Camera, UserRole

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Initialize sample data for development'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting sample data initialization...'))

        try:
            # 既存データをチェック
            if Company.objects.exists():
                self.stdout.write(self.style.WARNING('Sample data already exists. Skipping initialization.'))
                return

            # サンプル企業を作成
            company1 = Company.objects.create(
                code='company_001',
                name='サンプル企業A',
                description='テスト用企業'
            )
            self.stdout.write(f'Created company: {company1.name}')

            company2 = Company.objects.create(
                code='company_002',
                name='サンプル企業B',
                description='テスト用企業'
            )
            self.stdout.write(f'Created company: {company2.name}')

            # サンプル現場を作成
            site1 = Site.objects.create(
                company=company1,
                code='site_001',
                name='東京営業所',
                description='東京の営業所'
            )
            self.stdout.write(f'Created site: {site1.name}')

            site2 = Site.objects.create(
                company=company1,
                code='site_002',
                name='大阪営業所',
                description='大阪の営業所'
            )
            self.stdout.write(f'Created site: {site2.name}')

            site3 = Site.objects.create(
                company=company2,
                code='site_001',
                name='名古屋支店',
                description='名古屋の支店'
            )
            self.stdout.write(f'Created site: {site3.name}')

            # サンプルカメラを作成
            cameras_data = [
                {
                    'site': site1,
                    'code': 'camera_001',
                    'name': 'エントランスカメラ',
                    'url': 'http://example-camera-1.local/snapshot.jpg',
                    'username': 'admin',
                    'password': 'password',
                    'capture_interval_minutes': 5,
                },
                {
                    'site': site1,
                    'code': 'camera_002',
                    'name': '会議室カメラ',
                    'url': 'http://example-camera-2.local/snapshot.jpg',
                    'username': 'admin',
                    'password': 'password',
                    'capture_interval_minutes': 10,
                },
                {
                    'site': site2,
                    'code': 'camera_001',
                    'name': '工場入口カメラ',
                    'url': 'http://example-camera-3.local/snapshot.jpg',
                    'username': 'user',
                    'password': 'pass',
                    'capture_interval_minutes': 1,
                },
                {
                    'site': site3,
                    'code': 'camera_001',
                    'name': '駐車場カメラ',
                    'url': 'http://example-camera-4.local/snapshot.jpg',
                    'username': 'operator',
                    'password': 'op123',
                    'capture_interval_minutes': 3,
                },
            ]

            for camera_data in cameras_data:
                camera = Camera.objects.create(**camera_data)
                self.stdout.write(f'Created camera: {camera.name}')

            # サンプルユーザーを作成
            # システム管理者
            admin_user = User.objects.create_superuser(
                username='admin',
                email='admin@example.com',
                password='admin123'
            )
            UserRole.objects.create(
                user=admin_user,
                role='system_admin'
            )
            self.stdout.write(f'Created system admin user: admin')

            # 企業管理者
            company_admin = User.objects.create_user(
                username='company_admin',
                email='company_admin@example.com',
                password='password123'
            )
            UserRole.objects.create(
                user=company_admin,
                role='company_admin',
                company=company1
            )
            self.stdout.write(f'Created company admin user: company_admin')

            # 現場管理者
            site_admin = User.objects.create_user(
                username='site_admin',
                email='site_admin@example.com',
                password='password123'
            )
            UserRole.objects.create(
                user=site_admin,
                role='site_admin',
                site=site1
            )
            self.stdout.write(f'Created site admin user: site_admin')

            # 一般ユーザー
            general_user = User.objects.create_user(
                username='user',
                email='user@example.com',
                password='password123'
            )
            UserRole.objects.create(
                user=general_user,
                role='general_user'
            )
            self.stdout.write(f'Created general user: user')

            self.stdout.write(self.style.SUCCESS('Sample data initialization completed successfully!'))
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('=== Test User Credentials ==='))
            self.stdout.write('System Admin: admin / admin123')
            self.stdout.write('Company Admin: company_admin / password123')
            self.stdout.write('Site Admin: site_admin / password123')
            self.stdout.write('General User: user / password123')

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during initialization: {str(e)}'))
            raise
