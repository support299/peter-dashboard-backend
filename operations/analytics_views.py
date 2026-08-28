"""
Extra Jobber analytics endpoints: one-off service types, cancellations, CX, filters.
Mounted from operations.views urlpatterns.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from integrations.models import Integration
from operations.cancellations import process_all_cancellation_tasks
from operations.models import (
    CANCELLATION_JOB,
    CANCELLATION_VISIT,
    CancellationRecord,
    Client,
    CustomerFeedback,
    DIVISION_CHOICES,
    DivisionRule,
    Employee,
    GoogleReview,
    Job,
    ServiceTypeMapping,
    Visit,
)
from operations.taxonomy import DIVISION_LABELS, UNCATEGORIZED, clear_taxonomy_cache, seed_taxonomy_defaults


def _integration():
    return Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()


def _money(value):
    if value is None:
        return 0
    return float(Decimal(value))


def _date_range(request):
    start = request.GET.get("from") or ""
    end = request.GET.get("to") or ""
    return start, end


def _apply_common_filters(qs, request, *, date_field="start_at"):
    start, end = _date_range(request)
    if start:
        qs = qs.filter(**{f"{date_field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{date_field}__date__lte": end})
    division = request.GET.get("division") or ""
    if division:
        qs = qs.filter(division=division)
    employee = request.GET.get("employee") or ""
    if employee and hasattr(qs.model, "assigned_employees"):
        qs = qs.filter(assigned_employees__id=employee)
    elif employee and hasattr(qs.model, "team_leader_id"):
        qs = qs.filter(team_leader_id=employee)
    team_leader = request.GET.get("team_leader") or ""
    if team_leader and hasattr(qs.model, "team_leader_id"):
        qs = qs.filter(team_leader_id=team_leader)
    client = request.GET.get("customer") or request.GET.get("client") or ""
    if client:
        qs = qs.filter(Q(client_id=client) | Q(client__external_id=client))
    service_type = request.GET.get("service_type") or ""
    if service_type and hasattr(qs.model, "service_type"):
        qs = qs.filter(service_type=service_type)
    city = request.GET.get("city") or ""
    if city:
        if hasattr(qs.model, "property"):
            qs = qs.filter(Q(property__city__icontains=city) | Q(client__billing_city__icontains=city))
        elif hasattr(qs.model, "billing_city"):
            qs = qs.filter(billing_city__icontains=city)
    source = request.GET.get("source") or request.GET.get("sales_source") or ""
    if source and hasattr(qs.model, "source"):
        qs = qs.filter(source__icontains=source)
    return qs.distinct()


@require_GET
def jobber_filters(request):
    integration = _integration()
    if not integration:
        return JsonResponse({"ok": True, "employees": [], "divisions": [], "service_types": [], "cities": [], "sources": []})
    employees = list(
        Employee.objects.filter(integration=integration)
        .exclude(full_name="")
        .order_by("full_name")
        .values("id", "full_name", "external_id")[:500]
    )
    cities = sorted(
        {
            *(PropertyCity for PropertyCity in PropertyCityQuery(integration)),
        }
    )
    sources = list(
        Job.objects.filter(integration=integration)
        .exclude(source="")
        .values_list("source", flat=True)
        .distinct()
        .order_by("source")[:100]
    )
    service_types = list(
        Job.objects.filter(integration=integration, is_one_off=True)
        .exclude(service_type="")
        .values_list("service_type", flat=True)
        .distinct()
        .order_by("service_type")
    )
    return JsonResponse(
        {
            "ok": True,
            "divisions": [{"key": k, "label": v} for k, v in DIVISION_CHOICES],
            "employees": [{"id": e["id"], "label": e["full_name"], "external_id": e["external_id"]} for e in employees],
            "service_types": service_types or [UNCATEGORIZED],
            "cities": cities,
            "sources": sources,
        }
    )


def PropertyCityQuery(integration):
    from operations.models import Property

    for city in Property.objects.filter(integration=integration).exclude(city="").values_list("city", flat=True).distinct():
        yield city
    for city in Client.objects.filter(integration=integration).exclude(billing_city="").values_list("billing_city", flat=True).distinct():
        yield city


@require_GET
def one_off_dashboard(request):
    integration = _integration()
    if not integration:
        return JsonResponse({"ok": True, "kpis": [], "by_service_type": [], "charts": {}})
    jobs = Job.objects.filter(integration=integration, is_one_off=True)
    jobs = _apply_common_filters(jobs, request, date_field="start_at")
    service_type = request.GET.get("service_type")
    if service_type:
        jobs = jobs.filter(service_type=service_type)

    by_type = list(
        jobs.values("service_type")
        .annotate(count=Count("id"), revenue=Sum("total"), avg_price=Avg("total"))
        .order_by("-count")
    )
    total = jobs.count()
    revenue = jobs.aggregate(s=Sum("total"))["s"]
    avg = jobs.aggregate(a=Avg("total"))["a"]
    return JsonResponse(
        {
            "ok": True,
            "kpis": [
                {"key": "one_off_total", "label": "Total one-off jobs", "value": total, "kind": "count", "view": "oneoff"},
                {
                    "key": "one_off_revenue",
                    "label": "One-off revenue",
                    "value": _money(revenue),
                    "kind": "currency",
                    "view": "oneoff",
                },
                {
                    "key": "one_off_avg",
                    "label": "Average price",
                    "value": _money(avg),
                    "kind": "currency",
                    "view": "oneoff",
                },
            ],
            "by_service_type": [
                {
                    "label": row["service_type"] or UNCATEGORIZED,
                    "key": row["service_type"] or UNCATEGORIZED,
                    "count": row["count"],
                    "value": row["count"],
                    "revenue": _money(row["revenue"]),
                    "avg_price": _money(row["avg_price"]),
                }
                for row in by_type
            ],
            "charts": {
                "by_type": [
                    {
                        "label": row["service_type"] or UNCATEGORIZED,
                        "key": row["service_type"] or UNCATEGORIZED,
                        "value": row["count"],
                        "count": row["count"],
                        "revenue": _money(row["revenue"]),
                    }
                    for row in by_type
                ],
                "revenue_by_type": [
                    {
                        "label": row["service_type"] or UNCATEGORIZED,
                        "key": row["service_type"] or UNCATEGORIZED,
                        "value": _money(row["revenue"]),
                        "count": row["count"],
                    }
                    for row in by_type
                ],
            },
        }
    )


@require_GET
def cancellations_dashboard(request):
    integration = _integration()
    if not integration:
        return JsonResponse({"ok": True, "kpis": [], "charts": {}, "recent": []})
    rows = CancellationRecord.objects.filter(integration=integration)
    start, end = _date_range(request)
    if start:
        rows = rows.filter(task_date__gte=start)
    if end:
        rows = rows.filter(task_date__lte=end)
    division = request.GET.get("division") or ""
    if division:
        rows = rows.filter(division=division)

    visits = rows.filter(cancellation_type=CANCELLATION_VISIT)
    jobs = rows.filter(cancellation_type=CANCELLATION_JOB)
    visit_agg = visits.aggregate(n=Count("id"), total=Sum("value"), avg=Avg("value"))
    job_agg = jobs.aggregate(n=Count("id"), total=Sum("value"), lost=Count("id", filter=Q(is_lost_client=True)))

    def by_division(qs):
        return [
            {
                "label": DIVISION_LABELS.get(row["division"], row["division"] or "—"),
                "key": row["division"] or "",
                "value": row["count"],
                "count": row["count"],
                "revenue": _money(row["total"]),
            }
            for row in qs.values("division").annotate(count=Count("id"), total=Sum("value")).order_by("-count")
        ]

    def by_month(qs):
        buckets = defaultdict(lambda: {"count": 0, "value": Decimal("0")})
        for row in qs.exclude(task_date__isnull=True).values("task_date", "value"):
            month = row["task_date"].strftime("%Y-%m")
            buckets[month]["count"] += 1
            if row["value"] is not None:
                buckets[month]["value"] += row["value"]
        return [
            {"month": m, "value": _money(v["value"]), "count": v["count"]}
            for m, v in sorted(buckets.items())
        ]

    recent = [
        {
            "id": r.id,
            "type": r.cancellation_type,
            "client": r.client_name,
            "value": _money(r.value),
            "division": r.division,
            "task_date": r.task_date,
            "lost_client": r.is_lost_client,
            "task_id": r.jobber_task_external_id,
        }
        for r in rows.select_related("client")[:25]
    ]

    return JsonResponse(
        {
            "ok": True,
            "kpis": [
                {
                    "key": "cancelled_visits_count",
                    "label": "Cancelled visits",
                    "value": visit_agg["n"] or 0,
                    "kind": "count",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_VISIT},
                },
                {
                    "key": "cancelled_visits_value",
                    "label": "Cancelled visit value",
                    "value": _money(visit_agg["total"]),
                    "kind": "currency",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_VISIT},
                },
                {
                    "key": "cancelled_visits_avg",
                    "label": "Avg cancelled visit",
                    "value": _money(visit_agg["avg"]),
                    "kind": "currency",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_VISIT},
                },
                {
                    "key": "cancelled_jobs_count",
                    "label": "Cancelled jobs",
                    "value": job_agg["n"] or 0,
                    "kind": "count",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_JOB},
                },
                {
                    "key": "lost_clients",
                    "label": "Lost clients",
                    "value": job_agg["lost"] or 0,
                    "kind": "count",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_JOB},
                },
                {
                    "key": "lost_mrr",
                    "label": "Lost monthly revenue",
                    "value": _money(job_agg["total"]),
                    "kind": "currency",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_JOB},
                },
            ],
            "charts": {
                "visits_by_division": by_division(visits),
                "jobs_by_division": by_division(jobs),
                "visits_by_month": by_month(visits),
                "jobs_by_month": by_month(jobs),
            },
            "recent": recent,
        }
    )


@require_GET
def cancellations_list(request):
    integration = _integration()
    qs = CancellationRecord.objects.all()
    if integration:
        qs = qs.filter(integration=integration)
    start, end = _date_range(request)
    if start:
        qs = qs.filter(task_date__gte=start)
    if end:
        qs = qs.filter(task_date__lte=end)
    ctype = request.GET.get("type") or ""
    if ctype:
        qs = qs.filter(cancellation_type=ctype)
    division = request.GET.get("division") or ""
    if division:
        qs = qs.filter(division=division)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(client_name__icontains=q) | Q(jobber_task_external_id__icontains=q))
    page = max(1, int(request.GET.get("page") or 1))
    page_size = 25
    total = qs.count()
    rows = qs[page_size * (page - 1) : page_size * page]
    return JsonResponse(
        {
            "ok": True,
            "count": total,
            "page": page,
            "pages": max(1, (total + page_size - 1) // page_size),
            "results": [
                {
                    "id": r.id,
                    "type": r.cancellation_type,
                    "client": r.client_name,
                    "value": _money(r.value),
                    "division": r.division,
                    "task_date": r.task_date,
                    "lost_client": r.is_lost_client,
                    "task_id": r.jobber_task_external_id,
                    "job_id": r.job_id,
                }
                for r in rows
            ],
        }
    )


@require_http_methods(["GET", "POST"])
def process_cancellations(request):
    integration = _integration()
    if not integration:
        return JsonResponse({"ok": False, "error": "No active Jobber integration"}, status=400)
    stats = process_all_cancellation_tasks(integration)
    return JsonResponse({"ok": True, **stats})


@require_GET
def cx_dashboard(request):
    feedback = CustomerFeedback.objects.all()
    reviews = GoogleReview.objects.all()
    start, end = _date_range(request)
    if start:
        feedback = feedback.filter(received_at__gte=start)
        reviews = reviews.filter(reviewed_at__gte=start)
    if end:
        feedback = feedback.filter(received_at__lte=end)
        reviews = reviews.filter(reviewed_at__lte=end)
    division = request.GET.get("division") or ""
    if division:
        feedback = feedback.filter(division=division)

    fb_count = feedback.count()
    responded = feedback.filter(responded=True).count()
    avg_rating = feedback.aggregate(a=Avg("rating"))["a"]
    google_count = reviews.count()
    google_avg = reviews.aggregate(a=Avg("rating"))["a"]
    return JsonResponse(
        {
            "ok": True,
            "kpis": [
                {"key": "ratings_avg", "label": "Customer ratings", "value": round(float(avg_rating or 0), 2), "kind": "count"},
                {"key": "feedback_received", "label": "Feedback received", "value": fb_count, "kind": "count"},
                {
                    "key": "feedback_response_rate",
                    "label": "Feedback response rate",
                    "value": round((responded / fb_count) * 100, 1) if fb_count else 0,
                    "kind": "percent",
                },
                {"key": "google_reviews", "label": "Google reviews", "value": google_count, "kind": "count"},
                {
                    "key": "google_avg",
                    "label": "Google rating",
                    "value": round(float(google_avg or 0), 2),
                    "kind": "count",
                },
            ],
            "recent_feedback": [
                {
                    "id": f.id,
                    "client": f.client_name,
                    "rating": float(f.rating) if f.rating is not None else None,
                    "text": f.feedback_text[:200],
                    "responded": f.responded,
                    "received_at": f.received_at,
                }
                for f in feedback[:20]
            ],
            "recent_reviews": [
                {
                    "id": r.id,
                    "author": r.author,
                    "rating": float(r.rating) if r.rating is not None else None,
                    "text": r.review_text[:200],
                    "reviewed_at": r.reviewed_at,
                    "replied": r.replied,
                }
                for r in reviews[:20]
            ],
        }
    )


@require_http_methods(["GET", "POST"])
def service_type_mappings(request):
    seed_taxonomy_defaults()
    if request.method == "POST":
        import json

        body = json.loads(request.body.decode() or "{}")
        obj_id = body.get("id")
        if body.get("delete") and obj_id:
            ServiceTypeMapping.objects.filter(id=obj_id).delete()
            clear_taxonomy_cache()
            return JsonResponse({"ok": True})
        defaults = {
            "keyword": (body.get("keyword") or "").strip(),
            "service_type": (body.get("service_type") or "").strip(),
            "priority": int(body.get("priority") or 100),
            "active": bool(body.get("active", True)),
        }
        if not defaults["keyword"] or not defaults["service_type"]:
            return JsonResponse({"ok": False, "error": "keyword and service_type required"}, status=400)
        if obj_id:
            ServiceTypeMapping.objects.filter(id=obj_id).update(**defaults)
        else:
            ServiceTypeMapping.objects.create(**defaults)
        clear_taxonomy_cache()
        return JsonResponse({"ok": True})
    rows = list(
        ServiceTypeMapping.objects.all().values("id", "keyword", "service_type", "priority", "active")
    )
    return JsonResponse({"ok": True, "results": rows})


@require_http_methods(["GET", "POST"])
def division_rules(request):
    seed_taxonomy_defaults()
    if request.method == "POST":
        import json

        body = json.loads(request.body.decode() or "{}")
        obj_id = body.get("id")
        if body.get("delete") and obj_id:
            DivisionRule.objects.filter(id=obj_id).delete()
            clear_taxonomy_cache()
            return JsonResponse({"ok": True})
        defaults = {
            "keyword": (body.get("keyword") or "").strip(),
            "division": (body.get("division") or "").strip(),
            "priority": int(body.get("priority") or 100),
            "active": bool(body.get("active", True)),
        }
        if not defaults["keyword"] or not defaults["division"]:
            return JsonResponse({"ok": False, "error": "keyword and division required"}, status=400)
        if obj_id:
            DivisionRule.objects.filter(id=obj_id).update(**defaults)
        else:
            DivisionRule.objects.create(**defaults)
        clear_taxonomy_cache()
        return JsonResponse({"ok": True})
    rows = list(DivisionRule.objects.all().values("id", "keyword", "division", "priority", "active"))
    return JsonResponse({"ok": True, "results": rows, "divisions": [{"key": k, "label": v} for k, v in DIVISION_CHOICES]})
