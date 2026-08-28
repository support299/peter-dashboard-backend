from decimal import Decimal

from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from analytics.hub_models import (
    AdminInternalSyncRun,
    HubAlert,
    HubBonus,
    HubEmployee,
    HubLeaveRequest,
    HubPendingLockIn,
    HubVisit,
)
from analytics.sync import latest_successful_sync, latest_sync, run_sync


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
        qs = qs.filter(**{f"{field}__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__lt": end})
    return qs


@require_GET
def status(request):
    latest = latest_sync()
    success = latest_successful_sync()
    return JsonResponse(
        {
            "ok": True,
            "configured": True,
            "sync": _serialize_sync(latest),
            "last_success": _serialize_sync(success),
            "has_data": HubEmployee.objects.exists() or HubLeaveRequest.objects.exists(),
        },
        encoder=DjangoJSONEncoder,
    )


@csrf_exempt
@require_http_methods(["POST"])
def sync(request):
    running = AdminInternalSyncRun.objects.filter(
        status__in=[AdminInternalSyncRun.STATUS_QUEUED, AdminInternalSyncRun.STATUS_RUNNING]
    ).exists()
    if running:
        return JsonResponse({"ok": False, "error": "A sync is already running."}, status=409)

    run = run_sync()
    payload = {
        "ok": run.status == AdminInternalSyncRun.STATUS_SUCCESS,
        "sync": _serialize_sync(run),
    }
    if run.status != AdminInternalSyncRun.STATUS_SUCCESS:
        payload["error"] = run.error or "Sync failed"
        return JsonResponse(payload, status=502, encoder=DjangoJSONEncoder)
    return JsonResponse(payload, encoder=DjangoJSONEncoder)


