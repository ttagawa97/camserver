"""
Models for camera image management system.
"""

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid


class Company(models.Model):
    """企業モデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Companies'

    def __str__(self):
        return self.name


class Site(models.Model):
    """現場モデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='sites')
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('company', 'code')

    def __str__(self):
        return f"{self.company.name} / {self.name}"


class Camera(models.Model):
    """カメラモデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name='cameras')
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    
    # カメラ接続情報
    url = models.URLField()
    username = models.CharField(max_length=255, blank=True, default='')
    password = models.CharField(max_length=255, blank=True, default='')
    
    # スケジュール設定
    capture_interval_minutes = models.IntegerField(default=1)  # 取得間隔（分）
    is_capturing = models.BooleanField(default=True)  # 取得中断フラグ
    
    # 画像設定
    save_quality = models.IntegerField(default=85)  # JPEG品質
    save_days = models.IntegerField(default=30)  # 保存日数
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_capture_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('site', 'code')

    def __str__(self):
        return f"{self.site.name} / {self.name}"


class Image(models.Model):
    """取得画像メタデータモデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name='images')
    
    # ファイルパス
    file_path = models.CharField(max_length=512)
    thumbnail_path = models.CharField(max_length=512, blank=True, default='')
    
    # メタデータ
    captured_at = models.DateTimeField()
    file_size = models.IntegerField(default=0)  # バイト
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['camera', '-captured_at']),
            models.Index(fields=['camera', 'captured_at']),
        ]

    def __str__(self):
        return f"{self.camera.name} - {self.captured_at}"


class CameraSchedule(models.Model):
    """カメラ取得スケジュール（APScheduler用）"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.OneToOneField(Camera, on_delete=models.CASCADE, related_name='schedule')
    
    # APScheduler用ジョブID
    job_id = models.CharField(max_length=255, unique=True)
    
    # スケジュール状態
    is_running = models.BooleanField(default=False)
    next_run_time = models.DateTimeField(null=True, blank=True)
    last_run_time = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Schedule: {self.camera.name}"


class UserRole(models.Model):
    """ユーザー権限割り当てモデル"""
    ROLE_CHOICES = [
        ('system_admin', 'システム管理者'),
        ('company_admin', '企業管理者'),
        ('site_admin', '現場管理者'),
        ('general_user', '一般ユーザー'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='role')
    
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='general_user')
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)
    site = models.ForeignKey(Site, on_delete=models.SET_NULL, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"
