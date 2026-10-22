from django.core.management.base import BaseCommand, CommandError

from insights.rebuild import rebuild_insights
from integrations.models import Integration


class Command(BaseCommand):
    help = "Classify revenue by division/category, flag touch-up visits and rebuild MRR from Jobber recurring jobs."

    def handle(self, *args, **options):
        integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
        if not integration:
            raise CommandError("No active Jobber integration.")
        stats = rebuild_insights(integration)
        self.stdout.write(self.style.SUCCESS(f"Insights rebuilt: {stats}"))
