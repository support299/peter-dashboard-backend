from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask

# Copies of the old schedule in config/celery.py, which made every job run twice.
OLD_DUPLICATES = ["daily-process-cancellations", "daily-sync-jobber", "daily-sync-internal", "daily-recurring-expenses", "daily-sync-pricing"]


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
                # After the Hub's ~08:30 UTC push of Jobber timesheet hours.
                "hour": "9",
                "minute": "0",
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
            {
                "name": "Daily recurring expenses",
                "task": "financials.tasks.generate_recurring_expenses",
                # 10:00 UTC = early morning in Quebec, so today's charges are ready to verify.
                "hour": "10",
                "minute": "0",
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
        removed, _ = PeriodicTask.objects.filter(name__in=OLD_DUPLICATES).delete()
        if removed:
            self.stdout.write(self.style.SUCCESS(f"Removed {removed} duplicate schedule(s) from the old config"))
