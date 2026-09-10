"""MRR bookings API — list/CRUD, KPIs, pull from lock-ins."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import F, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from accounts.authz import protect
from analytics.hub_models import HubPendingLockIn
from analytics.mrr_models import (
    MrrBooking,
    avg_hourly_rate,
    compute_monthly_value,
    hours_per_month,
    normalize_frequency,
)


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _money(value):
    try:
        return float(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def _parse_date(value):
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    text = str(value).strip()
    parsed = parse_date(text)
    if parsed:
        return parsed
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in {"active"}:
        return MrrBooking.STATUS_ACTIVE
    if text in {"cancelled", "canceled"}:
        return MrrBooking.STATUS_CANCELLED
    if text in {"inactive", "unactive", "paused"}:
        return MrrBooking.STATUS_INACTIVE
    return MrrBooking.STATUS_ACTIVE


def serialize_booking(item: MrrBooking) -> dict:
    hourly = avg_hourly_rate(item.visit_value, item.hours_per_visit)
    monthly_hours = hours_per_month(item.hours_per_visit, item.visits_per_month)
    return {
        "id": item.id,
        "name": item.name,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "contract": item.contract,
        "status": item.status,
        "frequency": item.frequency,
        "plan_type": item.plan_type,
        "visits_per_month": _money(item.visits_per_month),
        "visits_per_year": _money(item.visits_per_year),
        "hours_per_visit": _money(item.hours_per_visit) if item.hours_per_visit is not None else None,
        "hours_per_month": _money(monthly_hours) if monthly_hours is not None else None,
        "avg_hourly_rate": _money(hourly) if hourly is not None else None,
        "visit_value": _money(item.visit_value),
        "monthly_value": _money(item.monthly_value),
        "yearly_value": _money(item.yearly_value),
        "months_lifespan": _money(item.months_lifespan),
        "years_lifespan": _money(item.years_lifespan),
        "clv": _money(item.clv),
        "method": item.method,
        "cancellation_reason": item.cancellation_reason,
        "client_comments": item.client_comments,
        "sales_person": item.sales_person,
        "notes": item.notes,
        "source": item.source,
        "lockin_external_id": item.lockin_external_id,
        "manual_override": item.manual_override,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _apply_booking_payload(booking: MrrBooking, body: dict, *, mark_manual=False):
    if "name" in body:
        booking.name = (body.get("name") or "").strip()
    if "start_date" in body:
        booking.start_date = _parse_date(body.get("start_date"))
    if "end_date" in body:
        booking.end_date = _parse_date(body.get("end_date"))
    if "contract" in body:
        booking.contract = (body.get("contract") or "").strip() or "Residential"
    if "status" in body:
        booking.status = _normalize_status(body.get("status") or "")
    if "frequency" in body:
        booking.frequency = normalize_frequency(body.get("frequency") or "")
    if "plan_type" in body:
        booking.plan_type = (body.get("plan_type") or "").strip()
    if "hours_per_visit" in body and body.get("hours_per_visit") not in (None, ""):
        booking.hours_per_visit = Decimal(str(body.get("hours_per_visit")))
    if "visit_value" in body and body.get("visit_value") not in (None, ""):
        booking.visit_value = Decimal(str(body.get("visit_value")))
    if "method" in body:
        booking.method = (body.get("method") or "").strip()
    if "cancellation_reason" in body:
        booking.cancellation_reason = (body.get("cancellation_reason") or "").strip()
    if "client_comments" in body:
        booking.client_comments = (body.get("client_comments") or "").strip()
    if "sales_person" in body:
        booking.sales_person = (body.get("sales_person") or "").strip()
    if "notes" in body:
        booking.notes = (body.get("notes") or "").strip()

    if booking.status != MrrBooking.STATUS_ACTIVE and not booking.end_date:
        booking.end_date = timezone.localdate()
    if booking.status == MrrBooking.STATUS_ACTIVE:
        booking.end_date = booking.end_date  # keep if set
        # Active rows don't force end_date

    if mark_manual:
        booking.manual_override = True
        if booking.source == MrrBooking.SOURCE_LOCKIN:
            pass  # keep source=lockin but override flag protects fields
        elif not booking.source:
            booking.source = MrrBooking.SOURCE_MANUAL

    booking.recompute()
    return booking


def _kpi_payload(qs, request):
    start = _parse_date(request.GET.get("from"))
    end = _parse_date(request.GET.get("to"))

    active_qs = qs.filter(status=MrrBooking.STATUS_ACTIVE)
    active_mrr = active_qs.aggregate(total=Sum("monthly_value"))["total"] or Decimal("0")

    new_qs = qs.filter(status=MrrBooking.STATUS_ACTIVE)
    if start:
        new_qs = new_qs.filter(start_date__gte=start)
    if end:
        new_qs = new_qs.filter(start_date__lte=end)
    if not start and not end:
        # Default: current calendar month
        today = timezone.localdate()
        month_start = today.replace(day=1)
        new_qs = qs.filter(status=MrrBooking.STATUS_ACTIVE, start_date__gte=month_start, start_date__lte=today)
    new_mrr = new_qs.aggregate(total=Sum("monthly_value"))["total"] or Decimal("0")

    lost_qs = qs.exclude(status=MrrBooking.STATUS_ACTIVE)
    if start:
        lost_qs = lost_qs.filter(end_date__gte=start)
    if end:
        lost_qs = lost_qs.filter(end_date__lte=end)
    if not start and not end:
        today = timezone.localdate()
        month_start = today.replace(day=1)
        lost_qs = qs.exclude(status=MrrBooking.STATUS_ACTIVE).filter(
            end_date__gte=month_start, end_date__lte=today
        )
    lost_mrr = lost_qs.aggregate(total=Sum("monthly_value"))["total"] or Decimal("0")

    return {
        "active_mrr": _money(active_mrr),
        "new_mrr": _money(new_mrr),
        "lost_mrr": _money(lost_mrr),
        "net_mrr": _money(Decimal(str(new_mrr)) - Decimal(str(lost_mrr))),
        "active_count": active_qs.count(),
        "new_count": new_qs.count(),
        "lost_count": lost_qs.count(),
    }


@require_GET
def mrr_list(request):
    qs = MrrBooking.objects.order_by(
        F("start_date").desc(nulls_last=True),
        F("monthly_value").desc(),
        "-id",
    )
    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=_normalize_status(status))
    frequency = (request.GET.get("frequency") or "").strip()
    if frequency:
        qs = qs.filter(frequency=normalize_frequency(frequency))
    contract = (request.GET.get("contract") or "").strip()
    if contract:
        qs = qs.filter(contract__iexact=contract)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(sales_person__icontains=q)
            | Q(notes__icontains=q)
            | Q(cancellation_reason__icontains=q)
        )
    start = _parse_date(request.GET.get("from"))
    end = _parse_date(request.GET.get("to"))
    if start:
        qs = qs.filter(Q(start_date__gte=start) | Q(end_date__gte=start) | Q(start_date__isnull=True))
    if end:
        qs = qs.filter(Q(start_date__lte=end) | Q(start_date__isnull=True))

    per_page = int(request.GET.get("page_size") or 50)
    paginator = Paginator(qs, min(max(per_page, 1), 200))
    page = paginator.get_page(request.GET.get("page") or 1)
    return JsonResponse(
        {
            "ok": True,
            "count": paginator.count,
            "page": page.number,
            "pages": paginator.num_pages,
            "results": [serialize_booking(item) for item in page.object_list],
            "kpis": _kpi_payload(MrrBooking.objects.all(), request),
            "filters": {
                "statuses": [MrrBooking.STATUS_ACTIVE, MrrBooking.STATUS_INACTIVE, MrrBooking.STATUS_CANCELLED],
                "frequencies": ["Weekly", "Bi-Weekly", "Monthly"],
                "plan_types": ["Basic", "Detailed", "Custom"],
                "contracts": sorted(
                    {c for c in MrrBooking.objects.exclude(contract="").values_list("contract", flat=True).distinct()}
                    or ["Residential", "Commercial"]
                ),
            },
        },
        encoder=DjangoJSONEncoder,
    )


@csrf_exempt
@require_http_methods(["POST"])
def mrr_create(request):
    body = _json_body(request)
    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
    booking = MrrBooking(name=name, source=MrrBooking.SOURCE_MANUAL, manual_override=True)
    if getattr(request, "user", None) and request.user.is_authenticated:
        booking.created_by = request.user
    _apply_booking_payload(booking, body, mark_manual=True)
    booking.save()
    return JsonResponse({"ok": True, "result": serialize_booking(booking)}, encoder=DjangoJSONEncoder, status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "PUT", "DELETE"])
def mrr_detail(request, pk: int):
    booking = get_object_or_404(MrrBooking, pk=pk)
    if request.method == "GET":
        return JsonResponse({"ok": True, "result": serialize_booking(booking)}, encoder=DjangoJSONEncoder)
    if request.method == "DELETE":
        booking.delete()
        return JsonResponse({"ok": True})
    body = _json_body(request)
    _apply_booking_payload(booking, body, mark_manual=True)
    booking.save()
    return JsonResponse({"ok": True, "result": serialize_booking(booking)}, encoder=DjangoJSONEncoder)


@csrf_exempt
@require_http_methods(["POST"])
def mrr_import_lockins(request):
    """
    Create/update MRR rows from locked-in Hub pending quotes.
    Manual-override rows are never overwritten.
    Visit value is left as-is (or 0 for new rows) — user fills price manually.
    """
    body = _json_body(request)
    only_locked = body.get("only_locked", True)
    qs = HubPendingLockIn.objects.all()
    if only_locked:
        qs = qs.filter(locked_in=True)

    created = 0
    updated = 0
    skipped = 0
    for lockin in qs:
        existing = None
        if lockin.external_id:
            existing = MrrBooking.objects.filter(lockin_external_id=lockin.external_id).first()
        if existing is None and lockin.client_name:
            existing = (
                MrrBooking.objects.filter(name__iexact=lockin.client_name, source=MrrBooking.SOURCE_LOCKIN)
                .order_by("-id")
                .first()
            )

        if existing and existing.manual_override:
            skipped += 1
            continue

        freq = normalize_frequency(lockin.frequency or "Monthly")
        start = None
        if lockin.first_recurring_visit_at:
            start = lockin.first_recurring_visit_at.date()
        elif lockin.locked_at:
            start = lockin.locked_at.date()
        elif lockin.quote_approved_at:
            start = lockin.quote_approved_at.date()

        if existing is None:
            booking = MrrBooking(
                name=lockin.client_name or lockin.quote_id or f"Lock-in {lockin.external_id}",
                start_date=start,
                frequency=freq,
                status=MrrBooking.STATUS_ACTIVE,
                source=MrrBooking.SOURCE_LOCKIN,
                lockin=lockin,
                lockin_external_id=lockin.external_id or "",
                visit_value=Decimal("0"),
                method="Lock-in",
                notes=f"Imported from lock-in {lockin.quote_id or lockin.external_id}",
                manual_override=False,
            )
            if getattr(request, "user", None) and request.user.is_authenticated:
                booking.created_by = request.user
            booking.recompute()
            booking.save()
            created += 1
        else:
            existing.name = lockin.client_name or existing.name
            existing.frequency = freq or existing.frequency
            if start and not existing.start_date:
                existing.start_date = start
            existing.lockin = lockin
            existing.lockin_external_id = lockin.external_id or existing.lockin_external_id
            existing.source = MrrBooking.SOURCE_LOCKIN
            existing.recompute()
            existing.save()
            updated += 1

    return JsonResponse({"ok": True, "created": created, "updated": updated, "skipped": skipped})


urlpatterns = [
    path("mrr/", protect(mrr_list)),
    path("mrr/create/", protect(mrr_create)),
    path("mrr/import-lockins/", protect(mrr_import_lockins)),
    path("mrr/<int:pk>/", protect(mrr_detail)),
]
