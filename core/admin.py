"""
Admin configuration for core application.
"""

from django.contrib import admin
from core.models import Company, Site, Camera, Image, CameraSchedule, UserRole


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('code', 'name')
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'company', 'is_active', 'created_at')
    list_filter = ('is_active', 'company', 'created_at')
    search_fields = ('code', 'name')
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'site', 'capture_interval_minutes', 'is_capturing', 'last_capture_at')
    list_filter = ('is_capturing', 'is_active', 'created_at')
    search_fields = ('code', 'name', 'url')
    readonly_fields = ('id', 'created_at', 'updated_at', 'last_capture_at')
    fieldsets = (
        ('基本情報', {
            'fields': ('id', 'site', 'code', 'name', 'description')
        }),
        ('接続設定', {
            'fields': ('url', 'username', 'password')
        }),
        ('取得設定', {
            'fields': ('capture_interval_minutes', 'is_capturing', 'save_quality', 'save_days', 'ai_text')
        }),
        ('ステータス', {
            'fields': ('is_active', 'last_capture_at', 'created_at', 'updated_at')
        }),
    )


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = ('camera', 'captured_at', 'file_size', 'width', 'height', 'ai_analysis_status')
    list_filter = ('camera', 'ai_analysis_status', 'captured_at', 'created_at')
    search_fields = ('camera__name', 'file_path')
    readonly_fields = ('id', 'created_at', 'updated_at', 'ai_requested_at', 'ai_responded_at')


@admin.register(CameraSchedule)
class CameraScheduleAdmin(admin.ModelAdmin):
    list_display = ('camera', 'job_id', 'is_running', 'next_run_time', 'last_run_time')
    list_filter = ('is_running', 'created_at')
    search_fields = ('job_id', 'camera__name')
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'company', 'site', 'created_at')
    list_filter = ('role', 'created_at')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('id', 'created_at', 'updated_at')
