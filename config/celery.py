import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("peter_dashboard")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.imports = (
    "integrations.tasks",
    "operations.tasks",
    "analytics.tasks",
)
app.autodiscover_tasks()

# Fallback beat schedule (django-celery-beat PeriodicTasks take precedence with DatabaseScheduler)
app.conf.beat_schedule = {
    "daily-process-cancellations": {
        "task": "operations.tasks.process_daily_cancellations",
        "schedule": crontab(hour=23, minute=5),
        "options": {"expires": 3600},
    },
    "daily-sync-jobber": {
        "task": "integrations.tasks.sync_jobber_full",
        "schedule": crontab(hour=22, minute=30),
        "options": {"expires": 7200},
    },
    "daily-sync-internal": {
        "task": "analytics.tasks.sync_admin_internal",
        "schedule": crontab(hour=22, minute=45),
        "options": {"expires": 3600},
    },
    "daily-sync-pricing": {
        "task": "analytics.tasks.sync_pricing_calculator",
        "schedule": crontab(hour=23, minute=0),
        "options": {"expires": 3600},
    },
}
