from decimal import Decimal

from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from accounts.authz import protect
from analytics.pricing_models import (
    PricingAddon,
    PricingBundle,
    PricingCoupon,
    PricingLocation,
    PricingPackage,
    PricingService,
    PricingSubmission,
    PricingSyncRun,
)
from analytics.pricing_sync import (
    latest_pricing_sync,
    latest_successful_pricing_sync,
    run_pricing_sync,
)


def _num(value):
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    return value


def _money(value):
    return round(_num(value), 2)


def _serialize_sync(run):
    if not run:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "source_generated_at": run.source_generated_at,
        "counts": run.counts or {},
        "error": run.error or "",
    }


def _page(queryset, request, serializer, per_page=25):
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get("page") or 1
    page = paginator.get_page(page_number)
    return JsonResponse(
        {
            "ok": True,
            "count": paginator.count,
            "page": page.number,
            "pages": paginator.num_pages,
            "results": [serializer(item) for item in page.object_list],
        },
        encoder=DjangoJSONEncoder,
    )


def _apply_search(qs, request, fields):
    q = (request.GET.get("q") or "").strip()
    if not q:
        return qs
    query = Q()
    for field in fields:
        query |= Q(**{f"{field}__icontains": q})
    return qs.filter(query)


def _apply_exact(qs, request, mapping):
    for param, field in mapping.items():
        value = request.GET.get(param)
        if value not in (None, ""):
            qs = qs.filter(**{field: value})
    return qs


def _apply_date_range(qs, request, field):
    start = request.GET.get("from")
    end = request.GET.get("to")
    if start:
        qs = qs.filter(**{f"{field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lt": end})
    return qs


def _apply_bool(qs, request, param, field):
    value = request.GET.get(param)
    if value == "true":
        return qs.filter(**{field: True})
    if value == "false":
        return qs.filter(**{field: False})
    return qs


@require_GET
def status(request):
    latest = latest_pricing_sync()
    success = latest_successful_pricing_sync()
    return JsonResponse(
        {
            "ok": True,
            "configured": True,
            "sync": _serialize_sync(latest),
            "last_success": _serialize_sync(success),
            "has_data": PricingSubmission.objects.exists() or PricingService.objects.exists(),
        },
        encoder=DjangoJSONEncoder,
    )


@csrf_exempt
@require_http_methods(["POST"])
def sync(request):
    running = PricingSyncRun.objects.filter(
        status__in=[PricingSyncRun.STATUS_QUEUED, PricingSyncRun.STATUS_RUNNING]
    ).exists()
    if running:
        return JsonResponse({"ok": False, "error": "A sync is already running."}, status=409)

    run = run_pricing_sync()
    payload = {"ok": run.status == PricingSyncRun.STATUS_SUCCESS, "sync": _serialize_sync(run)}
    if run.status != PricingSyncRun.STATUS_SUCCESS:
        payload["error"] = run.error or "Sync failed"
        return JsonResponse(payload, status=502, encoder=DjangoJSONEncoder)
    return JsonResponse(payload, encoder=DjangoJSONEncoder)


def _serialize_submission(item, detail=False):
    data = {
        "id": item.id,
        "external_id": item.external_id,
        "status": item.status,
        "customer": item.customer_name,
        "email": item.customer_email,
        "phone": item.customer_phone,
        "company": item.customer_company,
        "property_type": item.property_type or "—",
        "location": item.location_name or "—",
        "sqft": _num(item.actual_sqft) if item.actual_sqft is not None else None,
        "final_total": _money(item.final_total),
        "base_price": _money(item.total_base_price),
        "addons": _money(item.total_addons_price),
        "discounts": _money((item.discounted_amount or 0) + (item.bundle_discount_amount or 0)),
        "coupon": item.coupon_code or None,
        "bundle": item.bundle_name or None,
        "lead_source": item.heard_about_us or "—",
        "services": item.service_names or [],
        "is_bid_in_person": item.is_bid_in_person,
        "is_on_the_go": item.is_on_the_go,
        "created_at": item.source_created_at,
    }
    if detail:
        data["payload"] = item.source_payload or {}
        data["street_address"] = item.street_address
        data["postal_code"] = item.postal_code
        data["size_range"] = item.size_range
        data["pricing"] = {
            "base": _money(item.total_base_price),
            "adjustments": _money(item.total_adjustments),
            "surcharges": _money(item.total_surcharges),
            "addons": _money(item.total_addons_price),
            "coupon_discount": _money(item.discounted_amount),
            "bundle_discount": _money(item.bundle_discount_amount),
            "final_total": _money(item.final_total),
        }
        data["quote_url"] = item.quote_url
        data["expires_at"] = item.expires_at
    return data


