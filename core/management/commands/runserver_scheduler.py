"""
Management command to start the development server with scheduler.
"""

import logging
from django.core.management.base import BaseCommand
from django.core.management import call_command
from camserver.scheduler import scheduler_instance

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run development server with APScheduler'

    def add_arguments(self, parser):
        parser.add_argument('--noreload', action='store_true',
                          dest='noreload',
                          help='Tells Django to NOT use the auto-reloader.')
        parser.add_argument('--nothreading', action='store_true',
                          dest='nothreading',
                          help='Tells Django to NOT use threading in the development server.')

    def handle(self, *args, **options):
        """Start scheduler before running development server"""
        
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('Camera Image Management System - Development Server'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
        
        # Start APScheduler
        try:
            scheduler_instance.start()
            self.stdout.write(self.style.SUCCESS('APScheduler started successfully'))
            scheduler_info = scheduler_instance.get_scheduler_info()
            self.stdout.write(f'Active jobs: {scheduler_info["jobs_count"]}')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed to start APScheduler: {str(e)}'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Starting Django development server...'))
        self.stdout.write('')

        # Run development server
        try:
            call_command('runserver', *args, **options)
        except KeyboardInterrupt:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Shutting down server...'))
            scheduler_instance.stop()
            self.stdout.write(self.style.SUCCESS('APScheduler stopped'))
