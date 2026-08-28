"""Trigger / inspect Celery background jobs from the API."""

from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods


TASK_MAP = {
    "sync_jobber": "integrations.tasks.sync_jobber_full",
    "sync_internal": "analytics.tasks.sync_admin_internal",
    "sync_pricing": "analytics.tasks.sync_pricing_calculator",
    "process_cancellations": "operations.tasks.process_daily_cancellations",
}


@require_GET
def celery_status(request):
    from django_celery_beat.models import PeriodicTask
    from django_celery_results.models import TaskResult

    schedules = list(
        PeriodicTask.objects.filter(enabled=True).values("name", "task", "last_run_at", "total_run_count")[:20]
    )
    recent = list(
        TaskResult.objects.order_by("-date_done").values("task_id", "task_name", "status", "date_done", "result")[:20]
    )
    return JsonResponse({"ok": True, "schedules": schedules, "recent": recent, "tasks": list(TASK_MAP.keys())})


@require_http_methods(["POST"])
def celery_run(request):
    import json

    from celery import current_app

    body = json.loads(request.body.decode() or "{}")
    key = (body.get("task") or "").strip()
    task_name = TASK_MAP.get(key)
    if not task_name:
        return JsonResponse({"ok": False, "error": f"Unknown task. Use one of: {', '.join(TASK_MAP)}"}, status=400)
    async_result = current_app.send_task(task_name)
    return JsonResponse({"ok": True, "task": key, "task_id": async_result.id})
