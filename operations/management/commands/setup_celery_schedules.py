from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask


class Command(BaseCommand):
    help = "Seed django-celery-beat periodic tasks for daily Jobber / Internal / Pricing / cancellations jobs."

    def handle(self, *args, **options):
        schedules = [
            {
                "name": "Daily Jobber full sync",
                "task": "integrations.tasks.sync_jobber_full",
                "hour": "22",
                "minute": "30",
            },
            {
                "name": "Daily Admin Internal sync",
                "task": "analytics.tasks.sync_admin_internal",
                "hour": "22",
                "minute": "45",
            },
            {
                "name": "Daily Pricing Calculator sync",
                "task": "analytics.tasks.sync_pricing_calculator",
                "hour": "23",
                "minute": "0",
            },
            {
                "name": "Daily Cancelled Visit/Job processing",
                "task": "operations.tasks.process_daily_cancellations",
                "hour": "23",
                "minute": "5",
            },
        ]
        for item in schedules:
            crontab, _ = CrontabSchedule.objects.get_or_create(
                minute=item["minute"],
                hour=item["hour"],
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )
            PeriodicTask.objects.update_or_create(
                name=item["name"],
                defaults={
                    "task": item["task"],
                    "crontab": crontab,
                    "interval": None,
                    "enabled": True,
                },
            )
            self.stdout.write(self.style.SUCCESS(f"Scheduled: {item['name']} @ {item['hour']}:{item['minute']} UTC"))
