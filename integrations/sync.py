import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from analytics.models import KpiDefinition, MetricFact
from integrations.client import JobberAPIError, JobberClient
from integrations.mapping import (
    classify_title,
    clip,
    clean_name,
    format_street,
    is_cancelled_status,
    is_completed_status,
    is_one_off_type,
    is_recurring_type,
    line_item_total,
    line_item_unit_price,
    node_id,
    parse_decimal,
    parse_dt,
    person_display_name,
    money_total,
)
from integrations.models import Integration, SchemaSnapshot, SyncRun
from integrations.queries import (
    ACCOUNT_QUERY,
    APP_DISCONNECT,
    CLIENTS_SELECTION,
    INVOICES_SELECTION,
    JOBS_SELECTION,
    PROPERTIES_SELECTION,
    QUERY_FIELDS_INTROSPECTION,
    TASKS_SELECTION,
    TYPE_INTROSPECTION,
    TYPES_TO_INTROSPECT,
    USERS_SELECTION,
    VISITS_SELECTION,
)
from integrations.versioning import extract_deprecated_fields
from operations.models import (
    Client,
    Employee,
    Invoice,
    InvoiceLineItem,
    Job,
    JobLineItem,
    JobberTask,
    Property,
    Visit,
    VisitAssignment,
)
from operations.taxonomy import classify_division, classify_service_type, seed_taxonomy_defaults
from operations.cancellations import process_all_cancellation_tasks, process_cancellation_task
from operations.taxonomy import cancellation_type_from_title

logger = logging.getLogger(__name__)

DEFAULT_KPIS = [
    {
        "key": "visits_total",
        "name": "Visits",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {},
    },
    {
        "key": "visits_completed",
        "name": "Completed visits",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "completed_at",
        "filters": {"is_complete": True},
    },
    {
        "key": "visits_cancelled",
        "name": "Cancelled visits",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {"is_cancelled": True},
    },
    {
        "key": "visits_one_off",
        "name": "One-off visits",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {"is_one_off": True},
    },
    {
        "key": "visits_recurring",
        "name": "Recurring visits",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {"is_recurring": True},
    },
    {
        "key": "first_cleans",
        "name": "First cleans",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {"is_first_clean": True},
    },
    {
        "key": "deep_cleans",
        "name": "Deep cleans",
        "category": "operations",
        "unit": "count",
        "source_model": "visit",
        "aggregation": "count",
        "date_field": "start_at",
        "filters": {"is_deep_clean": True},
    },
    {
        "key": "new_recurring_jobs",
        "name": "New recurring jobs",
        "category": "sales",
        "unit": "count",
        "source_model": "job",
        "aggregation": "count",
        "date_field": "source_created_at",
        "filters": {"is_recurring": True},
    },
    {
        "key": "invoice_revenue",
        "name": "Invoice revenue",
        "category": "finance",
        "unit": "currency",
        "source_model": "invoice",
        "aggregation": "sum",
        "date_field": "issued_date",
        "value_field": "total",
        "filters": {},
    },
    {
        "key": "visit_revenue",
        "name": "Visit revenue",
        "category": "finance",
        "unit": "currency",
        "source_model": "visit",
        "aggregation": "sum",
        "date_field": "start_at",
        "value_field": "price_per_visit",
        "filters": {"is_cancelled": False},
    },
    {
        "key": "avg_price_per_visit",
        "name": "Average price per visit",
        "category": "operations",
        "unit": "currency",
        "source_model": "visit",
        "aggregation": "avg",
        "date_field": "start_at",
        "value_field": "price_per_visit",
        "filters": {"is_cancelled": False},
    },
    {
        "key": "cancelled_visit_events",
        "name": "Cancelled visit events",
        "category": "cancellations",
        "unit": "count",
        "source_model": "cancellation",
        "aggregation": "count",
        "date_field": "task_date",
        "filters": {"cancellation_type": "cancelled_visit"},
    },
    {
        "key": "cancelled_job_events",
        "name": "Cancelled job events",
        "category": "cancellations",
        "unit": "count",
        "source_model": "cancellation",
        "aggregation": "count",
        "date_field": "task_date",
        "filters": {"cancellation_type": "cancelled_job"},
    },
]


