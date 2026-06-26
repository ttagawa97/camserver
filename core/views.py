"""
Views for core application REST API.
"""

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.middleware.csrf import get_token
from datetime import datetime, timedelta
import re

from core.models import Company, Site, Camera, Image, CameraSchedule, UserRole
from core.serializers import (
    CompanySerializer, SiteSerializer, CameraSerializer, ImageSerializer,
    CameraScheduleSerializer, UserRoleSerializer, UserSerializer
)
from tasks.camera import capture_camera_image, test_camera_connection


def _user_role(user):
    try:
        return user.role
    except UserRole.DoesNotExist:
        return None


def _status_label(is_active):
    return 'active' if is_active else 'inactive'


def _validation_error(message, field_errors=None):
    body = {
        'success': False,
        'code': 'VALIDATION_ERROR',
        'message': message,
    }
    if field_errors:
        body['errors'] = field_errors
    return Response(body, status=status.HTTP_400_BAD_REQUEST)


def _forbidden(message='この操作を実行する権限がありません'):
    return Response({
        'success': False,
        'code': 'FORBIDDEN',
        'message': message,
    }, status=status.HTTP_403_FORBIDDEN)


def _next_numbered_code(model, prefix, filters=None):
    filters = filters or {}
    pattern = re.compile(rf'^{re.escape(prefix)}_(\d+)$')
    max_number = 0

    for code in model.objects.filter(**filters).values_list('code', flat=True):
        match = pattern.match(code)
        if match:
            max_number = max(max_number, int(match.group(1)))

    return f'{prefix}_{max_number + 1:06d}'


IMAGE_QUALITY_TO_SAVE_QUALITY = {
    'VGA': 70,
    'SVGA': 75,
    'WXGA': 80,
    'HD': 85,
    'FullHD': 90,
    '4K': 95,
}


def _image_quality_from_save_quality(save_quality):
    quality = 'HD'
    for label, value in IMAGE_QUALITY_TO_SAVE_QUALITY.items():
        if save_quality >= value:
            quality = label
    return quality


def _camera_response(camera, include_sensitive=False):
    data = {
        'camera_id': str(camera.id),
        'company_id': str(camera.site.company_id),
        'site_id': str(camera.site_id),
        'camera_name': camera.name,
        'capture_interval_minutes': camera.capture_interval_minutes,
        'image_quality': _image_quality_from_save_quality(camera.save_quality),
        'status': _status_label(camera.is_active),
        'last_capture_at': camera.last_capture_at,
    }

    if include_sensitive:
        data.update({
            'address': camera.url,
            'auth_method': 'basic',
            'login_id': camera.username,
            'retention_days': camera.save_days,
        })

    return data


def _get_scoped_site(user_role, site_id):
    try:
        site = Site.objects.select_related('company').get(id=site_id)
    except (Site.DoesNotExist, ValueError):
        return None, _validation_error('入力内容に誤りがあります', {
            'site_id': ['指定された現場が見つかりません'],
        })

    if user_role.role == 'system_admin':
        return site, None

    if user_role.role == 'company_admin':
        if not user_role.company:
            return None, _forbidden('所属企業が設定されていません')
        if site.company_id != user_role.company_id:
            return None, _forbidden('所属企業以外の現場は指定できません')
        return site, None

    if user_role.role == 'site_admin':
        if not user_role.site:
            return None, _forbidden('所属現場が設定されていません')
        if site.id != user_role.site_id:
            return None, _forbidden('所属現場以外は指定できません')
        return site, None

    return None, _forbidden()


