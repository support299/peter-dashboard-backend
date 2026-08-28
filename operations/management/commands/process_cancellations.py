from django.core.management.base import BaseCommand

from integrations.models import Integration
from operations.cancellations import process_all_cancellation_tasks
from operations.taxonomy import seed_taxonomy_defaults


class Command(BaseCommand):
    help = "Process Jobber Cancelled Visit / Cancelled Job tasks into cancellation records (daily workflow)."

    def handle(self, *args, **options):
        seed_taxonomy_defaults()
        integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
        if not integration:
            self.stderr.write("No active Jobber integration.")
            return
        stats = process_all_cancellation_tasks(integration)
        self.stdout.write(self.style.SUCCESS(f"Cancellations processed: {stats}"))
