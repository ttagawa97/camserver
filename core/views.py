"""
Views for core application REST API.
"""

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from django.contrib.auth.models import User
from django.utils import timezone
from django.shortcuts import get_object_or_404
from datetime import datetime, timedelta

from core.models import Company, Site, Camera, Image, CameraSchedule, UserRole
from core.serializers import (
    CompanySerializer, SiteSerializer, CameraSerializer, ImageSerializer,
    CameraScheduleSerializer, UserRoleSerializer, UserSerializer
)
from tasks.camera import capture_camera_image, test_camera_connection


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

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """カメラ接続テスト"""
        camera = self.get_object()
        result = test_camera_connection(camera)
        return Response(result, status=status.HTTP_200_OK if result['success'] else status.HTTP_400_BAD_REQUEST)

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

    @action(detail=False, methods=['get'])
    def latest_images(self, request):
        """複数カメラの最新画像"""
        camera_ids = request.query_params.getlist('camera_ids')

        if not camera_ids:
            return Response({'error': 'camera_ids required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            cameras = Camera.objects.filter(id__in=camera_ids)
            result = {}
            
            for camera in cameras:
                latest = Image.objects.filter(camera=camera).first()
                result[str(camera.id)] = ImageSerializer(latest).data if latest else None
            
            return Response(result)
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

        try:
            user = User.objects.get(username=username)
            if user.check_password(password):
                return Response({
                    'success': True,
                    'user': UserSerializer(user).data
                })
            else:
                return Response({
                    'success': False,
                    'message': 'パスワードが間違っています'
                }, status=status.HTTP_401_UNAUTHORIZED)
        except User.DoesNotExist:
            return Response({
                'success': False,
                'message': 'ユーザーが見つかりません'
            }, status=status.HTTP_401_UNAUTHORIZED)

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        """ログインユーザー情報"""
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def logout(self, request):
        """ログアウト"""
        return Response({'success': True, 'message': 'ログアウト成功'})
