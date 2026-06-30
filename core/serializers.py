"""
Serializers for core application.
"""

from rest_framework import serializers
from django.contrib.auth.models import User
from core.models import Company, Site, Camera, Image, CameraSchedule, UserRole


class CompanySerializer(serializers.ModelSerializer):
    site_count = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = ('id', 'code', 'name', 'description', 'is_active', 'site_count', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_site_count(self, obj):
        return obj.sites.count()


class SiteSerializer(serializers.ModelSerializer):
    camera_count = serializers.SerializerMethodField()

    class Meta:
        model = Site
        fields = ('id', 'company', 'code', 'name', 'description', 'is_active', 'camera_count', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_camera_count(self, obj):
        return obj.cameras.count()


class CameraSerializer(serializers.ModelSerializer):
    last_image = serializers.SerializerMethodField()
    image_count = serializers.SerializerMethodField()

    class Meta:
        model = Camera
        fields = (
            'id', 'site', 'code', 'name', 'description',
            'url', 'username',  # password は返さない
            'capture_interval_minutes', 'is_capturing', 'save_quality', 'save_days', 'ai_text',
            'is_active', 'last_capture_at', 'image_count', 'last_image',
            'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at', 'last_capture_at')
        extra_kwargs = {
            'password': {'write_only': True}
        }

    def get_last_image(self, obj):
        last_img = obj.images.first()
        if last_img:
            return ImageSerializer(last_img).data
        return None

    def get_image_count(self, obj):
        return obj.images.count()


class ImageSerializer(serializers.ModelSerializer):
    camera_name = serializers.CharField(source='camera.name', read_only=True)

    class Meta:
        model = Image
        fields = (
            'id', 'camera', 'camera_name', 'file_path', 'thumbnail_path',
            'captured_at', 'file_size', 'width', 'height',
            'ai_analysis_status', 'ai_response_text', 'ai_requested_at', 'ai_responded_at',
            'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class CameraScheduleSerializer(serializers.ModelSerializer):
    camera_name = serializers.CharField(source='camera.name', read_only=True)

    class Meta:
        model = CameraSchedule
        fields = (
            'id', 'camera', 'camera_name', 'job_id',
            'is_running', 'next_run_time', 'last_run_time',
            'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'job_id', 'created_at', 'updated_at')


class UserRoleSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model = UserRole
        fields = (
            'id', 'user', 'username', 'email', 'role',
            'company', 'site', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class UserSerializer(serializers.ModelSerializer):
    role = UserRoleSerializer(read_only=True)

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name', 'role', 'is_active')
        read_only_fields = ('id',)
