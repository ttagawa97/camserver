"""
Camera image capture tasks.
"""

import logging
import os
from io import BytesIO
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from PIL import Image as PILImage

from django.utils import timezone
from django.conf import settings
from core.models import Camera, Image, CameraSchedule

logger = logging.getLogger(__name__)


def get_image_storage_path(camera):
    """
    画像保存パスを生成
    media/company_code/site_code/camera_code/YYYY/MM/DD/
    """
    site = camera.site
    company = site.company

    year = timezone.now().strftime('%Y')
    month = timezone.now().strftime('%m')
    day = timezone.now().strftime('%d')

    path = os.path.join(
        settings.MEDIA_ROOT,
        company.code,
        site.code,
        camera.code,
        year,
        month,
        day
    )

    os.makedirs(path, exist_ok=True)
    return path


def generate_image_filename(camera):
    """
    画像ファイル名を生成
    YYYYMMDD_HHmmss_microsecond.jpg
    """
    now = timezone.now()
    filename = now.strftime('%Y%m%d_%H%M%S_%f') + '.jpg'
    return filename


def test_camera_connection(camera):
    """
    カメラ接続テスト
    """
    try:
        logger.info(f"Testing connection to camera {camera.id}: {camera.name}")

        auth = None
        if camera.username and camera.password:
            auth = HTTPBasicAuth(camera.username, camera.password)

        response = requests.get(
            camera.url,
            auth=auth,
            timeout=10,
            verify=False  # 開発環境用
        )

        if response.status_code == 200:
            logger.info(f"Camera {camera.id} connection test: SUCCESS")
            return {
                'success': True,
                'message': 'カメラへの接続に成功しました',
                'status_code': response.status_code,
                'content_length': len(response.content)
            }
        else:
            logger.warning(f"Camera {camera.id} returned status code {response.status_code}")
            return {
                'success': False,
                'message': f'カメラが {response.status_code} を返しました',
                'status_code': response.status_code
            }

    except requests.exceptions.Timeout:
        logger.error(f"Camera {camera.id} connection timeout")
        return {
            'success': False,
            'message': 'カメラへの接続がタイムアウトしました',
            'error': 'timeout'
        }
    except requests.exceptions.ConnectionError:
        logger.error(f"Camera {camera.id} connection error")
        return {
            'success': False,
            'message': 'カメラへの接続に失敗しました',
            'error': 'connection_error'
        }
    except Exception as e:
        logger.error(f"Camera {camera.id} connection test error: {str(e)}")
        return {
            'success': False,
            'message': f'テスト中にエラーが発生しました: {str(e)}',
            'error': 'unknown_error'
        }


def capture_camera_image(camera):
    """
    カメラから画像を取得して保存
    """
    try:
        logger.info(f"Starting to capture image from camera {camera.id}: {camera.name}")

        # カメラ接続テスト
        test_result = test_camera_connection(camera)
        if not test_result['success']:
            logger.error(f"Camera {camera.id} connection test failed: {test_result['message']}")
            return None

        # 画像ダウンロード
        auth = None
        if camera.username and camera.password:
            auth = HTTPBasicAuth(camera.username, camera.password)

        response = requests.get(
            camera.url,
            auth=auth,
            timeout=30,
            verify=False
        )

        if response.status_code != 200:
            logger.error(f"Failed to capture image from camera {camera.id}: status {response.status_code}")
            return None

        # 画像を保存
        image_bytes = response.content
        logger.info(f"Downloaded {len(image_bytes)} bytes from camera {camera.id}")

        # 保存パスとファイル名を生成
        storage_path = get_image_storage_path(camera)
        filename = generate_image_filename(camera)
        file_path = os.path.join(storage_path, filename)

        # 元画像を保存
        with open(file_path, 'wb') as f:
            f.write(image_bytes)

        # 画像メタデータを取得
        try:
            pil_image = PILImage.open(BytesIO(image_bytes))
            width, height = pil_image.size
            pil_image.close()
        except Exception as e:
            logger.warning(f"Failed to get image metadata: {str(e)}")
            width, height = 0, 0

        # サムネイル生成
        thumbnail_path = generate_thumbnail(file_path, camera)

        # DBに記録
        relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT)
        relative_thumbnail_path = os.path.relpath(thumbnail_path, settings.MEDIA_ROOT) if thumbnail_path else ''

        image = Image.objects.create(
            camera=camera,
            file_path=relative_path,
            thumbnail_path=relative_thumbnail_path,
            captured_at=timezone.now(),
            file_size=len(image_bytes),
            width=width,
            height=height
        )

        # カメラの最終取得時刻を更新
        camera.last_capture_at = timezone.now()
        camera.save(update_fields=['last_capture_at'])

        # CameraScheduleの情報を更新
        try:
            schedule = CameraSchedule.objects.get(camera=camera)
            schedule.last_run_time = timezone.now()
            schedule.next_run_time = timezone.now()  # 実際にはAPSchedulerが更新
            schedule.save(update_fields=['last_run_time'])
        except CameraSchedule.DoesNotExist:
            pass

        logger.info(f"Successfully captured image from camera {camera.id}: {relative_path}")
        return image

    except Exception as e:
        logger.error(f"Unexpected error capturing image from camera {camera.id}: {str(e)}")
        return None


def generate_thumbnail(file_path, camera, size=(320, 240)):
    """
    サムネイル生成
    """
    try:
        logger.info(f"Generating thumbnail for {file_path}")

        # 元画像を開く
        pil_image = PILImage.open(file_path)

        # JPEG品質設定で変換
        pil_image.thumbnail(size, PILImage.Resampling.LANCZOS)

        # サムネイルパス
        base_path = os.path.splitext(file_path)[0]
        thumbnail_path = base_path + '_thumb.jpg'

        # 品質指定で保存
        pil_image.save(thumbnail_path, 'JPEG', quality=camera.save_quality)
        pil_image.close()

        logger.info(f"Thumbnail generated: {thumbnail_path}")
        return thumbnail_path

    except Exception as e:
        logger.error(f"Failed to generate thumbnail: {str(e)}")
        return None


def cleanup_old_images():
    """
    保存期間を超過した画像を削除
    """
    from datetime import timedelta

    logger.info("Starting cleanup of old images")

    cameras = Camera.objects.filter(is_active=True)
    deleted_count = 0

    for camera in cameras:
        # 保存期間を計算
        cutoff_date = timezone.now() - timedelta(days=camera.save_days)

        # 期限切れ画像を取得
        old_images = Image.objects.filter(
            camera=camera,
            captured_at__lt=cutoff_date
        )

        for image in old_images:
            try:
                # ファイルを削除
                file_path = os.path.join(settings.MEDIA_ROOT, image.file_path)
                if os.path.exists(file_path):
                    os.remove(file_path)

                if image.thumbnail_path:
                    thumb_path = os.path.join(settings.MEDIA_ROOT, image.thumbnail_path)
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)

                # DB記録を削除
                image.delete()
                deleted_count += 1
                logger.info(f"Deleted old image: {image.file_path}")

            except Exception as e:
                logger.error(f"Failed to delete image {image.id}: {str(e)}")

    logger.info(f"Cleanup completed: {deleted_count} images deleted")
    return deleted_count
