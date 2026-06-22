"""
ASGI config for camserver project.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'camserver.settings')

application = get_asgi_application()