@require_GET
def dashboard(request):
    success = latest_successful_pricing_sync()
    submissions = PricingSubmission.objects.filter(is_deleted=False)
    stored = (success.summary if success else {}) or {}

    by_status = list(
        submissions.values("status").annotate(count=Count("id"), revenue=Sum("final_total")).order_by("-count")
    )
    by_property = list(
        submissions.values("property_type").annotate(count=Count("id"), revenue=Sum("final_total")).order_by("-count")
    )
    by_location = list(
        submissions.exclude(location_name="")
        .values("location_name")
        .annotate(count=Count("id"), revenue=Sum("final_total"))
        .order_by("-revenue")[:8]
    )
    monthly = list(
        submissions.exclude(source_created_at=None)
        .annotate(month=TruncMonth("source_created_at"))
        .values("month")
        .annotate(count=Count("id"), revenue=Sum("final_total"))
        .order_by("month")
    )
    by_source = list(
        submissions.values("heard_about_us")
        .annotate(count=Count("id"), revenue=Sum("final_total"))
        .order_by("-count")[:12]
    )

    aggregates = submissions.aggregate(
        revenue=Sum("final_total"),
        avg=Avg("final_total"),
        base=Sum("total_base_price"),
        addons=Sum("total_addons_price"),
        discounts=Sum("discounted_amount"),
    )

    kpis = [
        {
            "key": "submissions",
            "label": "Quotes",
            "value": submissions.count(),
            "kind": "count",
            "view": "submissions",
            "filters": {},
            "hint": "All stored submissions",
        },
        {
            "key": "revenue",
            "label": "Quote volume",
            "value": _money(aggregates["revenue"]),
            "kind": "currency",
            "view": "submissions",
            "filters": {},
            "hint": "Sum of final totals",
        },
        {
            "key": "avg_quote",
            "label": "Avg quote",
            "value": _money(aggregates["avg"]),
            "kind": "currency",
            "view": "submissions",
            "filters": {},
            "hint": "Average final total",
        },
        {
            "key": "pipeline",
            "label": "Pipeline",
            "value": _money(
                submissions.filter(status__in=["approved", "packages_selected", "submitted"]).aggregate(
                    total=Sum("final_total")
                )["total"]
            ),
            "kind": "currency",
            "view": "submissions",
            "filters": {},
            "hint": "Submitted / approved volume",
        },
        {
            "key": "approved",
            "label": "Quotes approved",
            "value": submissions.filter(status="approved").count(),
            "kind": "count",
            "view": "submissions",
            "filters": {"status": "approved"},
            "hint": "Approved quotes",
        },
        {
            "key": "approval_rate",
            "label": "Approval rate",
            "value": round(
                (submissions.filter(status="approved").count() / submissions.count()) * 100,
                1,
            )
            if submissions.count()
            else 0,
            "kind": "percent",
            "view": "submissions",
            "filters": {},
            "hint": "Approved / all quotes",
        },
        {
            "key": "services",
            "label": "Active services",
            "value": PricingService.objects.filter(is_active=True).count(),
            "kind": "count",
            "view": "services",
            "filters": {"active": "true"},
            "hint": "Catalog services",
        },
        {
            "key": "locations",
            "label": "Locations",
            "value": PricingLocation.objects.filter(is_active=True).count(),
            "kind": "count",
            "view": "locations",
            "filters": {"active": "true"},
            "hint": "Active service areas",
        },
        {
            "key": "coupons",
            "label": "Coupons used",
            "value": submissions.filter(coupon_applied=True).count(),
            "kind": "count",
            "view": "submissions",
            "filters": {"coupon": "true"},
            "hint": "Quotes with a coupon",
        },
        {
            "key": "addons_revenue",
            "label": "Add-ons revenue",
            "value": _money(aggregates["addons"]),
            "kind": "currency",
            "view": "submissions",
            "filters": {},
            "hint": "Add-on line total",
        },
    ]

    return JsonResponse(
        {
            "ok": True,
            "sync": _serialize_sync(success),
            "kpis": kpis,
            "charts": {
                "status": [
                    {
                        "label": (row["status"] or "—").replace("_", " ").title(),
                        "key": row["status"] or "",
                        "count": row["count"],
                        "value": row["count"],
                        "revenue": _money(row["revenue"]),
                    }
                    for row in by_status
                ],
                "property": [
                    {
                        "label": (row["property_type"] or "unknown").replace("_", " ").title(),
                        "key": row["property_type"] or "",
                        "count": row["count"],
                        "value": row["count"],
                        "revenue": _money(row["revenue"]),
                    }
                    for row in by_property
                ],
                "locations": [
                    {
                        "label": row["location_name"],
                        "key": row["location_name"],
                        "count": row["count"],
                        "value": _money(row["revenue"]),
                        "revenue": _money(row["revenue"]),
                    }
                    for row in by_location
                ],
                "monthly": [
                    {
                        "month": row["month"].date().isoformat()[:7] if row["month"] else "",
                        "value": _money(row["revenue"]),
                        "count": row["count"],
                    }
                    for row in monthly
                    if row["month"]
                ],
                "sales_by_source": [
                    {
                        "label": row["heard_about_us"] or "Unknown",
                        "key": row["heard_about_us"] or "",
                        "count": row["count"],
                        "value": _money(row["revenue"]),
                        "revenue": _money(row["revenue"]),
                    }
                    for row in by_source
                ],
            },
            "recent": [_serialize_submission(item) for item in submissions[:8]],
            "catalog_summary": {
                "services": PricingService.objects.count(),
                "packages": PricingPackage.objects.count(),
                "locations": PricingLocation.objects.count(),
                "addons": PricingAddon.objects.count(),
                "coupons": PricingCoupon.objects.count(),
                "bundles": PricingBundle.objects.count(),
            },
            "summary": stored,
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def submissions(request):
    qs = PricingSubmission.objects.filter(is_deleted=False)
    qs = _apply_exact(qs, request, {"status": "status", "property_type": "property_type", "location": "location_name"})
    qs = _apply_bool(qs, request, "coupon", "coupon_applied")
    qs = _apply_bool(qs, request, "bundle", "bundle_applied")
    qs = _apply_bool(qs, request, "bid_in_person", "is_bid_in_person")
    qs = _apply_date_range(qs, request, "source_created_at")
    qs = _apply_search(
        qs,
        request,
        [
            "customer_first_name",
            "customer_last_name",
            "customer_email",
            "customer_company",
            "location_name",
            "coupon_code",
            "status",
        ],
    )
    return _page(qs, request, _serialize_submission)


@require_GET
def submission_detail(request, pk):
    item = get_object_or_404(PricingSubmission, pk=pk)
    return JsonResponse({"ok": True, "item": _serialize_submission(item, detail=True)}, encoder=DjangoJSONEncoder)


@require_GET
def services(request):
    qs = PricingService.objects.all()
    qs = _apply_bool(qs, request, "active", "is_active")
    qs = _apply_search(qs, request, ["name", "description"])
    return _page(
        qs,
        request,
        lambda item: {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "active": item.is_active,
            "commercial": item.is_commercial,
            "residential": item.is_residential,
            "order": item.sort_order,
        },
    )


@require_GET
def packages(request):
    qs = PricingPackage.objects.all()
    qs = _apply_exact(qs, request, {"service": "service_name"})
    qs = _apply_bool(qs, request, "active", "is_active")
    qs = _apply_search(qs, request, ["name", "service_name"])
    return _page(
        qs,
        request,
        lambda item: {
            "id": item.id,
            "name": item.name,
            "service": item.service_name,
            "base_price": _money(item.base_price),
            "active": item.is_active,
        },
    )


@require_GET
def locations(request):
    qs = PricingLocation.objects.all()
    qs = _apply_bool(qs, request, "active", "is_active")
    qs = _apply_search(qs, request, ["name", "address"])
    return _page(
        qs,
        request,
        lambda item: {
            "id": item.id,
            "name": item.name,
            "address": item.address,
            "trip_surcharge": _money(item.trip_surcharge),
            "active": item.is_active,
        },
    )


@require_GET
def coupons(request):
    qs = PricingCoupon.objects.all()
    qs = _apply_bool(qs, request, "active", "is_active")
    qs = _apply_search(qs, request, ["code"])
    return _page(
        qs,
        request,
        lambda item: {
            "id": item.id,
            "code": item.code,
            "percent": _num(item.percentage_discount) if item.percentage_discount is not None else None,
            "fixed": _money(item.fixed_discount) if item.fixed_discount is not None else None,
            "used": item.used_count,
            "active": item.is_active,
            "expires_at": item.expiration_date,
        },
    )


@require_GET
def addons(request):
    qs = PricingAddon.objects.all()
    qs = _apply_search(qs, request, ["name", "description"])
    return _page(
        qs,
        request,
        lambda item: {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "base_price": _money(item.base_price),
            "global": item.is_global,
        },
    )


@require_GET
def filter_options(request):
    return JsonResponse(
        {
            "ok": True,
            "submissions": {
                "statuses": sorted(
                    PricingSubmission.objects.exclude(status="").values_list("status", flat=True).distinct()
                ),
                "property_types": sorted(
                    PricingSubmission.objects.exclude(property_type="")
                    .values_list("property_type", flat=True)
                    .distinct()
                ),
                "locations": sorted(
                    PricingSubmission.objects.exclude(location_name="")
                    .values_list("location_name", flat=True)
                    .distinct()
                ),
            },
            "packages": {
                "services": sorted(
                    PricingPackage.objects.exclude(service_name="").values_list("service_name", flat=True).distinct()
                ),
            },
        }
    )


urlpatterns = [
    path("status/", protect(status)),
    path("sync/", protect(sync)),
    path("dashboard/", protect(dashboard)),
    path("submissions/", protect(submissions)),
    path("submissions/<int:pk>/", protect(submission_detail)),
    path("services/", protect(services)),
    path("packages/", protect(packages)),
    path("locations/", protect(locations)),
    path("coupons/", protect(coupons)),
    path("addons/", protect(addons)),
    path("filters/", protect(filter_options)),
]
