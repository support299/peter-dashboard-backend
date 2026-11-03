import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("peter_dashboard")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.imports = (
    "integrations.tasks",
    "operations.tasks",
    "analytics.tasks",
    "financials.tasks",
    "insights.tasks",
)
app.autodiscover_tasks()

# No schedule here: the DatabaseScheduler copies these entries into the database as extra tasks,
# so every job ran twice. The schedule lives in the database (manage.py setup_celery_schedules).
app.conf.beat_schedule = {}
