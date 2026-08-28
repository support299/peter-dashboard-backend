import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from analytics.hub_models import (
    AdminInternalSyncRun,
    HubAlert,
    HubBonus,
    HubEmployee,
    HubLeaveRequest,
    HubPendingLockIn,
    HubVisit,
)
from analytics.parsing import as_dict, as_list, parse_d, parse_decimal, parse_dt

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


class AdminInternalAPIError(Exception):
    pass


def _config():
    return settings.ADMIN_INTERNAL_APP


def fetch_analytics_payload():
    cfg = _config()
    base = (cfg.get("BASE_URL") or "").rstrip("/")
    path = cfg.get("ANALYTICS_PATH") or "/api/admin-internal-app/analytics/"
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base}{path}"
    api_key = cfg.get("API_KEY") or ""
    if not base:
        raise AdminInternalAPIError("ADMIN_INTERNAL_APP_BASE_URL is not configured.")
    if not api_key:
        raise AdminInternalAPIError("ADMIN_INTERNAL_APP_API_KEY is not configured.")

    timeout = int(cfg.get("TIMEOUT") or 60)
    response = requests.get(url, headers={"X-API-Key": api_key}, timeout=timeout)
    if response.status_code >= 400:
        raise AdminInternalAPIError(f"Upstream returned {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise AdminInternalAPIError("Upstream response was not valid JSON.") from exc


def _upsert_employees(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        HubEmployee.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": row.get("name") or "",
                "email": row.get("email") or "",
                "phone": row.get("phone") or "",
                "role": row.get("role") or "",
                "status": row.get("status") or "",
                "position": row.get("position") or "",
                "sectors": as_list(row.get("sectors")),
                "work_days": parse_decimal(row.get("work_days")),
                "hire_date": parse_d(row.get("hire_date")),
                "available_vacation_days": parse_decimal(row.get("available_vacation_days")),
                "vacation_balance_reset_on": parse_d(row.get("vacation_balance_reset_on")),
                "jobber_id": row.get("jobber_id") or "",
                "ghl_id": row.get("ghl_id") or "",
                "rates": as_dict(row.get("rates")),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_updated_at": parse_dt(row.get("updated_at")),
                "source_payload": row,
            },
        )
    return seen


def _apply_performance(rows):
    for row in rows:
        external_id = str(row.get("employee_id") or "").strip()
        if not external_id:
            continue
        updates = {
            "lock_in_bonus_count": int(row.get("lock_in_bonus_count") or 0),
            "lock_in_bonus_amount_total": parse_decimal(row.get("lock_in_bonus_amount_total"), ZERO),
            "lock_in_bonus_amount_paid": parse_decimal(row.get("lock_in_bonus_amount_paid"), ZERO),
            "visits_count": int(row.get("visits") or 0),
            "leave_requests_count": int(row.get("leave_requests") or 0),
            "absences_count": int(row.get("absences") or 0),
            "vacations_count": int(row.get("vacations") or 0),
            "late_arrivals_count": int(row.get("late_arrivals") or row.get("lates") or row.get("late") or 0),
            "attendance_days": parse_decimal(row.get("attendance_days") or row.get("work_days") or row.get("attendance")),
            "performance_score": parse_decimal(row.get("performance_score") or row.get("rating") or row.get("score")),
            "feedback_count": int(row.get("feedback_count") or row.get("feedback") or 0),
        }
        if row.get("position"):
            updates["position"] = row["position"]
        if row.get("employee_name"):
            updates["name"] = row["employee_name"]
        HubEmployee.objects.filter(external_id=external_id).update(**updates)


