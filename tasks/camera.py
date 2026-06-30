"""
Camera image capture tasks.
"""

import logging
import os
import subprocess
import base64
from io import BytesIO
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from requests.auth import HTTPBasicAuth
from PIL import Image as PILImage, ImageOps

from django.utils import timezone
from django.conf import settings
from core.models import Camera, Image, CameraSchedule

logger = logging.getLogger(__name__)

_WINDOWS_HOST_CACHE = None

IMAGE_QUALITY_TO_SAVE_QUALITY = {
    'VGA': 70,
    'SVGA': 75,
    'WXGA': 80,
    'HD': 85,
    'FullHD': 90,
    '4K': 95,
}

IMAGE_QUALITY_SIZES = {
    'VGA': (640, 480),
    'SVGA': (800, 600),
    'WXGA': (1280, 800),
    'HD': (1280, 720),
    'FullHD': (1920, 1080),
    '4K': (3840, 2160),
}

AI_ERROR_TEXT = 'API ERROR'


def _is_wsl():
    try:
        with open('/proc/sys/kernel/osrelease', 'r', encoding='utf-8') as f:
            return 'microsoft' in f.read().lower()
    except OSError:
        return False


def _windows_host_candidates():
    """WSLからWindows側localhostサービスへ接続するための候補IPを返す。"""
    global _WINDOWS_HOST_CACHE

    configured = getattr(settings, 'CAMERA_LOCALHOST_FALLBACK_HOSTS', [])
    if isinstance(configured, str):
        configured = [host.strip() for host in configured.split(',') if host.strip()]

    candidates = list(configured)
    if not _is_wsl():
        return candidates

    if _WINDOWS_HOST_CACHE is not None:
        return candidates + _WINDOWS_HOST_CACHE

    discovered = []
    try:
        result = subprocess.run(
            [
                'powershell.exe',
                '-NoProfile',
                '-Command',
                "Get-NetIPAddress -AddressFamily IPv4 | "
                "Sort-Object @{Expression={if ($_.InterfaceAlias -like '*WSL*') {0} elseif ($_.InterfaceAlias -like '*Wi-Fi*' -or $_.InterfaceAlias -like '*Ethernet*') {1} else {2}}} | "
                "ForEach-Object { $_.IPAddress }",
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.splitlines():
                ip = line.strip()
                if ip and not ip.startswith(('127.', '169.254.')) and ip not in discovered:
                    discovered.append(ip)
    except Exception as e:
        logger.debug(f'Failed to discover Windows host IPs: {e}')

    _WINDOWS_HOST_CACHE = discovered
    return candidates + discovered


def _camera_url_candidates(url):
    parsed = urlparse(url)
    if parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        return [url]

    candidates = [url]
    for host in _windows_host_candidates():
        netloc = host
        if parsed.port:
            netloc = f'{host}:{parsed.port}'
        candidate = urlunparse(parsed._replace(netloc=netloc))
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _camera_auth(camera):
    if camera.username and camera.password:
        return HTTPBasicAuth(camera.username, camera.password)
    return None


def _request_camera(camera, timeout):
    last_error = None
    for url in _camera_url_candidates(camera.url):
        try:
            response = requests.get(
                url,
                auth=_camera_auth(camera),
                timeout=timeout,
                verify=False,
            )
            response.effective_camera_url = url
            return response
        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"Camera {camera.id} request failed for {url}: {e}")

    if last_error:
        raise last_error
    raise requests.exceptions.ConnectionError('No camera URL candidates available')


def _resolve_camera(camera_or_id):
    if isinstance(camera_or_id, Camera):
        return camera_or_id

    try:
        return Camera.objects.select_related('site', 'site__company').get(id=camera_or_id)
    except (Camera.DoesNotExist, ValueError, TypeError):
        logger.error(f"Camera not found for capture: {camera_or_id}")
        return None


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


def _image_quality_from_save_quality(save_quality):
    quality = 'HD'
    for label, value in IMAGE_QUALITY_TO_SAVE_QUALITY.items():
        if save_quality >= value:
            quality = label
    return quality


def convert_image_for_storage(image_bytes, camera):
    """
    取得画像をカメラ設定の保存画質に合わせ、保存用JPEGへ変換する。
    """
    quality_label = _image_quality_from_save_quality(camera.save_quality)
    target_size = IMAGE_QUALITY_SIZES[quality_label]

    with PILImage.open(BytesIO(image_bytes)) as pil_image:
        pil_image = ImageOps.exif_transpose(pil_image)
        pil_image.thumbnail(target_size, PILImage.Resampling.LANCZOS)

        if pil_image.mode not in ('RGB', 'L'):
            pil_image = pil_image.convert('RGB')

        output = BytesIO()
        pil_image.save(output, 'JPEG', quality=camera.save_quality, optimize=True)
        converted_bytes = output.getvalue()
        width, height = pil_image.size

    return converted_bytes, width, height


def _truncate_ai_text(text):
    max_length = getattr(settings, 'OPENAI_AI_RESPONSE_MAX_LENGTH', 256)
    return (text or '')[:max_length]


def _extract_openai_text(response_json):
    output_text = response_json.get('output_text')
    if isinstance(output_text, str) and output_text:
        return output_text

    parts = []
    for item in response_json.get('output', []):
        for content in item.get('content', []):
            text = content.get('text')
            if isinstance(text, str):
                parts.append(text)
    return '\n'.join(parts)


def _call_openai_image_analysis(image, prompt):
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is not configured')

    image_path = os.path.join(settings.MEDIA_ROOT, image.file_path)
    with open(image_path, 'rb') as f:
        encoded_image = base64.b64encode(f.read()).decode('ascii')

    response = requests.post(
        'https://api.openai.com/v1/responses',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': getattr(settings, 'OPENAI_MODEL', 'gpt-5.5'),
            'input': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': prompt},
                        {
                            'type': 'input_image',
                            'image_url': f'data:image/jpeg;base64,{encoded_image}',
                        },
                    ],
                },
            ],
        },
        timeout=getattr(settings, 'OPENAI_AI_ANALYSIS_TIMEOUT_SECONDS', 60),
    )
    response.raise_for_status()
    return _extract_openai_text(response.json())


