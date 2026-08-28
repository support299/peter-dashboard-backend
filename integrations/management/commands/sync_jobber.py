from django.core.management.base import BaseCommand, CommandError

from integrations.models import Integration, SyncRun
from integrations.sync import JobberSyncService


class Command(BaseCommand):
    help = "Run a full Jobber sync for the connected account."

    def handle(self, *args, **options):
        integration = (
            Integration.objects.filter(provider=Integration.PROVIDER_JOBBER)
            .exclude(status=Integration.STATUS_DISCONNECTED)
            .order_by("-updated_at")
            .first()
        )
        if not integration or not integration.access_token:
            raise CommandError("No connected Jobber account. Complete OAuth first.")
        run = SyncRun.objects.create(integration=integration, kind=SyncRun.KIND_FULL, status=SyncRun.STATUS_QUEUED)
        self.stdout.write("Starting Jobber full sync...")
        JobberSyncService(integration, sync_run=run).run_full()
        self.stdout.write(self.style.SUCCESS(f"Sync finished: {run.status} {run.entity_counts}"))
