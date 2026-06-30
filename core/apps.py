"""
App configuration for core application.
"""

import logging
import sys
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    verbose_name = 'Camera Management Core'
    _scheduler_started = False

    def ready(self):
        """Initialize scheduler when app is ready."""
        if any(command in sys.argv for command in ('makemigrations', 'migrate', 'test')):
            return

        # 重複起動を防ぐためのフラグチェック
        if CoreConfig._scheduler_started:
            return

        # スケジューラーを起動
        try:
            from camserver.scheduler import scheduler_instance
            logger.info("Starting APScheduler on Django app startup...")
            scheduler_instance.start()
            CoreConfig._scheduler_started = True
            logger.info("APScheduler started successfully")
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {str(e)}")
