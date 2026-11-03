from celery import shared_task
from django.utils import timezone

from insights.models import InsightsSetting


@shared_task(name="insights.tasks.rebuild_insights_task")
def rebuild_insights_task():
    """Recalculate revenue classification, divisions and MRR (after line-item rules change)."""
    from insights.manage_views import RECLASSIFY_KEY
    from insights.rebuild import rebuild_insights
    from integrations.models import Integration

    status = InsightsSetting.get(RECLASSIFY_KEY, {}) or {}
    started = timezone.now().isoformat()
    InsightsSetting.put(RECLASSIFY_KEY, {**status, "state": "running", "started_at": started})
    try:
        integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
        stats = rebuild_insights(integration) if integration else {}
    except Exception as exc:
        InsightsSetting.put(RECLASSIFY_KEY, {**status, "state": "failed", "error": str(exc)[:300], "finished_at": timezone.now().isoformat()})
        raise
    InsightsSetting.put(
        RECLASSIFY_KEY,
        {"state": "done", "started_at": started, "finished_at": timezone.now().isoformat(), "invoices": (stats.get("revenue") or {}).get("invoices"), "error": ""},
    )
    return stats