def _validate_camera_payload(data, require_password=True):
    errors = {}
    camera_name = data.get('camera_name') or data.get('name')
    address = data.get('address') or data.get('url')
    auth_method = data.get('auth_method', 'basic')
    login_id = data.get('login_id') or data.get('username')
    password = data.get('password')
    capture_interval_minutes = data.get('capture_interval_minutes')
    image_quality = data.get('image_quality')
    retention_days = data.get('retention_days') or data.get('save_days')

    if camera_name is not None:
        camera_name = camera_name.strip()
    if address is not None:
        address = address.strip()
    if auth_method is not None:
        auth_method = auth_method.strip()
    if login_id is not None:
        login_id = login_id.strip()

    if not camera_name:
        errors['camera_name'] = ['カメラ名を入力してください']
    elif len(camera_name) > 100:
        errors['camera_name'] = ['カメラ名は100文字以内で入力してください']

    if not address:
        errors['address'] = ['アドレスを入力してください']
    else:
        try:
            URLValidator()(address)
        except DjangoValidationError:
            errors['address'] = ['有効なURLを入力してください']

    if auth_method != 'basic':
        errors['auth_method'] = ['認証方式はbasicのみ指定できます']

    if not login_id:
        errors['login_id'] = ['IDを入力してください']
    elif len(login_id) > 255:
        errors['login_id'] = ['IDは255文字以内で入力してください']

    if require_password and not password:
        errors['password'] = ['パスワードを入力してください']

    try:
        capture_interval_minutes = int(capture_interval_minutes)
        if capture_interval_minutes < 1:
            errors['capture_interval_minutes'] = ['取得間隔は1分以上で指定してください']
    except (TypeError, ValueError):
        errors['capture_interval_minutes'] = ['取得間隔を指定してください']

    if image_quality not in IMAGE_QUALITY_TO_SAVE_QUALITY:
        errors['image_quality'] = ['保存画質は定義済み値から指定してください']

    try:
        retention_days = int(retention_days)
        if retention_days < 1:
            errors['retention_days'] = ['保存期間は1日以上で指定してください']
    except (TypeError, ValueError):
        errors['retention_days'] = ['保存期間を指定してください']

    if errors:
        return None, _validation_error('入力内容に誤りがあります', errors)

    return {
        'camera_name': camera_name,
        'address': address,
        'login_id': login_id,
        'password': password,
        'capture_interval_minutes': capture_interval_minutes,
        'image_quality': image_quality,
        'retention_days': retention_days,
    }, None


def _schedule_camera_if_possible(camera):
    try:
        from camserver.scheduler import scheduler_instance
        scheduler_instance.schedule_camera(camera)
    except Exception:
        pass


