from celery import shared_task


@shared_task(name="integrations.tasks.sync_jobber_full", bind=True, max_retries=1, soft_time_limit=3600, time_limit=3900)
def sync_jobber_full(self):
    """Daily Jobber full sync (entities + tasks + metric facts + cancellations)."""
    from integrations.models import Integration, SyncRun
    from integrations.sync import JobberSyncService

    integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
    if not integration:
        return {"ok": False, "error": "no_active_integration"}
    run = SyncRun.objects.create(
        integration=integration,
        kind=SyncRun.KIND_FULL,
        status=SyncRun.STATUS_QUEUED,
    )
    try:
        JobberSyncService(integration, sync_run=run).run_full()
        return {"ok": True, "sync_run_id": run.id, "counts": run.entity_counts}
    except Exception as exc:
        run.refresh_from_db()
        return {"ok": False, "sync_run_id": run.id, "error": str(exc)}
