from datetime import date
from decimal import Decimal

from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Case, Count, IntegerField, Q, Sum, Value, When
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_GET

from analytics.models import MetricFact
from operations.models import Client, Invoice, Job, Visit
from operations import analytics_views as ops_analytics
from operations import celery_views as celery_api


def _num(value):
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    return value


def _label(value):
    text = (value or "").replace("_", " ").strip()
    return text.title() if text else "—"


def _clean_name(value):
    text = (value or "").strip()
    if not text or text in {"-", "—", "n/a", "N/A"}:
        return ""
    return text


def _looks_like_phone(value) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    digits = sum(ch.isdigit() for ch in text)
    if digits < 7:
        return False
    return all(ch.isdigit() or ch in "+()- ." for ch in text)


def client_display_name(client, *, include_phone_fallback=True) -> str:
    if not client:
        return ""
    full = " ".join(part for part in [_clean_name(client.first_name), _clean_name(client.last_name)] if part)
    if full:
        return full
    company = _clean_name(client.company_name)
    if company:
        return company
    name = _clean_name(client.name)
    if name and not _looks_like_phone(name):
        return name
    if include_phone_fallback:
        phone = _clean_name(client.phone)
        if phone:
            return phone
        email = _clean_name(client.email)
        if email:
            return email
    return "Unnamed customer"


def _unnamed_client():
    return (
        Q(name__in=["", "-"])
        & Q(first_name__in=["", "-"])
        & Q(last_name__in=["", "-"])
        & Q(company_name="")
    )


def _money(value):
    return round(_num(value), 2)


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


def _date_range(qs, request, field):
    start = request.GET.get("from")
    end = request.GET.get("to")
    if start:
        qs = qs.filter(**{f"{field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lt": end})
    return qs


def _last_months(count=12):
    today = timezone.localdate()
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def _fill_months(rows, value_key="value"):
    lookup = {}
    for row in rows:
        month = row["month"]
        if month:
            lookup[date(month.year, month.month, 1)] = _num(row.get(value_key) or 0)
    return [{"month": month.isoformat()[:7], "value": lookup.get(month, 0)} for month in _last_months()]