def _upsert_leave(rows):
    employees = {e.external_id: e for e in HubEmployee.objects.all()}
    seen = set()
    for row in rows:
        external_id = str(row.get("submission_id") or row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        emp_id = str(row.get("employee_id") or "").strip()
        HubLeaveRequest.objects.update_or_create(
            external_id=external_id,
            defaults={
                "employee": employees.get(emp_id),
                "employee_external_id": emp_id,
                "employee_name": row.get("employee_name") or "",
                "status": row.get("status") or "",
                "leave_type": row.get("leave_type") or "",
                "leave_type_raw": row.get("leave_type_raw") or "",
                "start_date": parse_d(row.get("start_date")),
                "end_date": parse_d(row.get("end_date")),
                "weekday_count": row.get("weekday_count"),
                "vacation_days_deducted": parse_decimal(row.get("vacation_days_deducted")),
                "jobber_task_id": row.get("jobber_task_id") or "",
                "jobber_sync_error": row.get("jobber_sync_error") or "",
                "decided_at": parse_dt(row.get("decided_at")),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_updated_at": parse_dt(row.get("updated_at")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_pending(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        HubPendingLockIn.objects.update_or_create(
            external_id=external_id,
            defaults={
                "quote_id": row.get("quote_id") or "",
                "client_name": row.get("client_name") or "",
                "client_jobber_id": row.get("client_jobber_id") or "",
                "status": row.get("status") or "",
                "locked_in": bool(row.get("locked_in")),
                "locked_at": parse_dt(row.get("locked_at")),
                "frequency": row.get("frequency") or "",
                "quote_sent_at": parse_dt(row.get("quote_sent_at")),
                "quote_approved_at": parse_dt(row.get("quote_approved_at")),
                "eligibility_expires_at": parse_dt(row.get("eligibility_expires_at")),
                "expected_first_visit_at": parse_dt(row.get("expected_first_visit_at")),
                "first_recurring_visit_id": row.get("first_recurring_visit_id") or "",
                "first_recurring_visit_at": parse_dt(row.get("first_recurring_visit_at")),
                "expired_reason": row.get("expired_reason") or "",
                "technician_ids": as_list(row.get("technician_ids")),
                "technician_names": as_list(row.get("technician_names")),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_bonuses(rows):
    employees = {e.external_id: e for e in HubEmployee.objects.all()}
    pendings = {p.external_id: p for p in HubPendingLockIn.objects.all()}
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        tech = as_dict(row.get("technician"))
        pending = as_dict(row.get("pending"))
        emp_id = str(tech.get("id") or "").strip()
        pending_id = str(pending.get("id") or "").strip()
        HubBonus.objects.update_or_create(
            external_id=external_id,
            defaults={
                "status": row.get("status") or "",
                "bonus_type": row.get("bonus_type") or "",
                "amount": parse_decimal(row.get("amount")),
                "position_snapshot": row.get("position_snapshot") or "",
                "bonus_confirmed": bool(row.get("bonus_confirmed")),
                "bonus_paid": bool(row.get("bonus_paid")),
                "paid_date": parse_d(row.get("paid_date")),
                "confirmed_date": parse_d(row.get("confirmed_date")),
                "in_process_date": parse_dt(row.get("in_process_date")),
                "payroll_reference": row.get("payroll_reference") or "",
                "employee": employees.get(emp_id),
                "employee_external_id": emp_id,
                "employee_name": tech.get("name") or "",
                "employee_email": tech.get("email") or "",
                "employee_position": tech.get("position") or "",
                "pending": pendings.get(pending_id),
                "pending_external_id": pending_id,
                "client_name": pending.get("client_name") or "",
                "source_created_at": parse_dt(row.get("created_at")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_visits(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        HubVisit.objects.update_or_create(
            external_id=external_id,
            defaults={
                "jobber_visit_id": row.get("jobber_visit_id") or "",
                "title": row.get("title") or "",
                "client_name": row.get("client_name") or "",
                "client_jobber_id": row.get("client_jobber_id") or "",
                "job_type": row.get("job_type") or "",
                "start_at": parse_dt(row.get("start_at")),
                "technician_ids": as_list(row.get("technician_ids")),
                "technician_names": as_list(row.get("technician_names")),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_alerts(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        HubAlert.objects.update_or_create(
            external_id=external_id,
            defaults={
                "message": row.get("message") or "",
                "active": bool(row.get("active", True)),
                "sort_order": int(row.get("sort_order") or 0),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_payload": row,
            },
        )
    return seen


def _prune(model, keep_ids):
    if not keep_ids:
        deleted, _ = model.objects.all().delete()
        return deleted
    deleted, _ = model.objects.exclude(external_id__in=keep_ids).delete()
    return deleted


def persist_payload(payload, sync_run: AdminInternalSyncRun):
    meta = as_dict(payload.get("meta"))
    employees_section = as_dict(payload.get("employees"))
    leave_section = as_dict(payload.get("leave"))
    lock_section = as_dict(payload.get("lock_in_bonuses"))
    performance_section = as_dict(payload.get("employee_performance"))
    workflows = as_dict(payload.get("internal_workflows"))
    custom_kpis = as_dict(payload.get("custom_kpis"))

    with transaction.atomic():
        emp_ids = _upsert_employees(as_list(employees_section.get("employees")))
        _apply_performance(as_list(performance_section.get("employees")))
        leave_ids = _upsert_leave(as_list(leave_section.get("all_requests")))
        pending_ids = _upsert_pending(as_list(lock_section.get("pending_lock_ins")))
        bonus_ids = _upsert_bonuses(as_list(lock_section.get("bonuses")))
        visit_ids = _upsert_visits(as_list(lock_section.get("visits")))
        alert_ids = _upsert_alerts(as_list(as_dict(workflows.get("alerts")).get("items")))

        _prune(HubEmployee, emp_ids)
        _prune(HubLeaveRequest, leave_ids)
        _prune(HubPendingLockIn, pending_ids)
        _prune(HubBonus, bonus_ids)
        _prune(HubVisit, visit_ids)
        _prune(HubAlert, alert_ids)

        sync_run.source_generated_at = parse_dt(meta.get("generated_at"))
        sync_run.source_from = parse_d(meta.get("from"))
        sync_run.source_to = parse_d(meta.get("to"))
        sync_run.meta = meta
        sync_run.summary = {
            "employees": as_dict(employees_section.get("summary")),
            "leave": as_dict(leave_section.get("summary")),
            "lock_in_bonuses": as_dict(lock_section.get("summary")),
            "custom_kpis": as_dict(custom_kpis.get("kpis")),
            "workflows": {
                "forms": as_dict(workflows.get("forms")),
                "training": as_dict(workflows.get("training")),
                "documents": as_dict(workflows.get("documents")),
                "alerts": as_dict(workflows.get("alerts")),
                "notifications": as_dict(workflows.get("notifications")),
            },
            "coverage_notes": as_dict(meta.get("coverage_notes")),
            "notes": {
                "employee_performance": performance_section.get("note") or "",
                "custom_kpis": custom_kpis.get("note") or "",
            },
        }
        sync_run.counts = {
            "employees": len(emp_ids),
            "leave_requests": len(leave_ids),
            "pending_lock_ins": len(pending_ids),
            "bonuses": len(bonus_ids),
            "visits": len(visit_ids),
            "alerts": len(alert_ids),
        }
        sync_run.status = AdminInternalSyncRun.STATUS_SUCCESS
        sync_run.finished_at = timezone.now()
        sync_run.error = ""
        sync_run.save()


def run_sync(sync_run: AdminInternalSyncRun | None = None) -> AdminInternalSyncRun:
    sync_run = sync_run or AdminInternalSyncRun.objects.create(status=AdminInternalSyncRun.STATUS_QUEUED)
    sync_run.status = AdminInternalSyncRun.STATUS_RUNNING
    sync_run.started_at = timezone.now()
    sync_run.error = ""
    sync_run.save(update_fields=["status", "started_at", "error"])

    try:
        payload = fetch_analytics_payload()
        persist_payload(payload, sync_run)
        logger.info("Admin internal sync %s succeeded: %s", sync_run.id, sync_run.counts)
    except Exception as exc:
        logger.exception("Admin internal sync %s failed", sync_run.id)
        sync_run.status = AdminInternalSyncRun.STATUS_FAILED
        sync_run.finished_at = timezone.now()
        sync_run.error = str(exc)
        sync_run.save(update_fields=["status", "finished_at", "error"])
    return sync_run


def latest_successful_sync():
    return AdminInternalSyncRun.objects.filter(status=AdminInternalSyncRun.STATUS_SUCCESS).first()


def latest_sync():
    return AdminInternalSyncRun.objects.first()
