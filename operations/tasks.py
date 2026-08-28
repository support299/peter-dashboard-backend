from celery import shared_task


@shared_task(name="operations.tasks.process_daily_cancellations", bind=True, max_retries=2)
def process_daily_cancellations(self):
    """End-of-day: turn Jobber Cancelled Visit/Job tasks into CancellationRecords."""
    from integrations.models import Integration
    from operations.cancellations import process_all_cancellation_tasks
    from operations.taxonomy import seed_taxonomy_defaults

    seed_taxonomy_defaults()
    integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
    if not integration:
        return {"ok": False, "error": "no_active_integration"}
    stats = process_all_cancellation_tasks(integration)
    return {"ok": True, **stats}