@require_GET
def dashboard(request):
    from integrations.models import Integration

    integration = Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()
    visits = Visit.objects.all()
    jobs = Job.objects.all()
    invoices = Invoice.objects.all()
    clients = Client.objects.filter(is_archived=False)
    if integration:
        visits = visits.filter(integration=integration)
        jobs = jobs.filter(integration=integration)
        invoices = invoices.filter(integration=integration)
        clients = clients.filter(integration=integration)
    month_start = timezone.localdate().replace(day=1)
    visits_this_month = visits.filter(start_at__date__gte=month_start)

    invoice_status = [
        {
            "label": _label(row["invoice_status"]),
            "key": (row["invoice_status"] or "").lower(),
            "count": row["count"],
            "amount": _money(row["amount"]),
        }
        for row in invoices.values("invoice_status").annotate(count=Count("id"), amount=Sum("subtotal")).order_by("-amount")
    ]

    facts = MetricFact.objects.filter(bucket_date__gte=_last_months()[0])
    revenue_month = (
        facts.filter(kpi_key="invoice_revenue")
        .annotate(month=TruncMonth("bucket_date"))
        .values("month")
        .annotate(value=Sum("value"))
        .order_by("month")
    )
    visits_month = (
        facts.filter(kpi_key="visits_total")
        .annotate(month=TruncMonth("bucket_date"))
        .values("month")
        .annotate(value=Sum("value"))
        .order_by("month")
    )
    completed_month = (
        facts.filter(kpi_key="visits_completed")
        .annotate(month=TruncMonth("bucket_date"))
        .values("month")
        .annotate(value=Sum("value"))
        .order_by("month")
    )

    mix = [
        {"label": "Recurring", "key": "recurring", "value": visits.filter(is_recurring=True).count()},
        {"label": "One-off", "key": "one_off", "value": visits.filter(is_one_off=True).count()},
        {"label": "First cleans", "key": "first_clean", "value": visits.filter(is_first_clean=True).count()},
        {"label": "Deep cleans", "key": "deep_clean", "value": visits.filter(is_deep_clean=True).count()},
    ]

    recent_visits = [
        {
            "id": visit.id,
            "title": visit.title or "Visit",
            "client": client_display_name(visit.client) or "—",
            "start_at": visit.start_at,
            "status": _label(visit.visit_status),
            "amount": _money(visit.price_per_visit),
        }
        for visit in visits.select_related("client").order_by("-start_at", "-id")[:8]
    ]

    avg_price = visits.exclude(is_cancelled=True).exclude(price_per_visit__isnull=True).aggregate(a=Avg("price_per_visit"))["a"]
    new_recurring = jobs.filter(is_recurring=True).count()

    from operations.models import CancellationRecord, CANCELLATION_VISIT, CANCELLATION_JOB

    cancel_visits = CancellationRecord.objects.filter(cancellation_type=CANCELLATION_VISIT)
    cancel_jobs = CancellationRecord.objects.filter(cancellation_type=CANCELLATION_JOB)
    if integration:
        cancel_visits = cancel_visits.filter(integration=integration)
        cancel_jobs = cancel_jobs.filter(integration=integration)

    return JsonResponse(
        {
            "ok": True,
            "kpis": [
                {
                    "key": "revenue",
                    "label": "Invoice revenue",
                    "value": _money(invoices.aggregate(s=Sum("subtotal"))["s"]),
                    "kind": "currency",
                    "view": "invoices",
                },
                {
                    "key": "outstanding",
                    "label": "Outstanding",
                    "value": _money(invoices.aggregate(s=Sum("balance"))["s"]),
                    "kind": "currency",
                    "view": "invoices",
                    "filters": {"outstanding": "1"},
                },
                {
                    "key": "visits_total",
                    "label": "Total visits",
                    "value": visits.count(),
                    "kind": "count",
                    "view": "visits",
                },
                {
                    "key": "completed",
                    "label": "Completed visits",
                    "value": visits.filter(is_complete=True).count(),
                    "kind": "count",
                    "view": "visits",
                    "filters": {"status": "completed"},
                },
                {
                    "key": "cancelled",
                    "label": "Cancelled visits",
                    "value": cancel_visits.count(),
                    "kind": "count",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_VISIT},
                },
                {
                    "key": "cancelled_jobs",
                    "label": "Cancelled jobs",
                    "value": cancel_jobs.count(),
                    "kind": "count",
                    "view": "cancellations",
                    "filters": {"type": CANCELLATION_JOB},
                },
                {
                    "key": "one_off",
                    "label": "One-off jobs",
                    "value": jobs.filter(is_one_off=True).count(),
                    "kind": "count",
                    "view": "oneoff",
                },
                {
                    "key": "recurring",
                    "label": "Recurring visits",
                    "value": visits.filter(is_recurring=True).count(),
                    "kind": "count",
                    "view": "visits",
                    "filters": {"type": "recurring"},
                },
                {
                    "key": "first_cleans",
                    "label": "First cleans",
                    "value": visits.filter(is_first_clean=True).count(),
                    "kind": "count",
                    "view": "visits",
                    "filters": {"type": "first_clean"},
                },
                {
                    "key": "deep_cleans",
                    "label": "Deep cleans",
                    "value": visits.filter(is_deep_clean=True).count(),
                    "kind": "count",
                    "view": "visits",
                    "filters": {"type": "deep_clean"},
                },
                {
                    "key": "new_recurring",
                    "label": "New recurring jobs",
                    "value": new_recurring,
                    "kind": "count",
                    "view": "jobs",
                    "filters": {"type": "recurring"},
                },
                {
                    "key": "avg_price",
                    "label": "Avg price / visit",
                    "value": _money(avg_price),
                    "kind": "currency",
                    "view": "visits",
                },
                {
                    "key": "clients",
                    "label": "Customers",
                    "value": clients.count(),
                    "kind": "count",
                    "view": "clients",
                },
                {
                    "key": "visits",
                    "label": "Visits this month",
                    "value": visits_this_month.count(),
                    "kind": "count",
                    "view": "visits",
                    "filters": {"from": month_start.isoformat()},
                },
                {
                    "key": "jobs",
                    "label": "Jobs",
                    "value": jobs.count(),
                    "kind": "count",
                    "view": "jobs",
                },
            ],
            "charts": {
                "revenue": _fill_months(revenue_month),
                "visits": _fill_months(visits_month),
                "completed": _fill_months(completed_month),
                "mix": mix,
                "invoices": invoice_status,
            },
            "recent_visits": recent_visits,
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def summary(request):
    return dashboard(request)


def _visit_card(visit):
    return {
        "id": visit.id,
        "title": visit.title or "Visit",
        "status": _label(visit.visit_status),
        "start_at": visit.start_at,
        "end_at": visit.end_at,
        "completed_at": visit.completed_at,
        "client": client_display_name(visit.client) or None,
        "job": visit.job.title if visit.job else None,
        "team": [emp.full_name for emp in visit.assigned_employees.all()],
        "amount": _money(visit.price_per_visit),
        "kind": "Recurring" if visit.is_recurring else "One-off",
        "tags": [
            label
            for flag, label in (
                (visit.is_first_visit, "First visit"),
                (visit.is_first_clean, "First clean"),
                (visit.is_deep_clean, "Deep clean"),
                (visit.is_complete, "Completed"),
                (visit.is_cancelled, "Cancelled"),
            )
            if flag
        ],
    }


@require_GET
def jobs(request):
    qs = Job.objects.select_related("client", "salesperson").order_by("-source_created_at", "-id")
    status = request.GET.get("status")
    job_type = request.GET.get("type")
    search = request.GET.get("q")
    qs = _date_range(qs, request, "start_at")
    if status:
        qs = qs.filter(job_status__iexact=status)
    if job_type == "recurring":
        qs = qs.filter(is_recurring=True)
    elif job_type == "one_off":
        qs = qs.filter(is_one_off=True)
    division = request.GET.get("division") or ""
    if division:
        qs = qs.filter(division=division)
    service_type = request.GET.get("service_type") or ""
    if service_type:
        qs = qs.filter(service_type=service_type)
    employee = request.GET.get("employee") or request.GET.get("team_leader") or ""
    if employee:
        qs = qs.filter(Q(salesperson_id=employee) | Q(team_leader_id=employee))
    city = request.GET.get("city") or ""
    if city:
        qs = qs.filter(Q(property__city__icontains=city) | Q(client__billing_city__icontains=city))
    source = request.GET.get("source") or request.GET.get("sales_source") or ""
    if source:
        qs = qs.filter(source__icontains=source)
    if search:
        filters = Q(title__icontains=search) | Q(client__name__icontains=search) | Q(client__first_name__icontains=search) | Q(client__last_name__icontains=search)
        if search.isdigit():
            filters |= Q(job_number=int(search))
        qs = qs.filter(filters)
    return _page(
        qs,
        request,
        lambda job: {
            "id": job.id,
            "number": job.job_number,
            "title": job.title or f"Job #{job.job_number or job.id}",
            "status": _label(job.job_status),
            "kind": "Recurring" if job.is_recurring else "One-off",
            "division": job.division,
            "service_type": job.service_type,
            "total": _money(job.total),
            "start_at": job.start_at,
            "client": client_display_name(job.client) or None,
            "salesperson": job.salesperson.full_name if job.salesperson else None,
        },
    )


@require_GET
def job_detail(request, pk):
    job = get_object_or_404(
        Job.objects.select_related("client", "property", "salesperson").prefetch_related("line_items"),
        pk=pk,
    )
    visits = [_visit_card(visit) for visit in job.visits.select_related("client", "job").prefetch_related("assigned_employees").order_by("-start_at")[:20]]
    address = job.property
    return JsonResponse(
        {
            "ok": True,
            "type": "job",
            "item": {
                "id": job.id,
                "title": job.title or f"Job #{job.job_number or job.id}",
                "number": job.job_number,
                "status": _label(job.job_status),
                "kind": "Recurring" if job.is_recurring else "One-off",
                "client": client_display_name(job.client) or None,
                "client_id": job.client_id,
                "salesperson": job.salesperson.full_name if job.salesperson else None,
                "address": ", ".join(part for part in [address.street, address.city, address.province] if part) if address else None,
                "start_at": job.start_at,
                "end_at": job.end_at,
                "completed_at": job.completed_at,
                "total": _money(job.total),
                "invoiced": _money(job.invoiced_total),
                "uninvoiced": _money(job.uninvoiced_total),
                "instructions": job.instructions,
                "line_items": [
                    {
                        "name": item.name,
                        "quantity": _num(item.quantity),
                        "total": _money(item.total),
                    }
                    for item in job.line_items.all()[:30]
                ],
                "visits": visits,
            },
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def visits(request):
    qs = Visit.objects.select_related("client", "job").prefetch_related("assigned_employees").order_by("-start_at", "-id")
    status = request.GET.get("status")
    kind = request.GET.get("type")
    search = request.GET.get("q")
    qs = _date_range(qs, request, "start_at")
    if status == "completed":
        qs = qs.filter(is_complete=True)
    elif status == "cancelled":
        qs = qs.filter(is_cancelled=True)
    elif status:
        qs = qs.filter(visit_status__iexact=status)
    if kind == "recurring":
        qs = qs.filter(is_recurring=True)
    elif kind == "one_off":
        qs = qs.filter(is_one_off=True)
    elif kind == "first_clean":
        qs = qs.filter(is_first_clean=True)
    elif kind == "deep_clean":
        qs = qs.filter(is_deep_clean=True)
    division = request.GET.get("division") or ""
    if division:
        qs = qs.filter(division=division)
    service_type = request.GET.get("service_type") or ""
    if service_type:
        qs = qs.filter(service_type=service_type)
    employee = request.GET.get("employee") or ""
    if employee:
        qs = qs.filter(assigned_employees__id=employee)
    team_leader = request.GET.get("team_leader") or ""
    if team_leader:
        qs = qs.filter(team_leader_id=team_leader)
    city = request.GET.get("city") or ""
    if city:
        qs = qs.filter(Q(property__city__icontains=city) | Q(client__billing_city__icontains=city))
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(client__name__icontains=search) | Q(client__first_name__icontains=search) | Q(client__last_name__icontains=search))
    return _page(qs, request, _visit_card)


@require_GET
def visit_detail(request, pk):
    visit = get_object_or_404(
        Visit.objects.select_related("client", "job", "property", "invoice").prefetch_related("assigned_employees", "line_items"),
        pk=pk,
    )
    address = visit.property
    return JsonResponse(
        {
            "ok": True,
            "type": "visit",
            "item": {
                **_visit_card(visit),
                "client_id": visit.client_id,
                "job_id": visit.job_id,
                "address": ", ".join(part for part in [address.street, address.city, address.province] if part) if address else None,
                "duration_minutes": visit.duration_minutes,
                "completed_by": visit.completed_by,
                "instructions": visit.instructions,
                "invoice": visit.invoice.invoice_number if visit.invoice else None,
                "invoice_id": visit.invoice_id,
                "line_items": [
                    {"name": item.name, "quantity": _num(item.quantity), "total": _money(item.total)}
                    for item in visit.line_items.all()[:30]
                ],
            },
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def clients(request):
    qs = Client.objects.annotate(
        job_count=Count("jobs", distinct=True),
        visit_count=Count("visits", distinct=True),
        phone_name=Case(
            When(Q(name__startswith="+"), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        unnamed=Case(
            When(_unnamed_client(), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    ).order_by("phone_name", "unnamed", "name", "last_name", "first_name")
    search = request.GET.get("q")
    if request.GET.get("leads") == "1":
        qs = qs.filter(is_lead=True)
    elif request.GET.get("archived") == "1":
        qs = qs.filter(is_archived=True)
    else:
        qs = qs.filter(is_archived=False)
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(company_name__icontains=search)
            | Q(email__icontains=search)
            | Q(phone__icontains=search)
        )
    return _page(
        qs,
        request,
        lambda client: {
            "id": client.id,
            "name": client_display_name(client, include_phone_fallback=False),
            "first_name": _clean_name(client.first_name),
            "last_name": _clean_name(client.last_name),
            "company_name": _clean_name(client.company_name),
            "email": client.email,
            "phone": client.phone,
            "city": client.billing_city,
            "balance": _money(client.balance),
            "jobs": client.job_count,
            "visits": client.visit_count,
            "lead": client.is_lead,
        },
    )


@require_GET
def client_detail(request, pk):
    client = get_object_or_404(Client, pk=pk)
    jobs = [
        {
            "id": job.id,
            "title": job.title or f"Job #{job.job_number or job.id}",
            "status": _label(job.job_status),
            "total": _money(job.total),
        }
        for job in client.jobs.order_by("-source_created_at")[:12]
    ]
    visits = [_visit_card(visit) for visit in client.visits.select_related("client", "job").prefetch_related("assigned_employees").order_by("-start_at")[:12]]
    address = ", ".join(
        part
        for part in [client.billing_street, client.billing_city, client.billing_province, client.billing_postal_code]
        if part
    )
    return JsonResponse(
        {
            "ok": True,
            "type": "client",
            "item": {
                "id": client.id,
                "name": client_display_name(client),
                "email": client.email,
                "phone": client.phone,
                "company": client.company_name,
                "address": address or None,
                "balance": _money(client.balance),
                "lead": client.is_lead,
                "jobs": jobs,
                "visits": visits,
            },
        },
        encoder=DjangoJSONEncoder,
    )


@require_GET
def invoices(request):
    qs = Invoice.objects.select_related("client").order_by("-issued_date", "-id")
    search = request.GET.get("q")
    status = request.GET.get("status")
    qs = _date_range(qs, request, "issued_date")
    if request.GET.get("outstanding") == "1":
        qs = qs.filter(balance__gt=0)
    if status:
        qs = qs.filter(invoice_status__iexact=status)
    if search:
        qs = qs.filter(Q(invoice_number__icontains=search) | Q(subject__icontains=search) | Q(client__name__icontains=search))
    return _page(
        qs,
        request,
        lambda invoice: {
            "id": invoice.id,
            "number": invoice.invoice_number or f"INV-{invoice.id}",
            "status": _label(invoice.invoice_status),
            "issued_at": invoice.issued_date,
            "due_at": invoice.due_date,
            "total": _money(invoice.subtotal if invoice.subtotal is not None else invoice.total),
            "subtotal": _money(invoice.subtotal),
            "tax": _money(invoice.tax_amount),
            "gross_total": _money(invoice.total),
            "balance": _money(invoice.balance),
            "client": client_display_name(invoice.client) or None,
        },
    )


@require_GET
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("client").prefetch_related("line_items", "jobs"), pk=pk)
    return JsonResponse(
        {
            "ok": True,
            "type": "invoice",
            "item": {
                "id": invoice.id,
                "number": invoice.invoice_number or f"INV-{invoice.id}",
                "status": _label(invoice.invoice_status),
                "subject": invoice.subject,
                "client": client_display_name(invoice.client) or None,
                "client_id": invoice.client_id,
                "issued_at": invoice.issued_date,
                "due_at": invoice.due_date,
                "total": _money(invoice.subtotal if invoice.subtotal is not None else invoice.total),
                "subtotal": _money(invoice.subtotal),
                "tax": _money(invoice.tax_amount),
                "gross_total": _money(invoice.total),
                "balance": _money(invoice.balance),
                "paid": _money(invoice.payments_total),
                "jobs": [{"id": job.id, "title": job.title or f"Job #{job.job_number or job.id}"} for job in invoice.jobs.all()[:12]],
                "line_items": [
                    {"name": item.name, "quantity": _num(item.quantity), "total": _money(item.total)}
                    for item in invoice.line_items.all()[:40]
                ],
            },
        },
        encoder=DjangoJSONEncoder,
    )


urlpatterns = [
    path("dashboard/", dashboard),
    path("summary/", summary),
    path("jobs/", jobs),
    path("jobs/<int:pk>/", job_detail),
    path("visits/", visits),
    path("visits/<int:pk>/", visit_detail),
    path("clients/", clients),
    path("clients/<int:pk>/", client_detail),
    path("invoices/", invoices),
    path("invoices/<int:pk>/", invoice_detail),
    path("filters/", ops_analytics.jobber_filters),
    path("one-off/", ops_analytics.one_off_dashboard),
    path("cancellations/dashboard/", ops_analytics.cancellations_dashboard),
    path("cancellations/", ops_analytics.cancellations_list),
    path("cancellations/process/", ops_analytics.process_cancellations),
    path("cx/", ops_analytics.cx_dashboard),
    path("mappings/service-types/", ops_analytics.service_type_mappings),
    path("mappings/divisions/", ops_analytics.division_rules),
    path("celery/status/", celery_api.celery_status),
    path("celery/run/", celery_api.celery_run),
]
