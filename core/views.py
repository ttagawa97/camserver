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
from django.db.models import Q
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.middleware.csrf import get_token
from django.conf import settings
from datetime import datetime, timedelta
from pathlib import Path
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


def _conflict(message):
    return Response({
        'success': False,
        'code': 'CONFLICT',
        'message': message,
    }, status=status.HTTP_409_CONFLICT)


def _duplicate_error(message, field_errors=None):
    body = {
        'success': False,
        'code': 'DUPLICATE_ERROR',
        'message': message,
    }
    if field_errors:
        body['errors'] = field_errors
    return Response(body, status=status.HTTP_409_CONFLICT)


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
            'ai_text': camera.ai_text,
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


def _display_name(user):
    return user.get_full_name() or user.first_name or user.username


def _user_status(user):
    return 'active' if user.is_active else 'inactive'


def _user_response(user):
    role = _user_role(user)
    company = role.company if role else None
    site = role.site if role else None

    return {
        'user_id': str(user.id),
        'login_id': user.username,
        'user_name': _display_name(user),
        'role': role.role if role else None,
        'company_id': str(company.id) if company else None,
        'company_name': company.name if company else None,
        'site_id': str(site.id) if site else None,
        'site_name': site.name if site else None,
        'status': _user_status(user),
    }


def _validate_user_payload(data, current_user=None, password_required=True):
    errors = {}
    login_id = data.get('login_id') or data.get('username')
    user_name = data.get('user_name') or data.get('name') or data.get('first_name')
    password = data.get('password')
    role = data.get('role')
    company_id = data.get('company_id') or data.get('company')
    site_id = data.get('site_id') or data.get('site')
    user_status = data.get('status', 'active')

    if login_id is not None:
        login_id = login_id.strip()
    if user_name is not None:
        user_name = user_name.strip()
    if role is not None:
        role = role.strip()
    if user_status is not None:
        user_status = user_status.strip()

    check_duplicate_login = False
    if not login_id:
        errors['login_id'] = ['ログインIDを入力してください']
    elif len(login_id) > 150:
        errors['login_id'] = ['ログインIDは150文字以内で入力してください']
    else:
        check_duplicate_login = True

    if not user_name:
        errors['user_name'] = ['ユーザー名を入力してください']
    elif len(user_name) > 255:
        errors['user_name'] = ['ユーザー名は255文字以内で入力してください']

    if password_required and not password:
        errors['password'] = ['パスワードを入力してください']
    elif password:
        if len(password) < 8:
            errors['password'] = ['パスワードは8文字以上で入力してください']

    valid_roles = {choice[0] for choice in UserRole.ROLE_CHOICES}
    if role not in valid_roles:
        errors['role'] = ['権限を指定してください']

    if user_status not in ('active', 'inactive'):
        errors['status'] = ['ステータスはactiveまたはinactiveを指定してください']

    company = None
    if role in ('company_admin', 'site_admin', 'general_user'):
        if not company_id:
            errors['company_id'] = ['所属企業を指定してください']
        else:
            try:
                company = Company.objects.get(id=company_id)
            except (Company.DoesNotExist, ValueError):
                errors['company_id'] = ['指定された企業が見つかりません']
    elif company_id:
        errors['company_id'] = ['システム管理者には所属企業を指定できません']

    site = None
    if role in ('site_admin', 'general_user'):
        if not site_id:
            errors['site_id'] = ['所属現場を指定してください']
        else:
            try:
                site = Site.objects.select_related('company').get(id=site_id)
            except (Site.DoesNotExist, ValueError):
                errors['site_id'] = ['指定された現場が見つかりません']
    elif site_id:
        errors['site_id'] = ['この権限には所属現場を指定できません']

    if company and site and site.company_id != company.id:
        errors['site_id'] = ['所属現場は所属企業配下の現場を指定してください']

    if errors:
        return None, _validation_error('入力内容に誤りがあります', errors)

    if check_duplicate_login:
        duplicate = User.objects.filter(username=login_id)
        if current_user:
            duplicate = duplicate.exclude(id=current_user.id)
        if duplicate.exists():
            return None, _duplicate_error('重複するデータが存在します', {
                'login_id': ['同じログインIDが既に登録されています'],
            })

    return {
        'login_id': login_id,
        'user_name': user_name,
        'password': password,
        'role': role,
        'company': company if role in ('company_admin', 'site_admin', 'general_user') else None,
        'site': site if role in ('site_admin', 'general_user') else None,
        'status': user_status,
    }, None


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
    ai_text = data.get('ai_text', '')

    if camera_name is not None:
        camera_name = camera_name.strip()
    if address is not None:
        address = address.strip()
    if auth_method is not None:
        auth_method = auth_method.strip()
    if login_id is not None:
        login_id = login_id.strip()
    if ai_text is None:
        ai_text = ''
    else:
        ai_text = str(ai_text).strip()

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

    if len(ai_text) > 2000:
        errors['ai_text'] = ['AIテキストは2000文字以内で入力してください']

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
        'ai_text': ai_text,
    }, None