class CompanyViewSet(viewsets.ModelViewSet):
    """企業管理API"""
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['is_active']
    search_fields = ['code', 'name']
    ordering_fields = ['created_at', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        """ユーザー権限に基づいてクエリセットをフィルタリング"""
        user = self.request.user
        try:
            user_role = user.role
            if user_role.role == 'system_admin':
                return Company.objects.all()
            elif user_role.role == 'company_admin':
                return Company.objects.filter(id=user_role.company.id)
            else:
                return Company.objects.none()
        except UserRole.DoesNotExist:
            return Company.objects.none()

    def create(self, request, *args, **kwargs):
        """企業登録API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role != 'system_admin':
            return _forbidden()

        company_name = request.data.get('company_name') or request.data.get('name')
        if company_name is not None:
            company_name = company_name.strip()

        errors = {}
        if not company_name:
            errors['company_name'] = ['企業名を入力してください']
        elif len(company_name) > 255:
            errors['company_name'] = ['企業名は255文字以内で入力してください']
        elif Company.objects.filter(name=company_name).exists():
            errors['company_name'] = ['同じ企業名が既に登録されています']

        if errors:
            return _validation_error('入力内容に誤りがあります', errors)

        try:
            with transaction.atomic():
                company = Company.objects.create(
                    code=_next_numbered_code(Company, 'company'),
                    name=company_name,
                    is_active=True,
                )
        except IntegrityError:
            return _validation_error('企業コードの生成に失敗しました。再度お試しください')

        return Response({
            'success': True,
            'data': {
                'company_id': str(company.id),
                'company_name': company.name,
                'status': _status_label(company.is_active),
            },
        }, status=status.HTTP_201_CREATED)


class SiteViewSet(viewsets.ModelViewSet):
    """現場管理API"""
    queryset = Site.objects.all()
    serializer_class = SiteSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['company', 'is_active']
    search_fields = ['code', 'name']
    ordering_fields = ['created_at', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        """ユーザー権限に基づいてクエリセットをフィルタリング"""
        user = self.request.user
        try:
            user_role = user.role
            if user_role.role == 'system_admin':
                return Site.objects.all()
            elif user_role.role == 'company_admin':
                return Site.objects.filter(company=user_role.company)
            elif user_role.role == 'site_admin':
                return Site.objects.filter(id=user_role.site.id)
            else:
                return Site.objects.none()
        except UserRole.DoesNotExist:
            return Site.objects.none()

    def create(self, request, *args, **kwargs):
        """現場登録API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return _forbidden()

        company_id = request.data.get('company_id') or request.data.get('company')
        site_name = request.data.get('site_name') or request.data.get('name')
        if site_name is not None:
            site_name = site_name.strip()

        errors = {}
        if not company_id:
            errors['company_id'] = ['所属企業を指定してください']

        company = None
        if company_id:
            try:
                company = Company.objects.get(id=company_id)
            except (Company.DoesNotExist, ValueError):
                errors['company_id'] = ['指定された企業が見つかりません']

        if user_role.role == 'company_admin':
            if not user_role.company:
                return _forbidden('所属企業が設定されていません')
            if company and company.id != user_role.company.id:
                return _forbidden('所属企業以外の現場は登録できません')

        if not site_name:
            errors['site_name'] = ['現場名を入力してください']
        elif len(site_name) > 255:
            errors['site_name'] = ['現場名は255文字以内で入力してください']
        elif company and Site.objects.filter(company=company, name=site_name).exists():
            errors['site_name'] = ['同じ企業内に同じ現場名が既に登録されています']

        if errors:
            return _validation_error('入力内容に誤りがあります', errors)

        try:
            with transaction.atomic():
                site_obj = Site.objects.create(
                    company=company,
                    code=_next_numbered_code(Site, 'site', {'company': company}),
                    name=site_name,
                    is_active=True,
                )
        except IntegrityError:
            return _validation_error('現場コードの生成に失敗しました。再度お試しください')

        return Response({
            'success': True,
            'data': {
                'site_id': str(site_obj.id),
                'company_id': str(site_obj.company_id),
                'site_name': site_obj.name,
                'status': _status_label(site_obj.is_active),
            },
        }, status=status.HTTP_201_CREATED)


class CameraViewSet(viewsets.ModelViewSet):
    """カメラ管理API"""
    queryset = Camera.objects.all()
    serializer_class = CameraSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['site', 'is_capturing', 'is_active']
    search_fields = ['code', 'name', 'url']
    ordering_fields = ['created_at', 'name', 'last_capture_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """ユーザー権限に基づいてクエリセットをフィルタリング"""
        user = self.request.user
        try:
            user_role = user.role
            if user_role.role == 'system_admin':
                return Camera.objects.all()
            elif user_role.role == 'company_admin':
                return Camera.objects.filter(site__company=user_role.company)
            elif user_role.role == 'site_admin':
                return Camera.objects.filter(site=user_role.site)
            else:
                return Camera.objects.none()
        except UserRole.DoesNotExist:
            return Camera.objects.none()

    def list(self, request, *args, **kwargs):
        """カメラ一覧取得API"""
        user_role = _user_role(request.user)
        if not user_role:
            return _forbidden()

        cameras = self.get_queryset().select_related('site', 'site__company')

        site_id = request.query_params.get('site_id') or request.query_params.get('site')
        if site_id:
            cameras = cameras.filter(site_id=site_id)

        company_id = request.query_params.get('company_id')
        if company_id:
            cameras = cameras.filter(site__company_id=company_id)

        include_deleted = request.query_params.get('include_deleted') in ('true', '1', 'True')
        if not include_deleted:
            cameras = cameras.filter(is_active=True)

        keyword = request.query_params.get('keyword')
        if keyword:
            cameras = cameras.filter(name__icontains=keyword)

        include_sensitive = user_role.role in ('system_admin', 'company_admin', 'site_admin')
        return Response({
            'success': True,
            'data': {
                'cameras': [
                    _camera_response(camera, include_sensitive=include_sensitive)
                    for camera in cameras
                ],
            },
        })

    def retrieve(self, request, *args, **kwargs):
        """カメラ詳細取得API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin', 'site_admin'):
            return _forbidden()

        camera = self.get_object()
        return Response({
            'success': True,
            'data': _camera_response(camera, include_sensitive=True),
        })

    def create(self, request, *args, **kwargs):
        """カメラ登録API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin', 'site_admin'):
            return _forbidden()

        site_id = request.data.get('site_id') or request.data.get('site')
        if not site_id:
            return _validation_error('入力内容に誤りがあります', {
                'site_id': ['現場を指定してください'],
            })

        site, error_response = _get_scoped_site(user_role, site_id)
        if error_response:
            return error_response

        payload, error_response = _validate_camera_payload(request.data, require_password=True)
        if error_response:
            return error_response

        if Camera.objects.filter(site=site, name=payload['camera_name']).exists():
            return _validation_error('入力内容に誤りがあります', {
                'camera_name': ['同じ現場に同じカメラ名が既に登録されています'],
            })

        try:
            with transaction.atomic():
                camera = Camera.objects.create(
                    site=site,
                    code=_next_numbered_code(Camera, 'camera', {'site': site}),
                    name=payload['camera_name'],
                    url=payload['address'],
                    username=payload['login_id'],
                    password=payload['password'],
                    capture_interval_minutes=payload['capture_interval_minutes'],
                    save_quality=IMAGE_QUALITY_TO_SAVE_QUALITY[payload['image_quality']],
                    save_days=payload['retention_days'],
                    is_active=True,
                    is_capturing=True,
                )
        except IntegrityError:
            return _validation_error('カメラコードの生成に失敗しました。再度お試しください')

        _schedule_camera_if_possible(camera)
        return Response({
            'success': True,
            'data': {
                'camera_id': str(camera.id),
                'camera_name': camera.name,
                'status': _status_label(camera.is_active),
            },
            'message': 'カメラを登録しました',
        }, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """カメラ更新API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin', 'site_admin'):
            return _forbidden()

        camera = self.get_object()
        site_id = request.data.get('site_id') or request.data.get('site') or str(camera.site_id)
        site, error_response = _get_scoped_site(user_role, site_id)
        if error_response:
            return error_response

        payload, error_response = _validate_camera_payload(request.data, require_password=True)
        if error_response:
            return error_response

        duplicate = Camera.objects.filter(site=site, name=payload['camera_name']).exclude(id=camera.id)
        if duplicate.exists():
            return _validation_error('入力内容に誤りがあります', {
                'camera_name': ['同じ現場に同じカメラ名が既に登録されています'],
            })

        camera.site = site
        camera.name = payload['camera_name']
        camera.url = payload['address']
        camera.username = payload['login_id']
        camera.password = payload['password']
        camera.capture_interval_minutes = payload['capture_interval_minutes']
        camera.save_quality = IMAGE_QUALITY_TO_SAVE_QUALITY[payload['image_quality']]
        camera.save_days = payload['retention_days']
        camera.save()

        _schedule_camera_if_possible(camera)
        return Response({
            'success': True,
            'data': {
                'camera_id': str(camera.id),
                'camera_name': camera.name,
                'updated_at': camera.updated_at,
            },
            'message': 'カメラ設定を保存しました',
        })

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """カメラ接続テスト"""
        if request.data:
            user_role = _user_role(request.user)
            if not user_role or user_role.role not in ('system_admin', 'company_admin', 'site_admin'):
                return _forbidden()

            site_id = request.data.get('site_id') or request.data.get('site')
            if site_id:
                site, error_response = _get_scoped_site(user_role, site_id)
                if error_response:
                    return error_response
            else:
                site = self.get_object().site

            payload, error_response = _validate_camera_payload(
                {
                    **request.data,
                    'camera_name': request.data.get('camera_name') or 'connection_test',
                    'capture_interval_minutes': request.data.get('capture_interval_minutes') or 1,
                    'image_quality': request.data.get('image_quality') or 'HD',
                    'retention_days': request.data.get('retention_days') or 1,
                },
                require_password=True,
            )
            if error_response:
                return error_response

            camera = Camera(
                site=site,
                code='connection_test',
                name='connection_test',
                url=payload['address'],
                username=payload['login_id'],
                password=payload['password'],
            )
        else:
            camera = self.get_object()

        result = test_camera_connection(camera)
        data = {
            'result': 'success' if result.get('success') else 'failed',
            'http_status_code': result.get('status_code'),
            'response_time_ms': None,
            'message': result.get('message'),
            'preview_image_url': None,
            'error_code': result.get('error'),
        }
        return Response({
            'success': True,
            'data': data,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def capture_now(self, request, pk=None):
        """即座に画像取得"""
        camera = self.get_object()
        try:
            image = capture_camera_image(camera)
            if image:
                return Response({
                    'success': True,
                    'message': '画像取得成功',
                    'image': ImageSerializer(image).data
                })
            else:
                return Response({
                    'success': False,
                    'message': '画像取得失敗'
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': f'エラー: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['patch'])
    def update_schedule(self, request, pk=None):
        """カメラ取得スケジュール更新"""
        camera = self.get_object()
        interval = request.data.get('capture_interval_minutes')
        
        if interval is not None:
            camera.capture_interval_minutes = interval
            camera.save()
            
            # スケジューラーを再設定（別プロセスで行う）
            from camserver.scheduler import scheduler_instance
            try:
                scheduler_instance.reschedule_camera(camera)
                return Response({
                    'success': True,
                    'message': 'スケジュール更新成功',
                    'camera': CameraSerializer(camera).data
                })
            except Exception as e:
                return Response({
                    'success': False,
                    'message': f'スケジュール更新失敗: {str(e)}'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        return Response({
            'success': False,
            'message': 'capture_interval_minutes を指定してください'
        }, status=status.HTTP_400_BAD_REQUEST)


class ImageViewSet(viewsets.ReadOnlyModelViewSet):
    """画像参照API"""
    queryset = Image.objects.all()
    serializer_class = ImageSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['camera', 'captured_at']
    search_fields = ['camera__name']
    ordering_fields = ['captured_at', 'created_at']
    ordering = ['-captured_at']

    def get_queryset(self):
        """ユーザー権限に基づいてクエリセットをフィルタリング"""
        user = self.request.user
        try:
            user_role = user.role
            if user_role.role == 'system_admin':
                return Image.objects.all()
            elif user_role.role == 'company_admin':
                return Image.objects.filter(camera__site__company=user_role.company)
            elif user_role.role == 'site_admin':
                return Image.objects.filter(camera__site=user_role.site)
            else:
                return Image.objects.none()
        except UserRole.DoesNotExist:
            return Image.objects.none()

    @action(detail=False, methods=['get'])
    def by_date_range(self, request):
        """日付範囲で画像取得"""
        camera_id = request.query_params.get('camera_id')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        if not camera_id:
            return Response({'error': 'camera_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            camera = Camera.objects.get(id=camera_id)
            images = Image.objects.filter(camera=camera)

            if start_date:
                images = images.filter(captured_at__gte=start_date)
            if end_date:
                images = images.filter(captured_at__lte=end_date)

            serializer = self.get_serializer(images, many=True)
            return Response(serializer.data)
        except Camera.DoesNotExist:
            return Response({'error': 'Camera not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get'])
    def dates_with_images(self, request):
        """画像が存在する日付一覧"""
        camera_id = request.query_params.get('camera_id')

        if not camera_id:
            return Response({'error': 'camera_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            camera = Camera.objects.get(id=camera_id)
            images = Image.objects.filter(camera=camera)
            
            # 日付ごとにグループ化
            dates = {}
            for img in images:
                date_str = img.captured_at.date().isoformat()
                if date_str not in dates:
                    dates[date_str] = []
                dates[date_str].append(img.captured_at.isoformat())
            
            return Response(dates)
        except Camera.DoesNotExist:
            return Response({'error': 'Camera not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get', 'post'])
    def latest_images(self, request):
        """複数カメラの最新画像"""
        if request.method == 'POST':
            camera_ids = request.data.get('camera_ids', [])
        else:
            camera_ids = request.query_params.getlist('camera_ids')
            if not camera_ids and request.query_params.get('camera_id'):
                camera_ids = [request.query_params.get('camera_id')]

        if not camera_ids:
            return _validation_error('入力内容に誤りがあります', {
                'camera_ids': ['カメラIDを指定してください'],
            })

        try:
            cameras = Camera.objects.filter(id__in=camera_ids).select_related('site')
            result = []
            
            for camera in cameras:
                latest = Image.objects.filter(camera=camera).first()
                result.append({
                    'camera_id': str(camera.id),
                    'camera_name': camera.name,
                    'latest_status': 'success' if latest else 'no_image',
                    'captured_at': latest.captured_at if latest else None,
                    'image_url': latest.file_path if latest else None,
                    'thumbnail_url': latest.thumbnail_path if latest else None,
                })
            
            if request.method == 'POST':
                return Response({
                    'success': True,
                    'data': {
                        'server_time': timezone.now(),
                        'cameras': result,
                    },
                })

            return Response({
                str(item['camera_id']): item
                for item in result
            })
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CameraScheduleViewSet(viewsets.ReadOnlyModelViewSet):
    """カメラスケジュール参照API"""
    queryset = CameraSchedule.objects.all()
    serializer_class = CameraScheduleSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['camera', 'is_running']
    ordering_fields = ['next_run_time', 'last_run_time']
    ordering = ['-next_run_time']


class AuthViewSet(viewsets.ViewSet):
    """認証API"""
    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=['post'])
    def login(self, request):
        """ログイン"""
        username = request.data.get('username')
        password = request.data.get('password')

        if not username or not password:
            return Response({
                'success': False,
                'message': 'ユーザー名とパスワードを入力してください'
            }, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(request, username=username, password=password)
        if not user:
            return Response({
                'success': False,
                'message': 'ユーザー名またはパスワードが間違っています'
            }, status=status.HTTP_401_UNAUTHORIZED)

        auth_login(request, user)
        csrf_token = get_token(request)
        return Response({
            'success': True,
            'user': UserSerializer(user).data,
            'csrf_token': csrf_token,
        })

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        """ログインユーザー情報"""
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def logout(self, request):
        """ログアウト"""
        auth_logout(request)
        return Response({'success': True, 'message': 'ログアウト成功'})
