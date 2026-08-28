from celery import shared_task


@shared_task(name="analytics.tasks.sync_admin_internal", bind=True, max_retries=1, soft_time_limit=600, time_limit=900)
def sync_admin_internal(self):
    """Daily pull from Admin Internal App."""
    from analytics.sync import run_sync

    run = run_sync()
    return {
        "ok": run.status == "success",
        "status": run.status,
        "run_id": run.id,
        "error": run.error or "",
        "counts": run.counts or {},
    }


@shared_task(name="analytics.tasks.sync_pricing_calculator", bind=True, max_retries=1, soft_time_limit=900, time_limit=1200)
def sync_pricing_calculator(self):
    """Daily pull from Pricing Calculator."""
    from analytics.pricing_sync import run_pricing_sync

    run = run_pricing_sync()
    return {
        "ok": run.status == "success",
        "status": run.status,
        "run_id": run.id,
        "error": getattr(run, "error", "") or "",
        "counts": run.counts or {},
    }
