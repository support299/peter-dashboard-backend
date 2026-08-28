from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from integrations.models import Integration
from integrations.sync import JobberSyncService
from operations.models import CancellationRecord, Employee, JobberTask


class Command(BaseCommand):
    help = "Backfill Jobber Cancelled Visit/Job tasks into the dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--from-year", type=int, default=2023)
        parser.add_argument("--to-year", type=int, default=None)

    def handle(self, *args, **options):
        integration = (
            Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
        )
        if not integration:
            self.stderr.write("No active Jobber integration.")
            return

        service = JobberSyncService(integration)
        service.query_fields = {"tasks"}  # enough for _has_root
        service._employee_cache = {
            e.external_id: e for e in Employee.objects.filter(integration=integration)
        }
        service._client_cache = {}
        service._job_cache = {}

        from_year = options["from_year"]
        to_year = options["to_year"] or datetime.now(timezone.utc).year

        for year in range(from_year, to_year + 1):
            after = f"{year}-01-01T00:00:00Z"
            before = f"{year}-12-31T23:59:59Z"
            self.stdout.write(f"Syncing cancellation tasks for {year}...")
            service._sync_tasks(created_after=after, created_before=before, cancellation_only=True)
            self.stdout.write(
                f"  window done — stored this pass: {service.counts.get('tasks', 0)} "
                f"(cancellation matches: {service.counts.get('cancellation_tasks', 0)})"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. JobberTask={JobberTask.objects.count()} "
                f"CancellationRecord={CancellationRecord.objects.count()}"
            )
        )
