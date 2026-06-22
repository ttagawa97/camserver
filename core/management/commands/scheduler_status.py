"""
Management command to show scheduler status.
"""

import logging
from django.core.management.base import BaseCommand
from camserver.scheduler import scheduler_instance

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Show APScheduler status and active jobs'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('APScheduler Status'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
        
        try:
            info = scheduler_instance.get_scheduler_info()
            
            if info['running']:
                self.stdout.write(self.style.SUCCESS(f'Status: RUNNING ✓'))
            else:
                self.stdout.write(self.style.ERROR(f'Status: STOPPED ✗'))
            
            self.stdout.write(f'Active Jobs: {info["jobs_count"]}')
            self.stdout.write('')
            
            if info['jobs']:
                self.stdout.write(self.style.SUCCESS('Jobs:'))
                for job in info['jobs']:
                    self.stdout.write(f'  - ID: {job["id"]}')
                    self.stdout.write(f'    Name: {job["name"]}')
                    self.stdout.write(f'    Next Run: {job["next_run_time"]}')
                    self.stdout.write(f'    Trigger: {job["trigger"]}')
                    self.stdout.write('')
            else:
                self.stdout.write(self.style.WARNING('No active jobs'))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {str(e)}'))