def _schedule_camera_if_possible(camera):
    try:
        from camserver.scheduler import scheduler_instance
        scheduler_instance.schedule_camera(camera)
    except Exception as exc:
        logger.warning('Failed to schedule camera %s: %s', camera.id, exc)


def _delete_image_files(images):
    media_root = Path(settings.MEDIA_ROOT).resolve()
    deleted_files = 0

    for image in images:
        for relative_path in (image.file_path, image.thumbnail_path):
            if not relative_path:
                continue

            file_path = (media_root / relative_path).resolve()
            try:
                file_path.relative_to(media_root)
            except ValueError:
                continue

            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                deleted_files += 1

                parent = file_path.parent
                while parent != media_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

    return deleted_files


def _deactivate_users_for_company(company):
    target_user_ids = UserRole.objects.filter(
        Q(company=company) | Q(site__company=company),
        role__in=('company_admin', 'site_admin', 'general_user'),
    ).values_list('user_id', flat=True).distinct()

    return User.objects.filter(id__in=target_user_ids, is_active=True).update(is_active=False)


def _deactivate_users_for_site(site):
    target_user_ids = UserRole.objects.filter(
        site=site,
        role__in=('site_admin', 'general_user'),
    ).values_list('user_id', flat=True).distinct()

    return User.objects.filter(id__in=target_user_ids, is_active=True).update(is_active=False)


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

    def destroy(self, request, *args, **kwargs):
        """企業削除API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role != 'system_admin':
            return _forbidden()

        company = self.get_object()
        images = list(Image.objects.filter(camera__site__company=company))
        deleted_files = _delete_image_files(images)
        with transaction.atomic():
            deactivated_users = _deactivate_users_for_company(company)
            company.delete()

        return Response({
            'success': True,
            'message': '企業を削除しました',
            'data': {
                'deleted_images': len(images),
                'deleted_files': deleted_files,
                'deleted_users': deactivated_users,
            },
        })


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

    def destroy(self, request, *args, **kwargs):
        """現場削除API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return _forbidden()

        site_obj = self.get_object()
        if user_role.role == 'company_admin' and site_obj.company_id != user_role.company_id:
            return _forbidden('所属企業以外の現場は削除できません')

        images = list(Image.objects.filter(camera__site=site_obj))
        deleted_files = _delete_image_files(images)
        with transaction.atomic():
            deactivated_users = _deactivate_users_for_site(site_obj)
            site_obj.delete()

        return Response({
            'success': True,
            'message': '現場を削除しました',
            'data': {
                'deleted_images': len(images),
                'deleted_files': deleted_files,
                'deleted_users': deactivated_users,
            },
        })


