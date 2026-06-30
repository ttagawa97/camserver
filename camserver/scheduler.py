"""
APScheduler scheduler for camera image capture tasks.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class CameraSchedulerManager:
    """APSchedulerを管理するクラス"""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.scheduler.configure(
            timezone='Asia/Tokyo',
            job_defaults={'coalesce': True, 'max_instances': 1}
        )

    def start(self):
        """スケジューラー起動"""
        if not self.scheduler.running:
            logger.info("Starting APScheduler...")
            self.scheduler.start()
            # 既存のカメラスケジュール情報をDBから読み込み、ジョブ再登録
            self._restore_camera_schedules()
            self._schedule_ai_analysis()
            logger.info("APScheduler started successfully")

    def stop(self):
        """スケジューラー停止"""
        if self.scheduler.running:
            logger.info("Stopping APScheduler...")
            self.scheduler.shutdown(wait=True)
            logger.info("APScheduler stopped")

    def schedule_camera(self, camera):
        """カメラ取得ジョブをスケジュール"""
        from tasks.camera import capture_camera_image
        from core.models import CameraSchedule

        job_id = f"camera_{camera.id}"

        try:
            # 既存のジョブがあれば削除
            try:
                self.scheduler.remove_job(job_id)
            except:
                pass

            # 新規ジョブを登録
            if camera.is_capturing:
                job = self.scheduler.add_job(
                    func=capture_camera_image,
                    trigger=IntervalTrigger(minutes=camera.capture_interval_minutes),
                    args=[camera.id],
                    id=job_id,
                    name=f'Capture {camera.name}',
                    replace_existing=True
                )

                # DBのスケジュール情報を更新
                schedule, created = CameraSchedule.objects.get_or_create(camera=camera)
                schedule.job_id = job_id
                schedule.is_running = True
                schedule.next_run_time = getattr(job, 'next_run_time', None)
                schedule.save()

                logger.info(f"Scheduled camera {camera.id}: {camera.name} - interval: {camera.capture_interval_minutes}min")
            else:
                logger.info(f"Camera {camera.id} ({camera.name}) is disabled (is_capturing=False)")
        except Exception as e:
            logger.error(f"Failed to schedule camera {camera.id}: {str(e)}")
            raise

    def reschedule_camera(self, camera):
        """カメラ取得スケジュール再設定"""
        logger.info(f"Rescheduling camera {camera.id}: {camera.name}")
        self.schedule_camera(camera)

    def unschedule_camera(self, camera):
        """カメラ取得ジョブを削除"""
        from core.models import CameraSchedule

        job_id = f"camera_{camera.id}"

        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Unscheduled camera {camera.id}: {camera.name}")

            # DBのスケジュール情報を更新
            try:
                schedule = CameraSchedule.objects.get(camera=camera)
                schedule.is_running = False
                schedule.save()
            except CameraSchedule.DoesNotExist:
                pass
        except Exception as e:
            logger.warning(f"Failed to unschedule camera {camera.id}: {str(e)}")

    def _restore_camera_schedules(self):
        """DBから既存のカメラスケジュール情報を復元し、ジョブ再登録"""
        from core.models import Camera

        logger.info("Restoring camera schedules from database...")
        cameras = Camera.objects.filter(is_active=True, is_capturing=True)

        for camera in cameras:
            try:
                self.schedule_camera(camera)
            except Exception as e:
                logger.error(f"Failed to restore schedule for camera {camera.id}: {str(e)}")

    def _schedule_ai_analysis(self):
        """AI画像解析ジョブをスケジュール"""
        from tasks.camera import process_pending_ai_analysis

        interval_seconds = getattr(settings, 'OPENAI_AI_ANALYSIS_INTERVAL_SECONDS', 30)
        self.scheduler.add_job(
            func=process_pending_ai_analysis,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id='ai_image_analysis',
            name='AI image analysis',
            replace_existing=True,
        )
        logger.info(f"Scheduled AI image analysis job - interval: {interval_seconds}s")

    def get_scheduler_info(self):
        """スケジューラー情報を取得"""
        jobs_info = []
        for job in self.scheduler.get_jobs():
            jobs_info.append({
                'id': job.id,
                'name': job.name,
                'next_run_time': job.next_run_time,
                'trigger': str(job.trigger),
            })

        return {
            'running': self.scheduler.running,
            'jobs_count': len(jobs_info),
            'jobs': jobs_info
        }


# グローバルインスタンス
scheduler_instance = CameraSchedulerManager()