@require_GET
def dashboard(request):
    success = latest_successful_sync()
    employees = HubEmployee.objects.all()
    leave = HubLeaveRequest.objects.all()
    bonuses = HubBonus.objects.all()
    lock_ins = HubPendingLockIn.objects.all()
    visits = HubVisit.objects.all()

    leave_by_status = list(
        leave.values("status").annotate(count=Count("id")).order_by("-count")
    )
    leave_by_type = list(
        leave.values("leave_type").annotate(count=Count("id")).order_by("-count")
    )
    bonus_by_status = list(
        bonuses.values("status").annotate(count=Count("id"), amount=Sum("amount")).order_by("-count")
    )
    employees_by_role = list(
        employees.values("role").annotate(count=Count("id")).order_by("-count")
    )
    employees_by_position = list(
        employees.exclude(position="")
        .values("position")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    stored = (success.summary if success else {}) or {}
    kpis_source = stored.get("custom_kpis") or {}

    kpis = [
        {
            "key": "headcount_active",
            "label": "Active team",
            "value": employees.filter(status="active").count(),
            "kind": "count",
            "view": "employees",
            "filters": {"status": "active"},
        },
        {
            "key": "leave_pending",
            "label": "Pending leave",
            "value": leave.filter(status="pending").count(),
            "kind": "count",
            "view": "leave",
            "filters": {"status": "pending"},
        },
        {
            "key": "leave_approval_rate",
            "label": "Leave approval rate",
            "value": round(float(kpis_source.get("leave_approval_rate") or 0) * 100, 1),
            "kind": "percent",
            "view": "leave",
            "filters": {},
        },
        {
            "key": "bonus_total",
            "label": "Bonus amount",
            "value": _money(bonuses.aggregate(total=Sum("amount"))["total"]),
            "kind": "currency",
            "view": "bonuses",
            "filters": {},
        },
        {
            "key": "bonus_paid",
            "label": "Bonuses paid",
            "value": _money(bonuses.filter(bonus_paid=True).aggregate(total=Sum("amount"))["total"]),
            "kind": "currency",
            "view": "bonuses",
            "filters": {"paid": "true"},
        },
        {
            "key": "pending_lock_ins",
            "label": "Pending lock-ins",
            "value": lock_ins.filter(locked_in=False).count(),
            "kind": "count",
            "view": "lockins",
            "filters": {"locked_in": "false"},
        },
        {
            "key": "visits_total",
            "label": "Hub visits",
            "value": visits.count(),
            "kind": "count",
            "view": "visits",
            "filters": {},
        },
        {
            "key": "vacation_pool",
            "label": "Vacation days left",
            "value": _num(employees.aggregate(total=Sum("available_vacation_days"))["total"]),
            "kind": "count",
            "view": "employees",
            "filters": {},
        },
        {
            "key": "absences",
            "label": "Absences",
            "value": employees.aggregate(total=Sum("absences_count"))["total"] or 0,
            "kind": "count",
            "view": "employees",
            "filters": {},
        },
        {
            "key": "late_arrivals",
            "label": "Late arrivals",
            "value": employees.aggregate(total=Sum("late_arrivals_count"))["total"] or 0,
            "kind": "count",
            "view": "employees",
            "filters": {},
        },
        {
            "key": "attendance_days",
            "label": "Attendance days",
            "value": _num(employees.aggregate(total=Sum("attendance_days"))["total"]),
            "kind": "count",
            "view": "employees",
            "filters": {},
        },
    ]

    return JsonResponse(
        {
            "ok": True,
            "sync": _serialize_sync(success),
            "kpis": kpis,
            "charts": {
                "leave_status": [
                    {"label": (row["status"] or "—").replace("_", " ").title(), "key": row["status"] or "", "count": row["count"]}
                    for row in leave_by_status
                ],
                "leave_type": [
                    {"label": row["leave_type"] or "—", "key": row["leave_type"] or "", "count": row["count"]}
                    for row in leave_by_type
                ],
                "bonus_status": [
                    {
                        "label": (row["status"] or "—").replace("_", " ").title(),
                        "key": row["status"] or "",
                        "count": row["count"],
                        "amount": _money(row["amount"]),
                    }
                    for row in bonus_by_status
                ],
                "roles": [
                    {"label": (row["role"] or "—").title(), "key": row["role"] or "", "count": row["count"]}
                    for row in employees_by_role
                ],
                "positions": [
                    {"label": row["position"] or "—", "key": row["position"] or "", "count": row["count"]}
                    for row in employees_by_position
                ],
            },
            "alerts": [
                {"id": a.id, "message": a.message, "active": a.active}
                for a in HubAlert.objects.filter(active=True)[:10]
            ],
            "recent_leave": [_serialize_leave(item) for item in leave[:8]],
            "summary": stored,
        },
        encoder=DjangoJSONEncoder,
    )


def _serialize_employee(item):
    return {
        "id": item.id,
        "external_id": item.external_id,
        "name": item.name,
        "email": item.email,
        "phone": item.phone,
        "role": item.role,
        "status": item.status,
        "position": item.position or "Unspecified",
        "sectors": item.sectors or [],
        "hire_date": item.hire_date,
        "available_vacation_days": _num(item.available_vacation_days),
        "visits": item.visits_count,
        "leave_requests": item.leave_requests_count,
        "absences": item.absences_count,
        "vacations": item.vacations_count,
        "late_arrivals": item.late_arrivals_count,
        "attendance_days": _num(item.attendance_days) if item.attendance_days is not None else None,
        "performance_score": _num(item.performance_score) if item.performance_score is not None else None,
        "feedback_count": item.feedback_count,
        "bonus_amount": _money(item.lock_in_bonus_amount_total),
        "rates": item.rates or {},
    }


def _serialize_leave(item):
    return {
        "id": item.id,
        "external_id": item.external_id,
        "employee": item.employee_name,
        "employee_id": item.employee_id,
        "status": item.status,
        "leave_type": item.leave_type,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "weekday_count": item.weekday_count,
        "vacation_days_deducted": _num(item.vacation_days_deducted) if item.vacation_days_deducted is not None else None,
        "created_at": item.source_created_at,
    }


def _serialize_bonus(item):
    return {
        "id": item.id,
        "external_id": item.external_id,
        "employee": item.employee_name,
        "employee_id": item.employee_id,
        "status": item.status,
        "bonus_type": item.bonus_type,
        "amount": _money(item.amount),
        "position": item.position_snapshot or item.employee_position,
        "client": item.client_name,
        "paid": item.bonus_paid,
        "confirmed": item.bonus_confirmed,
        "paid_date": item.paid_date,
        "created_at": item.source_created_at,
    }


def _serialize_lockin(item):
    return {
        "id": item.id,
        "external_id": item.external_id,
        "quote_id": item.quote_id,
        "client": item.client_name,
        "status": item.status,
        "locked_in": item.locked_in,
        "frequency": item.frequency,
        "team": item.technician_names or [],
        "quote_approved_at": item.quote_approved_at,
        "eligibility_expires_at": item.eligibility_expires_at,
        "expected_first_visit_at": item.expected_first_visit_at,
    }


def _serialize_visit(item):
    return {
        "id": item.id,
        "external_id": item.external_id,
        "title": item.title,
        "client": item.client_name,
        "job_type": item.job_type,
        "start_at": item.start_at,
        "team": item.technician_names or [],
    }


@require_GET
def employees(request):
    qs = HubEmployee.objects.all()
    qs = _apply_exact(qs, request, {"status": "status", "role": "role", "position": "position"})
    qs = _apply_search(qs, request, ["name", "email", "phone", "position"])
    return _page(qs, request, _serialize_employee)


@require_GET
def leave(request):
    qs = HubLeaveRequest.objects.select_related("employee").all()
    qs = _apply_exact(qs, request, {"status": "status", "leave_type": "leave_type", "employee_id": "employee_id"})
    qs = _apply_date_range(qs, request, "start_date")
    qs = _apply_search(qs, request, ["employee_name", "leave_type", "status"])
    return _page(qs, request, _serialize_leave)


@require_GET
def bonuses(request):
    qs = HubBonus.objects.select_related("employee", "pending").all()
    qs = _apply_exact(qs, request, {"status": "status", "bonus_type": "bonus_type", "employee_id": "employee_id"})
    paid = request.GET.get("paid")
    if paid == "true":
        qs = qs.filter(bonus_paid=True)
    elif paid == "false":
        qs = qs.filter(bonus_paid=False)
    qs = _apply_date_range(qs, request, "source_created_at")
    qs = _apply_search(qs, request, ["employee_name", "client_name", "bonus_type", "status"])
    return _page(qs, request, _serialize_bonus)


@require_GET
def lockins(request):
    qs = HubPendingLockIn.objects.all()
    qs = _apply_exact(qs, request, {"status": "status"})
    locked = request.GET.get("locked_in")
    if locked == "true":
        qs = qs.filter(locked_in=True)
    elif locked == "false":
        qs = qs.filter(locked_in=False)
    qs = _apply_search(qs, request, ["client_name", "quote_id", "status", "frequency"])
    return _page(qs, request, _serialize_lockin)


@require_GET
def visits(request):
    qs = HubVisit.objects.all()
    qs = _apply_exact(qs, request, {"job_type": "job_type"})
    qs = _apply_date_range(qs, request, "start_at")
    qs = _apply_search(qs, request, ["title", "client_name", "job_type"])
    tech = (request.GET.get("technician") or "").strip()
    if tech:
        qs = qs.filter(technician_names__icontains=tech)
    return _page(qs, request, _serialize_visit)


@require_GET
def filter_options(request):
    return JsonResponse(
        {
            "ok": True,
            "employees": {
                "roles": sorted(HubEmployee.objects.exclude(role="").values_list("role", flat=True).distinct()),
                "statuses": sorted(HubEmployee.objects.exclude(status="").values_list("status", flat=True).distinct()),
                "positions": sorted(HubEmployee.objects.exclude(position="").values_list("position", flat=True).distinct()),
            },
            "leave": {
                "statuses": sorted(HubLeaveRequest.objects.exclude(status="").values_list("status", flat=True).distinct()),
                "types": sorted(HubLeaveRequest.objects.exclude(leave_type="").values_list("leave_type", flat=True).distinct()),
            },
            "bonuses": {
                "statuses": sorted(HubBonus.objects.exclude(status="").values_list("status", flat=True).distinct()),
                "types": sorted(HubBonus.objects.exclude(bonus_type="").values_list("bonus_type", flat=True).distinct()),
            },
            "lockins": {
                "statuses": sorted(HubPendingLockIn.objects.exclude(status="").values_list("status", flat=True).distinct()),
            },
            "visits": {
                "job_types": sorted(HubVisit.objects.exclude(job_type="").values_list("job_type", flat=True).distinct()),
            },
        }
    )


urlpatterns = [
    path("status/", status),
    path("sync/", sync),
    path("dashboard/", dashboard),
    path("employees/", employees),
    path("leave/", leave),
    path("bonuses/", bonuses),
    path("lockins/", lockins),
    path("visits/", visits),
    path("filters/", filter_options),
]