class JobberSyncService:
    def __init__(self, integration: Integration, sync_run: SyncRun | None = None):
        self.integration = integration
        self.sync_run = sync_run
        self.client = JobberClient(integration)
        self.counts = {}
        self.query_fields: set[str] = set()
        self._client_cache: dict[str, Client] = {}
        self._employee_cache: dict[str, Employee] = {}
        self._property_cache: dict[str, Property] = {}
        self._job_cache: dict[str, Job] = {}
        self._invoice_cache: dict[str, Invoice] = {}

    def run_full(self) -> SyncRun:
        run = self.sync_run or SyncRun.objects.create(
            integration=self.integration,
            kind=SyncRun.KIND_FULL,
            status=SyncRun.STATUS_QUEUED,
        )
        run.status = SyncRun.STATUS_RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])
        try:
            self._introspect()
            self._sync_account()
            self._sync_users()
            self._sync_clients()
            self._sync_properties()
            self._sync_jobs()
            self._sync_invoices()
            self._sync_visits()
            self._sync_tasks(
                created_after=(timezone.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                cancellation_only=True,
            )
            self._backfill_visit_flags()
            self._refresh_client_averages()
            seed_taxonomy_defaults()
            self._seed_kpis()
            cancel_stats = process_all_cancellation_tasks(self.integration)
            self._record("cancellations_created", cancel_stats.get("created", 0))
            self._rebuild_metric_facts()
            run.status = SyncRun.STATUS_SUCCESS
            run.error_message = ""
            self.integration.last_synced_at = timezone.now()
            self.integration.last_error = ""
            self.integration.status = Integration.STATUS_ACTIVE
            self.integration.save(update_fields=["last_synced_at", "last_error", "status", "updated_at"])
        except Exception as exc:
            logger.exception("Jobber full sync failed")
            run.status = SyncRun.STATUS_FAILED
            run.error_message = str(exc)
            self.integration.last_error = str(exc)
            self.integration.save(update_fields=["last_error", "updated_at"])
            raise
        finally:
            run.entity_counts = self.counts
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_message", "entity_counts", "finished_at"])
        return run

    def _record(self, entity: str, count: int):
        self.counts[entity] = self.counts.get(entity, 0) + count
        if self.sync_run:
            self.sync_run.entity_counts = self.counts
            self.sync_run.save(update_fields=["entity_counts"])

    def _introspect(self):
        try:
            data = self.client.execute(QUERY_FIELDS_INTROSPECTION)
            fields = ((data.get("__type") or {}).get("fields")) or []
            self.query_fields = {field["name"] for field in fields if field.get("name")}
            types = {}
            for type_name in TYPES_TO_INTROSPECT:
                try:
                    typed = self.client.execute(TYPE_INTROSPECTION, {"name": type_name})
                    types[type_name] = typed.get("__type")
                except JobberAPIError as exc:
                    types[type_name] = {"error": str(exc)}
                    logger.warning("Could not introspect %s: %s", type_name, exc)
            deprecated = extract_deprecated_fields(types)
            SchemaSnapshot.objects.create(
                integration=self.integration,
                api_version=self.client.version,
                served_api_version=(self.client.last_versioning or {}).get("served") or "",
                query_fields=sorted(self.query_fields),
                types=types,
                deprecated_fields=deprecated,
                versioning=self.client.last_versioning or {},
            )
            self._record("schema_fields", len(self.query_fields))
            self._record("deprecated_fields", len(deprecated))
        except JobberAPIError as exc:
            logger.warning("Schema introspection failed: %s", exc)

    def _has_root(self, name: str) -> bool:
        if not self.query_fields:
            return True
        return name in self.query_fields

    MINIMAL_SELECTIONS = {
        "jobs": "id jobNumber title jobStatus jobType total startAt endAt createdAt updatedAt client { id } property { id }",
        "visits": "id title visitStatus isComplete startAt endAt completedAt createdAt client { id } job { id }",
        "invoices": "id invoiceNumber invoiceStatus issuedDate createdAt updatedAt client { id } amounts { total }",
        "clients": "id name firstName lastName email phone isLead isArchived createdAt updatedAt",
        "users": "id status name { first last full } email { raw }",
        "properties": "id name client { id }",
        "tasks": "id title startAt endAt isComplete client { id }",
    }

    def _paginate(self, name: str, selection: str, extra_args: str = "", page_size: int | None = None):
        if not self._has_root(name):
            logger.warning("Jobber Query has no `%s` root field; skipping.", name)
            return
        working = selection
        filter_bit = f", {extra_args}" if extra_args else ""
        probe = f"""
        query Probe {{
          {name}(first: 1{filter_bit}) {{
            nodes {{ {selection} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        try:
            self.client.execute(probe)
        except JobberAPIError as exc:
            logger.warning("%s full selection failed (%s); using minimal fields.", name, exc)
            working = self.MINIMAL_SELECTIONS.get(name, "id createdAt updatedAt")
        yield from self.client.paginate(name, working, page_size=page_size, extra_args=extra_args)

    def _lookup(self, cache: dict, model, external_id: str):
        if not external_id:
            return None
        if external_id in cache:
            return cache[external_id]
        obj = model.objects.filter(integration=self.integration, external_id=external_id).first()
        if obj:
            cache[external_id] = obj
        return obj

    def _upsert_user(self, node: dict) -> Employee | None:
        if not node or not node.get("id"):
            return None
        name = node.get("name") if isinstance(node.get("name"), dict) else {}
        email = node.get("email")
        phone = node.get("phone") if isinstance(node.get("phone"), dict) else {}
        email_address = email if isinstance(email, str) else (email or {}).get("raw") or (email or {}).get("address") or ""
        employee, _ = Employee.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "first_name": clip(name.get("first"), 255),
                "last_name": clip(name.get("last"), 255),
                "full_name": clip(
                    name.get("full") or " ".join(filter(None, [name.get("first"), name.get("last")])),
                    255,
                ),
                "email": clip(email_address, 255),
                "phone": clip(phone.get("number") or phone.get("raw") or "", 255),
                "status": clip(node.get("status"), 64),
                "is_admin": bool(node.get("isAccountAdmin")),
                "is_owner": bool(node.get("isAccountOwner")),
                "available_for_scheduling": True if node.get("availableForScheduling") is None else bool(node.get("availableForScheduling")),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_payload": node,
            },
        )
        self._employee_cache[employee.external_id] = employee
        return employee

    def _upsert_client(self, node: dict) -> Client | None:
        if not node or not node.get("id"):
            return None
        address = node.get("billingAddress") or {}
        client, _ = Client.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "name": person_display_name(node) or clip(node.get("phone") or node.get("email"), 255),
                "first_name": clean_name(node.get("firstName")),
                "last_name": clean_name(node.get("lastName")),
                "company_name": clean_name(node.get("companyName")),
                "email": clip(node.get("email"), 255),
                "phone": clip(node.get("phone"), 255),
                "is_company": bool(node.get("isCompany")),
                "is_lead": bool(node.get("isLead")),
                "is_archived": bool(node.get("isArchived")),
                "balance": parse_decimal(node.get("balance")),
                "billing_city": clip(address.get("city"), 255),
                "billing_province": clip(address.get("province") or address.get("state"), 255),
                "billing_country": clip(address.get("country"), 120),
                "billing_postal_code": clip(address.get("postalCode"), 64),
                "billing_street": format_street(address),
                "jobber_web_uri": clip(node.get("jobberWebUri"), 1000),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )
        self._client_cache[client.external_id] = client
        return client

    def apply_remote_node(self, object_name: str, item_id: str):
        mapping = {
            "CLIENT": ("client", CLIENTS_SELECTION, self._upsert_client),
            "JOB": ("job", JOBS_SELECTION, self._upsert_job),
            "VISIT": ("visit", VISITS_SELECTION, self._upsert_visit),
            "SCHEDULED_ITEM": ("visit", VISITS_SELECTION, self._upsert_visit),
            "INVOICE": ("invoice", INVOICES_SELECTION, self._upsert_invoice),
            "PROPERTY": ("property", PROPERTIES_SELECTION, self._upsert_property_from_node),
            "USER": ("user", USERS_SELECTION, self._upsert_user),
            "TASK": ("task", TASKS_SELECTION, self._upsert_task),
        }
        spec = mapping.get(object_name)
        if not spec:
            logger.info("No local mapper for Jobber webhook object %s", object_name)
            return False
        root, selection, upsert = spec
        from integrations.queries import node_query

        data = self.client.execute(node_query(root, selection), {"id": item_id})
        node = data.get(root)
        if not node:
            logger.warning("Jobber %s %s was empty after webhook fetch", object_name, item_id)
            return False
        upsert(node)
        return True

    def delete_remote_node(self, object_name: str, item_id: str):
        model_map = {
            "CLIENT": Client,
            "JOB": Job,
            "VISIT": Visit,
            "SCHEDULED_ITEM": Visit,
            "INVOICE": Invoice,
            "PROPERTY": Property,
            "USER": Employee,
            "TASK": JobberTask,
        }
        model = model_map.get(object_name)
        if not model:
            return 0
        deleted, _ = model.objects.filter(integration=self.integration, external_id=item_id).delete()
        return deleted

    def _sync_account(self):
        data = self.client.execute(ACCOUNT_QUERY)
        account = data.get("account") or {}
        if account.get("id"):
            self.integration.account_external_id = account["id"]
            self.integration.account_name = account.get("name") or ""
            self.integration.metadata = {**(self.integration.metadata or {}), "account": account}
            self.integration.save(update_fields=["account_external_id", "account_name", "metadata", "updated_at"])
            self._record("account", 1)

    def _sync_users(self):
        count = 0
        for node in self._paginate("users", USERS_SELECTION):
            self._upsert_user(node)
            count += 1
        self._record("users", count)

    def _sync_clients(self):
        count = 0
        for node in self._paginate("clients", CLIENTS_SELECTION):
            self._upsert_client(node)
            count += 1
        self._record("clients", count)

    def _upsert_property_from_node(self, node: dict) -> Property | None:
        if not node or not node.get("id"):
            return None
        cached = self._property_cache.get(node["id"])
        if cached and not node.get("address"):
            return cached
        existing = Property.objects.filter(integration=self.integration, external_id=node["id"]).first()
        if existing and not node.get("address"):
            if node.get("name"):
                existing.name = clip(node.get("name"), 255)
                existing.save(update_fields=["name"])
            self._property_cache[existing.external_id] = existing
            return existing
        address = node.get("address") or {}
        client = self._lookup(self._client_cache, Client, node_id(node.get("client")))
        obj, _ = Property.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": client,
                "name": clip(node.get("name"), 255),
                "street": format_street(address),
                "city": clip(address.get("city"), 255),
                "province": clip(address.get("province") or address.get("state"), 255),
                "postal_code": clip(address.get("postalCode"), 64),
                "country": clip(address.get("country"), 120),
                "is_billing_address": node.get("isBillingAddress"),
                "source_payload": node,
            },
        )
        self._property_cache[obj.external_id] = obj
        return obj

    def _upsert_job(self, node: dict) -> Job | None:
        if not node or not node.get("id"):
            return None
        job_type = node.get("jobType") or ""
        title = node.get("title") or node.get("defaultVisitTitle") or ""
        first_clean, deep_clean = classify_title(title)
        visits_info = node.get("visitsInfo") or {}
        first_visit = visits_info.get("firstVisit") or {}
        source = node.get("source")
        source_label = source if isinstance(source, str) else (source or {}).get("name") or (source or {}).get("__typename") or ""
        client = self._lookup(self._client_cache, Client, node_id(node.get("client")))
        prop = self._upsert_property_from_node(node.get("property") or {})
        division = classify_division(
            title,
            client.name if client else "",
            client.company_name if client else "",
            prop.city if prop else "",
        )
        service_type = classify_service_type(title) if is_one_off_type(job_type) else ""
        job, _ = Job.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": client,
                "property": prop,
                "salesperson": self._lookup(self._employee_cache, Employee, node_id(node.get("salesperson"))),
                "team_leader": self._lookup(self._employee_cache, Employee, node_id(node.get("salesperson"))),
                "job_number": node.get("jobNumber"),
                "title": clip(title, 500),
                "job_status": clip(node.get("jobStatus"), 64),
                "job_type": clip(job_type, 64),
                "billing_type": clip(node.get("billingType"), 64),
                "source": clip(source_label, 255),
                "total": money_total(node),
                "invoiced_total": parse_decimal(node.get("invoicedTotal")),
                "uninvoiced_total": parse_decimal(node.get("uninvoicedTotal")),
                "start_at": parse_dt(node.get("startAt")),
                "end_at": parse_dt(node.get("endAt")),
                "completed_at": parse_dt(node.get("completedAt")),
                "instructions": node.get("instructions") or "",
                "first_visit_external_id": first_visit.get("id") or "",
                "first_visit_at": parse_dt(first_visit.get("startAt")),
                "is_recurring": is_recurring_type(job_type),
                "is_one_off": is_one_off_type(job_type),
                "is_first_clean": first_clean,
                "is_deep_clean": deep_clean,
                "division": division,
                "service_type": service_type,
                "monthly_recurring_value": money_total(node) if is_recurring_type(job_type) else None,
                "jobber_web_uri": clip(node.get("jobberWebUri"), 1000),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )
        if client and division and not client.division:
            client.division = division
            client.save(update_fields=["division"])
        self._job_cache[job.external_id] = job
        return job

    def _upsert_invoice(self, node: dict) -> Invoice | None:
        if not node or not node.get("id"):
            return None
        amounts = node.get("amounts") or {}
        invoice, _ = Invoice.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": self._lookup(self._client_cache, Client, node_id(node.get("client"))),
                "invoice_number": clip(node.get("invoiceNumber"), 64),
                "invoice_status": clip(node.get("invoiceStatus"), 64),
                "subject": clip(node.get("subject"), 500),
                "issued_date": parse_dt(node.get("issuedDate")),
                "due_date": parse_dt(node.get("dueDate")),
                "total": money_total(node),
                "subtotal": parse_decimal(amounts.get("subtotal")),
                "tax_amount": parse_decimal(amounts.get("taxAmount")),
                "balance": parse_decimal(amounts.get("invoiceBalance") or amounts.get("due")),
                "payments_total": parse_decimal(amounts.get("paymentsTotal")),
                "deposit_amount": parse_decimal(amounts.get("depositAmount")),
                "discount_amount": parse_decimal(amounts.get("discountAmount")),
                "jobber_web_uri": clip(node.get("jobberWebUri"), 1000),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )
        job_nodes = ((node.get("jobs") or {}).get("nodes")) or []
        jobs = [self._lookup(self._job_cache, Job, item["id"]) for item in job_nodes if item and item.get("id")]
        jobs = [job for job in jobs if job]
        if jobs:
            invoice.jobs.set(jobs)
        self._sync_line_items(invoice, ((node.get("lineItems") or {}).get("nodes")) or [], InvoiceLineItem)
        self._invoice_cache[invoice.external_id] = invoice
        return invoice

    def _upsert_visit(self, node: dict) -> Visit | None:
        if not node or not node.get("id"):
            return None
        job = self._lookup(self._job_cache, Job, node_id(node.get("job")))
        status = node.get("visitStatus") or ""
        title = node.get("title") or ""
        first_clean, deep_clean = classify_title(title)
        if job:
            job_first, job_deep = classify_title(job.title)
            first_clean = first_clean or job_first
            deep_clean = deep_clean or job_deep
        items = ((node.get("lineItems") or {}).get("nodes")) or []
        item_total = sum((line_item_total(item) or Decimal("0")) for item in items)
        price = item_total if item_total else (job.total if job and job.is_one_off else None)
        visit, _ = Visit.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": self._lookup(self._client_cache, Client, node_id(node.get("client"))) or (job.client if job else None),
                "job": job,
                "property": self._lookup(self._property_cache, Property, node_id(node.get("property"))) or (job.property if job else None),
                "invoice": self._lookup(self._invoice_cache, Invoice, node_id(node.get("invoice"))),
                "title": clip(title, 500),
                "visit_status": clip(status, 64),
                "is_complete": is_completed_status(status, node.get("isComplete")),
                "is_cancelled": is_cancelled_status(status),
                "is_recurring": bool(job and job.is_recurring),
                "is_one_off": bool(job and job.is_one_off) or not (job and job.is_recurring),
                "is_first_visit": bool(job and job.first_visit_external_id == node["id"]),
                "is_first_clean": first_clean or bool(job and job.first_visit_external_id == node["id"] and job.is_first_clean),
                "is_deep_clean": deep_clean,
                "all_day": bool(node.get("allDay")),
                "duration_minutes": node.get("duration"),
                "start_at": parse_dt(node.get("startAt")),
                "end_at": parse_dt(node.get("endAt")),
                "completed_at": parse_dt(node.get("completedAt")),
                "completed_by": clip(node.get("completedBy"), 255),
                "instructions": node.get("instructions") or "",
                "line_item_total": item_total if items else None,
                "price_per_visit": price,
                "division": (job.division if job else "") or classify_division(title, job.title if job else ""),
                "service_type": (job.service_type if job and job.is_one_off else "")
                or (classify_service_type(title) if (job and job.is_one_off) or (not job) else ""),
                "team_leader": (job.team_leader if job else None)
                or self._lookup(
                    self._employee_cache,
                    Employee,
                    node_id((((node.get("assignedUsers") or {}).get("nodes")) or [{}])[0]),
                ),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_payload": node,
            },
        )
        assigned = ((node.get("assignedUsers") or {}).get("nodes")) or []
        current_ids = []
        for user in assigned:
            employee = self._lookup(self._employee_cache, Employee, node_id(user))
            if not employee:
                continue
            VisitAssignment.objects.get_or_create(visit=visit, employee=employee)
            current_ids.append(employee.id)
        if current_ids:
            VisitAssignment.objects.filter(visit=visit).exclude(employee_id__in=current_ids).delete()
        parent_job = job or visit.job
        if parent_job:
            self._sync_line_items(parent_job, items, JobLineItem, extra_defaults={"visit": visit})
        return visit

    def _sync_properties(self):
        count = 0
        if self._has_root("properties"):
            for node in self._paginate("properties", PROPERTIES_SELECTION):
                self._upsert_property_from_node(node)
                count += 1
        self._record("properties", count)

    def _sync_jobs(self):
        count = 0
        for node in self._paginate("jobs", JOBS_SELECTION):
            self._upsert_job(node)
            count += 1
        self._record("jobs", count)

    def _sync_line_items(self, parent, nodes, model, extra_defaults=None):
        if not nodes:
            return 0
        count = 0
        extra_defaults = extra_defaults or {}
        for item in nodes:
            if not item or not item.get("id"):
                continue
            defaults = {
                "name": clip(item.get("name"), 255),
                "description": item.get("description") or "",
                "quantity": parse_decimal(item.get("quantity")),
                "unit_price": line_item_unit_price(item),
                "total": line_item_total(item),
                "source_payload": item,
                **extra_defaults,
            }
            if model is JobLineItem:
                defaults["job"] = parent
            else:
                defaults["invoice"] = parent
            model.objects.update_or_create(
                integration=self.integration,
                external_id=item["id"],
                defaults=defaults,
            )
            count += 1
        return count

    def _sync_invoices(self):
        count = 0
        for node in self._paginate("invoices", INVOICES_SELECTION):
            self._upsert_invoice(node)
            count += 1
        self._record("invoices", count)

    def _sync_visits(self):
        count = 0
        def visit_source():
            if self.query_fields and "visits" not in self.query_fields:
                yield from self._visits_via_jobs()
                return
            try:
                yield from self._paginate("visits", VISITS_SELECTION)
            except JobberAPIError as exc:
                logger.warning("Root visits query failed (%s); using jobs.visits", exc)
                yield from self._visits_via_jobs()

        for node in visit_source():
            if self._upsert_visit(node):
                count += 1
        self._record("visits", count)

    def _visits_via_jobs(self):
        query = f"""
        query JobVisits($id: EncodedId!, $first: Int!, $after: String) {{
          job(id: $id) {{
            visits(first: $first, after: $after) {{
              nodes {{ {VISITS_SELECTION} }}
              pageInfo {{ hasNextPage endCursor }}
            }}
          }}
        }}
        """
        for job in Job.objects.filter(integration=self.integration).only("external_id"):
            cursor = None
            while True:
                data = self.client.execute(query, {"id": job.external_id, "first": 25, "after": cursor})
                connection = ((data.get("job") or {}).get("visits")) or {}
                for node in connection.get("nodes") or []:
                    yield node
                page = connection.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                cursor = page.get("endCursor")
                if not cursor:
                    break

    def _sync_tasks(self, *, created_after: str | None = None, created_before: str | None = None, cancellation_only: bool = True):
        """Pull Jobber Tasks. Default: only store Cancelled Visit/Job titles (58k+ tasks otherwise)."""
        count = 0
        cancel_count = 0
        if not self._has_root("tasks"):
            logger.warning("Jobber Query has no `tasks` root field; skipping task sync.")
            self._record("tasks", 0)
            return
        extra = ""
        if created_after or created_before:
            bits = []
            if created_after:
                bits.append(f'after: "{created_after}"')
            if created_before:
                bits.append(f'before: "{created_before}"')
            extra = f"filter: {{ createdAt: {{ {', '.join(bits)} }} }}"
        try:
            for node in self._paginate("tasks", TASKS_SELECTION, extra_args=extra, page_size=100):
                title = node.get("title") or ""
                if cancellation_only and not cancellation_type_from_title(title):
                    continue
                task = self._upsert_task(node)
                if task:
                    count += 1
                    if cancellation_type_from_title(task.title):
                        cancel_count += 1
        except JobberAPIError as exc:
            logger.warning("Task sync failed (%s); continuing without tasks.", exc)
        self._record("tasks", count)
        self._record("cancellation_tasks", cancel_count)

    def _upsert_task(self, node: dict) -> JobberTask | None:
        if not node or not node.get("id"):
            return None
        assignees = ((node.get("assignedUsers") or node.get("assignees") or {}).get("nodes")) or []
        assigned = self._lookup(self._employee_cache, Employee, node_id(assignees[0] if assignees else None))
        is_complete = node.get("isComplete")
        status = "complete" if is_complete is True else ("open" if is_complete is False else "")
        task, _ = JobberTask.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "title": clip(node.get("title"), 500),
                "instructions": node.get("instructions") or "",
                "task_status": clip(status or node.get("status") or node.get("taskStatus") or "", 64),
                "due_at": parse_dt(node.get("startAt") or node.get("dueAt") or node.get("endAt")),
                "client": self._lookup(self._client_cache, Client, node_id(node.get("client"))),
                "job": self._lookup(self._job_cache, Job, node_id(node.get("job"))),
                "assigned_to": assigned,
                "source_created_at": parse_dt(node.get("startAt") or node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("endAt") or node.get("updatedAt")),
                "source_payload": node,
            },
        )
        if cancellation_type_from_title(task.title):
            process_cancellation_task(task, self.integration)
        return task

    def _refresh_client_averages(self):
        from django.db.models import Avg

        for client in Client.objects.filter(integration=self.integration).iterator():
            avg = (
                Visit.objects.filter(client=client, price_per_visit__isnull=False)
                .exclude(is_cancelled=True)
                .aggregate(v=Avg("price_per_visit"))["v"]
            )
            monthly = (
                Job.objects.filter(client=client, is_recurring=True, total__isnull=False)
                .order_by("-source_updated_at", "-id")
                .values_list("total", flat=True)
                .first()
            )
            updates = []
            if avg is not None and client.average_price_per_visit != avg:
                client.average_price_per_visit = avg
                updates.append("average_price_per_visit")
            if monthly is not None and client.monthly_recurring_value != monthly:
                client.monthly_recurring_value = monthly
                updates.append("monthly_recurring_value")
            if updates:
                client.save(update_fields=updates)

    def _backfill_visit_flags(self):
        first_ids = set(
            Job.objects.filter(integration=self.integration)
            .exclude(first_visit_external_id="")
            .values_list("first_visit_external_id", flat=True)
        )
        if first_ids:
            Visit.objects.filter(integration=self.integration, external_id__in=first_ids).update(is_first_visit=True)

    def _seed_kpis(self):
        for spec in DEFAULT_KPIS:
            KpiDefinition.objects.update_or_create(key=spec["key"], defaults=spec)

    def _rebuild_metric_facts(self):
        integration = self.integration
        MetricFact.objects.filter(integration=integration).delete()
        buckets = defaultdict(lambda: Decimal("0"))
        counts = defaultdict(int)

        def add(key, dt, amount=1, dimensions=None):
            if not dt:
                return
            if hasattr(dt, "hour"):
                day = timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()
            else:
                day = dt
            dims = tuple(sorted((dimensions or {}).items()))
            buckets[(key, day, dims)] += Decimal(str(amount))
            counts[(key, day, dims)] += 1

        visits = Visit.objects.filter(integration=integration)
        for visit in visits.iterator():
            event_at = visit.start_at or visit.completed_at or visit.source_created_at
            dims = {
                "job_type": "recurring" if visit.is_recurring else "one_off",
                "division": visit.division or "",
                "service_type": visit.service_type or "",
            }
            add("visits_total", event_at, 1, dims)
            if visit.is_complete:
                add("visits_completed", visit.completed_at or event_at, 1, dims)
            if visit.is_cancelled:
                add("visits_cancelled", event_at, 1, dims)
            if visit.is_one_off:
                add("visits_one_off", event_at, 1, dims)
            if visit.is_recurring:
                add("visits_recurring", event_at, 1, dims)
            if visit.is_first_clean:
                add("first_cleans", event_at, 1, dims)
            if visit.is_deep_clean:
                add("deep_cleans", event_at, 1, dims)
            if visit.price_per_visit is not None and not visit.is_cancelled:
                add("visit_revenue", event_at, visit.price_per_visit, dims)
                add("avg_price_per_visit", event_at, visit.price_per_visit, dims)

        for job in Job.objects.filter(integration=integration, is_recurring=True).iterator():
            add(
                "new_recurring_jobs",
                job.source_created_at or job.start_at,
                1,
                {"division": job.division or ""},
            )

        for invoice in Invoice.objects.filter(integration=integration).iterator():
            amount = invoice.subtotal if invoice.subtotal is not None else invoice.total
            if amount is None:
                continue
            add("invoice_revenue", invoice.issued_date or invoice.source_created_at, amount, None)

        from operations.models import CancellationRecord

        for row in CancellationRecord.objects.filter(integration=integration).iterator():
            key = (
                "cancelled_visit_events"
                if row.cancellation_type == "cancelled_visit"
                else "cancelled_job_events"
            )
            add(key, row.task_date, 1, {"division": row.division or ""})
            if row.value is not None:
                add(f"{key}_value", row.task_date, row.value, {"division": row.division or ""})

        # Convert avg_price_per_visit sums into averages
        for (key, day, dims), value in list(buckets.items()):
            if key == "avg_price_per_visit":
                n = counts[(key, day, dims)] or 1
                buckets[(key, day, dims)] = value / Decimal(n)

        facts = [
            MetricFact(
                integration=integration,
                kpi_key=key,
                bucket_date=day,
                dimensions=dict(dims),
                value=value,
            )
            for (key, day, dims), value in buckets.items()
        ]
        MetricFact.objects.bulk_create(facts, batch_size=1000)
        self._record("metric_facts", len(facts))


def disconnect_jobber(integration: Integration):
    from integrations.oauth import mark_disconnected

    try:
        JobberClient(integration).execute(APP_DISCONNECT)
    except Exception as exc:
        logger.warning("appDisconnect mutation failed: %s", exc)
    mark_disconnected(integration)