class UserViewSet(viewsets.ModelViewSet):
    """ユーザー管理API"""
    queryset = User.objects.select_related('role', 'role__company', 'role__site', 'role__site__company')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def _request_role(self):
        return _user_role(self.request.user)

    def _management_forbidden(self):
        return _forbidden('ユーザー管理を実行する権限がありません')

    def get_queryset(self):
        user_role = self._request_role()
        users = User.objects.select_related('role', 'role__company', 'role__site', 'role__site__company')

        if not user_role:
            return users.none()

        if user_role.role == 'system_admin':
            return users

        if user_role.role == 'company_admin' and user_role.company:
            return users.filter(
                role__role__in=('site_admin', 'general_user')
            ).filter(
                Q(role__company=user_role.company) | Q(role__site__company=user_role.company)
            )

        return users.none()

    def get_object(self):
        users = User.objects.select_related('role', 'role__company', 'role__site', 'role__site__company')
        return get_object_or_404(users, pk=self.kwargs.get(self.lookup_field))

    def _can_manage_payload(self, manager_role, payload):
        if manager_role.role == 'system_admin':
            return None

        if manager_role.role != 'company_admin' or not manager_role.company:
            return self._management_forbidden()

        if payload['role'] not in ('site_admin', 'general_user'):
            return _forbidden('企業管理者は現場管理者または一般ユーザーのみ管理できます')

        if not payload['company'] or payload['company'].id != manager_role.company_id:
            return _forbidden('所属企業以外のユーザーは管理できません')

        if payload['site'] and payload['site'].company_id != manager_role.company_id:
            return _forbidden('所属企業以外の現場は指定できません')

        return None

    def _can_manage_target(self, manager_role, target_user):
        if manager_role.role == 'system_admin':
            return None

        if manager_role.role != 'company_admin' or not manager_role.company:
            return self._management_forbidden()

        target_role = _user_role(target_user)
        if not target_role or target_role.role not in ('site_admin', 'general_user'):
            return _forbidden('企業管理者は現場管理者または一般ユーザーのみ管理できます')

        target_company_id = target_role.company_id
        if target_role.site:
            target_company_id = target_role.site.company_id

        if target_company_id != manager_role.company_id:
            return _forbidden('所属企業以外のユーザーは管理できません')

        return None

    def list(self, request, *args, **kwargs):
        user_role = self._request_role()
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return self._management_forbidden()

        users = self.get_queryset()

        company_id = request.query_params.get('company_id')
        if company_id:
            users = users.filter(Q(role__company_id=company_id) | Q(role__site__company_id=company_id))

        site_id = request.query_params.get('site_id')
        if site_id:
            users = users.filter(role__site_id=site_id)

        role = request.query_params.get('role')
        if role:
            users = users.filter(role__role=role)

        keyword = request.query_params.get('keyword')
        if keyword:
            users = users.filter(
                Q(username__icontains=keyword)
                | Q(first_name__icontains=keyword)
                | Q(last_name__icontains=keyword)
            )

        user_status = request.query_params.get('status', 'active')
        if user_status == 'active':
            users = users.filter(is_active=True)
        elif user_status in ('inactive', 'deleted'):
            users = users.filter(is_active=False)
        elif user_status != 'all':
            return _validation_error('入力内容に誤りがあります', {
                'status': ['ステータスはactive、inactive、deleted、allのいずれかを指定してください'],
            })

        users = users.order_by('id')

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(max(int(request.query_params.get('page_size', 50)), 1), 200)
        except ValueError:
            return _validation_error('入力内容に誤りがあります', {
                'pagination': ['pageとpage_sizeは数値で指定してください'],
            })

        total_count = users.count()
        start = (page - 1) * page_size
        end = start + page_size
        total_pages = (total_count + page_size - 1) // page_size if total_count else 1

        return Response({
            'success': True,
            'data': {
                'users': [_user_response(user) for user in users[start:end]],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total_count': total_count,
                    'total_pages': total_pages,
                },
            },
        })

    def retrieve(self, request, *args, **kwargs):
        user_role = self._request_role()
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return self._management_forbidden()

        target_user = self.get_object()
        forbidden_response = self._can_manage_target(user_role, target_user)
        if forbidden_response:
            return forbidden_response

        return Response({
            'success': True,
            'data': _user_response(target_user),
        })

    def create(self, request, *args, **kwargs):
        user_role = self._request_role()
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return self._management_forbidden()

        payload, error_response = _validate_user_payload(request.data, password_required=True)
        if error_response:
            return error_response

        forbidden_response = self._can_manage_payload(user_role, payload)
        if forbidden_response:
            return forbidden_response

        with transaction.atomic():
            user = User.objects.create_user(
                username=payload['login_id'],
                password=payload['password'],
                first_name=payload['user_name'],
                is_active=payload['status'] == 'active',
            )
            UserRole.objects.create(
                user=user,
                role=payload['role'],
                company=payload['company'],
                site=payload['site'],
            )

        return Response({
            'success': True,
            'data': _user_response(user),
        }, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        user_role = self._request_role()
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return self._management_forbidden()

        target_user = self.get_object()
        forbidden_response = self._can_manage_target(user_role, target_user)
        if forbidden_response:
            return forbidden_response

        payload, error_response = _validate_user_payload(
            request.data,
            current_user=target_user,
            password_required=False,
        )
        if error_response:
            return error_response

        forbidden_response = self._can_manage_payload(user_role, payload)
        if forbidden_response:
            return forbidden_response

        with transaction.atomic():
            target_user.username = payload['login_id']
            target_user.first_name = payload['user_name']
            target_user.last_name = ''
            target_user.is_active = payload['status'] == 'active'
            if payload['password']:
                target_user.set_password(payload['password'])
            target_user.save()

            role_obj, _ = UserRole.objects.get_or_create(user=target_user)
            role_obj.role = payload['role']
            role_obj.company = payload['company']
            role_obj.site = payload['site']
            role_obj.save()

        return Response({
            'success': True,
            'data': _user_response(target_user),
        })

    def destroy(self, request, *args, **kwargs):
        user_role = self._request_role()
        if not user_role or user_role.role not in ('system_admin', 'company_admin'):
            return self._management_forbidden()

        target_user = self.get_object()
        if target_user.id == request.user.id:
            return _conflict('ログイン中の自分自身は削除できません')

        forbidden_response = self._can_manage_target(user_role, target_user)
        if forbidden_response:
            return forbidden_response

        target_user.is_active = False
        target_user.save(update_fields=['is_active'])

        return Response({
            'success': True,
            'message': '削除しました',
        })


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
                    ai_text=payload['ai_text'],
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

        payload, error_response = _validate_camera_payload(request.data, require_password=False)
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
        if payload['password']:
            camera.password = payload['password']
        camera.capture_interval_minutes = payload['capture_interval_minutes']
        camera.save_quality = IMAGE_QUALITY_TO_SAVE_QUALITY[payload['image_quality']]
        camera.save_days = payload['retention_days']
        camera.ai_text = payload['ai_text']
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

    @action(detail=True, methods=['delete'], url_path='images')
    def delete_images(self, request, pk=None):
        """カメラ保存済み画像一括削除API"""
        user_role = _user_role(request.user)
        if not user_role or user_role.role not in ('system_admin', 'company_admin', 'site_admin'):
            return _forbidden()

        camera = self.get_object()
        images = list(Image.objects.filter(camera=camera))
        deleted_files = _delete_image_files(images)
        image_ids = [image.id for image in images]

        with transaction.atomic():
            deleted_images = Image.objects.filter(id__in=image_ids).delete()[0] if image_ids else 0

        return Response({
            'success': True,
            'message': 'カメラの保存済み画像を削除しました',
            'data': {
                'camera_id': str(camera.id),
                'deleted_image_count': deleted_images,
                'deleted_file_count': deleted_files,
            },
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

    def _media_url(self, request, path):
        if not path:
            return None
        return request.build_absolute_uri(f'{settings.MEDIA_URL}{path}')

    def _image_item(self, request, image):
        return {
            'image_id': str(image.id),
            'camera_id': str(image.camera_id),
            'camera_name': image.camera.name,
            'captured_at': image.captured_at,
            'thumbnail_url': self._media_url(request, image.thumbnail_path or image.file_path),
            'image_url': self._media_url(request, image.file_path),
            'image_quality': _image_quality_from_save_quality(image.camera.save_quality),
            'width': image.width or None,
            'height': image.height or None,
            'file_size_bytes': image.file_size or None,
            'ai_analysis_status': image.ai_analysis_status,
            'ai_response_text': image.ai_response_text or None,
        }

    def _scoped_camera(self, camera_id):
        try:
            camera = Camera.objects.select_related('site', 'site__company').get(id=camera_id)
        except (Camera.DoesNotExist, ValueError):
            return None

        user_role = _user_role(self.request.user)
        if not user_role:
            return None
        if user_role.role == 'system_admin':
            return camera
        if user_role.role == 'company_admin' and user_role.company_id == camera.site.company_id:
            return camera
        if user_role.role in ('site_admin', 'general_user') and user_role.site_id == camera.site_id:
            return camera
        return None

    @action(detail=False, methods=['get'])
    def by_date_range(self, request):
        """指定カメラ、指定日付のサムネイル一覧"""
        camera_id = request.query_params.get('camera_id')
        date = request.query_params.get('date')

        if not camera_id:
            return _validation_error('入力内容に誤りがあります', {
                'camera_id': ['カメラIDを指定してください'],
            })
        if not date:
            return _validation_error('入力内容に誤りがあります', {
                'date': ['日付を指定してください'],
            })

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1

        try:
            page_size = max(1, int(request.query_params.get('page_size', 100)))
        except (TypeError, ValueError):
            page_size = 100

        camera = self._scoped_camera(camera_id)
        if not camera:
            return Response({
                'success': False,
                'code': 'IMAGE_NOT_FOUND',
                'message': 'カメラまたは画像が見つかりません',
            }, status=status.HTTP_404_NOT_FOUND)

        images = self.get_queryset().filter(camera=camera, captured_at__date=date)
        if request.query_params.get('sort') == 'captured_at_asc':
            images = images.order_by('captured_at')
        else:
            images = images.order_by('-captured_at')

        total_count = images.count()
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_images = images[start:start + page_size]

        return Response({
            'success': True,
            'data': {
                'camera': {
                    'camera_id': str(camera.id),
                    'camera_name': camera.name,
                },
                'date': date,
                'images': [self._image_item(request, image) for image in page_images],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total_count': total_count,
                    'total_pages': total_pages,
                },
            },
        })

    @action(detail=False, methods=['get'], url_path='thumbnails')
    def thumbnails(self, request):
        """指定カメラ、指定日付のサムネイル一覧"""
        camera_id = request.query_params.get('camera_id')
        date = request.query_params.get('date')

        if not camera_id:
            return _validation_error('入力内容に誤りがあります', {
                'camera_id': ['カメラIDを指定してください'],
            })
        if not date:
            return _validation_error('入力内容に誤りがあります', {
                'date': ['日付を指定してください'],
            })

        camera = self._scoped_camera(camera_id)
        if not camera:
            return Response({
                'success': False,
                'code': 'IMAGE_NOT_FOUND',
                'message': 'カメラまたは画像が見つかりません',
            }, status=status.HTTP_404_NOT_FOUND)

        images = self.get_queryset().filter(camera=camera, captured_at__date=date)
        if request.query_params.get('sort') == 'captured_at_asc':
            images = images.order_by('captured_at')
        else:
            images = images.order_by('-captured_at')

        return Response({
            'success': True,
            'data': {
                'camera': {
                    'camera_id': str(camera.id),
                    'camera_name': camera.name,
                },
                'date': date,
                'images': [self._image_item(request, image) for image in images],
                'pagination': {
                    'page': 1,
                    'page_size': len(images),
                    'total_count': len(images),
                    'total_pages': 1,
                },
            },
        })

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

    @action(detail=False, methods=['get'], url_path='available-dates')
    def available_dates(self, request):
        """フロント画面向けの画像存在日付一覧"""
        camera_id = request.query_params.get('camera_id')
        if not camera_id:
            return _validation_error('入力内容に誤りがあります', {
                'camera_id': ['カメラIDを指定してください'],
            })

        camera = self._scoped_camera(camera_id)
        if not camera:
            return Response({
                'success': False,
                'code': 'IMAGE_NOT_FOUND',
                'message': 'カメラまたは画像が見つかりません',
            }, status=status.HTTP_404_NOT_FOUND)

        grouped = {}
        for image in self.get_queryset().filter(camera=camera).order_by('-captured_at'):
            date = timezone.localtime(image.captured_at).date().isoformat()
            item = grouped.setdefault(date, {
                'date': date,
                'image_count': 0,
                'latest_captured_at': image.captured_at,
            })
            item['image_count'] += 1
            if image.captured_at > item['latest_captured_at']:
                item['latest_captured_at'] = image.captured_at

        available_dates = list(grouped.values())
        return Response({
            'success': True,
            'data': {
                'available_dates': available_dates,
                'default_date': available_dates[0]['date'] if available_dates else '',
            },
        })

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
                    'image_url': self._media_url(request, latest.file_path) if latest else None,
                    'thumbnail_url': self._media_url(request, latest.thumbnail_path or latest.file_path) if latest else None,
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

    @action(detail=False, methods=['post'], url_path='latest/bulk')
    def latest_bulk(self, request):
        """フロント画面向けの複数カメラ最新画像取得API"""
        return self.latest_images(request)


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