def analyze_image_with_ai(image):
    """
    保存済み画像1件をAI解析し、結果をDBへ保存する。
    """
    prompt = (image.camera.ai_text or '').strip()
    if not prompt:
        image.ai_analysis_status = Image.AI_STATUS_NOT_REQUIRED
        image.ai_response_text = ''
        image.ai_error_message = ''
        image.save(update_fields=['ai_analysis_status', 'ai_response_text', 'ai_error_message', 'updated_at'])
        return image

    image.ai_analysis_status = Image.AI_STATUS_PROCESSING
    image.ai_requested_at = timezone.now()
    image.save(update_fields=['ai_analysis_status', 'ai_requested_at', 'updated_at'])

    try:
        ai_text = _call_openai_image_analysis(image, prompt)
        image.ai_response_text = _truncate_ai_text(ai_text)
        image.ai_analysis_status = Image.AI_STATUS_COMPLETED
        image.ai_error_message = ''
    except Exception as e:
        logger.error(f"AI analysis failed for image {image.id}: {str(e)}")
        image.ai_response_text = AI_ERROR_TEXT
        image.ai_analysis_status = Image.AI_STATUS_ERROR
        image.ai_error_message = str(e)[:512]

    image.ai_responded_at = timezone.now()
    image.save(update_fields=[
        'ai_analysis_status',
        'ai_response_text',
        'ai_error_message',
        'ai_responded_at',
        'updated_at',
    ])
    return image


def process_pending_ai_analysis(limit=1):
    """
    AI解析待ち画像を最新順（LIFO）に処理する。
    """
    processed = 0
    images = (
        Image.objects
        .filter(ai_analysis_status=Image.AI_STATUS_PENDING)
        .select_related('camera')
        .order_by('-captured_at', '-created_at')[:limit]
    )

    for image in images:
        analyze_image_with_ai(image)
        processed += 1

    return processed


def test_camera_connection(camera):
    """
    カメラ接続テスト
    """
    try:
        logger.info(f"Testing connection to camera {camera.id}: {camera.name}")

        response = _request_camera(camera, timeout=10)

        if response.status_code == 200:
            logger.info(f"Camera {camera.id} connection test: SUCCESS")
            return {
                'success': True,
                'message': 'カメラへの接続に成功しました',
                'status_code': response.status_code,
                'content_length': len(response.content),
                'effective_url': getattr(response, 'effective_camera_url', camera.url),
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
    camera = _resolve_camera(camera)
    if camera is None:
        return None

    try:
        logger.info(f"Starting to capture image from camera {camera.id}: {camera.name}")

        # カメラ接続テスト
        test_result = test_camera_connection(camera)
        if not test_result['success']:
            logger.error(f"Camera {camera.id} connection test failed: {test_result['message']}")
            return None

        # 画像ダウンロード
        response = _request_camera(camera, timeout=30)

        if response.status_code != 200:
            logger.error(f"Failed to capture image from camera {camera.id}: status {response.status_code}")
            return None

        # 保存画質に変換
        image_bytes = response.content
        logger.info(f"Downloaded {len(image_bytes)} bytes from camera {camera.id}")

        try:
            storage_image_bytes, width, height = convert_image_for_storage(image_bytes, camera)
        except Exception as e:
            logger.error(f"Failed to convert image from camera {camera.id}: {str(e)}")
            return None

        # 保存パスとファイル名を生成
        storage_path = get_image_storage_path(camera)
        filename = generate_image_filename(camera)
        file_path = os.path.join(storage_path, filename)

        # 変換後画像を保存
        with open(file_path, 'wb') as f:
            f.write(storage_image_bytes)

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
            file_size=len(storage_image_bytes),
            width=width,
            height=height,
            ai_analysis_status=(
                Image.AI_STATUS_PENDING
                if (camera.ai_text or '').strip()
                else Image.AI_STATUS_NOT_REQUIRED
            ),
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
