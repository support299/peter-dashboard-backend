"""Configurable Dashboard / Widget listing (future layout engine)."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from analytics.models import Dashboard, Widget


@require_GET
def dashboards(request):
    rows = Dashboard.objects.filter(is_active=True).prefetch_related("widgets").order_by("name")
    return JsonResponse(
        {
            "ok": True,
            "results": [
                {
                    "id": d.id,
                    "slug": d.slug,
                    "name": d.name,
                    "description": d.description,
                    "layout": d.layout or {},
                    "widgets": [
                        {
                            "id": w.id,
                            "title": w.title,
                            "widget_type": w.widget_type,
                            "kpi_keys": w.kpi_keys or [],
                            "config": w.config or {},
                            "position": w.position,
                        }
                        for w in d.widgets.all().order_by("position", "id")
                    ],
                }
                for d in rows
            ],
        }
    )


@require_GET
def dashboard_detail(request, slug):
    d = Dashboard.objects.filter(slug=slug, is_active=True).prefetch_related("widgets").first()
    if not d:
        return JsonResponse({"ok": False, "error": "Not found"}, status=404)
    return JsonResponse(
        {
            "ok": True,
            "dashboard": {
                "id": d.id,
                "slug": d.slug,
                "name": d.name,
                "description": d.description,
                "layout": d.layout or {},
                "widgets": [
                    {
                        "id": w.id,
                        "title": w.title,
                        "widget_type": w.widget_type,
                        "kpi_keys": w.kpi_keys or [],
                        "config": w.config or {},
                        "position": w.position,
                    }
                    for w in d.widgets.all().order_by("position", "id")
                ],
            },
        }
    )
