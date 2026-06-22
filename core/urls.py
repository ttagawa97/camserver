"""
URL routing for core application.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views import (
    CompanyViewSet, SiteViewSet, CameraViewSet, ImageViewSet,
    CameraScheduleViewSet, AuthViewSet
)

router = DefaultRouter()
router.register(r'companies', CompanyViewSet)
router.register(r'sites', SiteViewSet)
router.register(r'cameras', CameraViewSet)
router.register(r'images', ImageViewSet)
router.register(r'schedules', CameraScheduleViewSet)
router.register(r'auth', AuthViewSet, basename='auth')

urlpatterns = [
    path('', include(router.urls)),
]
