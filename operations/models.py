from django.db import models

from integrations.models import Integration


class SourceRecord(models.Model):
    integration = models.ForeignKey(Integration, on_delete=models.CASCADE)
    external_id = models.CharField(max_length=160, db_index=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)
    source_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True

    def unique_constraint(name):
        return models.UniqueConstraint(fields=["integration", "external_id"], name=name)


class Employee(SourceRecord):
    first_name = models.CharField(max_length=255, blank=True, default="")
    last_name = models.CharField(max_length=255, blank=True, default="")
    full_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="")
    is_admin = models.BooleanField(default=False)
    is_owner = models.BooleanField(default=False)
    available_for_scheduling = models.BooleanField(default=True)

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_employee_ext")]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["full_name"])]

    def __str__(self):
        return self.full_name or self.email or self.external_id


class Client(SourceRecord):
    name = models.CharField(max_length=255, blank=True, default="")
    first_name = models.CharField(max_length=255, blank=True, default="")
    last_name = models.CharField(max_length=255, blank=True, default="")
    company_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=255, blank=True, default="")
    is_company = models.BooleanField(default=False)
    is_lead = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    billing_city = models.CharField(max_length=255, blank=True, default="")
    billing_province = models.CharField(max_length=255, blank=True, default="")
    billing_country = models.CharField(max_length=120, blank=True, default="")
    billing_postal_code = models.CharField(max_length=64, blank=True, default="")
    billing_street = models.CharField(max_length=1024, blank=True, default="")
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    average_price_per_visit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    monthly_recurring_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    jobber_web_uri = models.CharField(max_length=1000, blank=True, default="")

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_client_ext")]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["is_lead", "is_archived"]),
            models.Index(fields=["email"]),
            models.Index(fields=["division"]),
        ]

    def __str__(self):
        return self.name or self.company_name or self.external_id


class Property(SourceRecord):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="properties")
    name = models.CharField(max_length=255, blank=True, default="")
    street = models.CharField(max_length=1024, blank=True, default="")
    city = models.CharField(max_length=255, blank=True, default="")
    province = models.CharField(max_length=255, blank=True, default="")
    postal_code = models.CharField(max_length=64, blank=True, default="")
    country = models.CharField(max_length=120, blank=True, default="")
    is_billing_address = models.BooleanField(null=True, blank=True)

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_property_ext")]
        indexes = [models.Index(fields=["city"]), models.Index(fields=["client"])]


class Job(SourceRecord):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs")
    property = models.ForeignKey(Property, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs")
    salesperson = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="sold_jobs"
    )
    job_number = models.IntegerField(null=True, blank=True)
    title = models.CharField(max_length=500, blank=True, default="")
    job_status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    job_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    billing_type = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=255, blank=True, default="")
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    invoiced_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    uninvoiced_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    instructions = models.TextField(blank=True, default="")
    first_visit_external_id = models.CharField(max_length=160, blank=True, default="")
    first_visit_at = models.DateTimeField(null=True, blank=True)
    is_recurring = models.BooleanField(default=False, db_index=True)
    is_one_off = models.BooleanField(default=False, db_index=True)
    is_first_clean = models.BooleanField(default=False)
    is_deep_clean = models.BooleanField(default=False)
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    service_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    team_leader = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="led_jobs"
    )
    monthly_recurring_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    jobber_web_uri = models.CharField(max_length=1000, blank=True, default="")

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_job_ext")]
        indexes = [
            models.Index(fields=["start_at"]),
            models.Index(fields=["completed_at"]),
            models.Index(fields=["job_status", "job_type"]),
            models.Index(fields=["source_created_at"]),
            models.Index(fields=["division", "service_type"]),
        ]

    def __str__(self):
        return f"#{self.job_number or '?'} {self.title}".strip()


class Invoice(SourceRecord):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    jobs = models.ManyToManyField(Job, blank=True, related_name="invoices")
    invoice_number = models.CharField(max_length=64, blank=True, default="")
    invoice_status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    subject = models.CharField(max_length=500, blank=True, default="")
    issued_date = models.DateTimeField(null=True, blank=True)
    due_date = models.DateTimeField(null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payments_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    jobber_web_uri = models.CharField(max_length=1000, blank=True, default="")

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_invoice_ext")]
        indexes = [
            models.Index(fields=["issued_date"]),
            models.Index(fields=["invoice_number"]),
        ]


class Visit(SourceRecord):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="visits")
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="visits")
    property = models.ForeignKey(Property, on_delete=models.SET_NULL, null=True, blank=True, related_name="visits")
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="visits")
    assigned_employees = models.ManyToManyField(Employee, through="VisitAssignment", related_name="visits", blank=True)
    title = models.CharField(max_length=500, blank=True, default="")
    visit_status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_complete = models.BooleanField(default=False, db_index=True)
    is_cancelled = models.BooleanField(default=False, db_index=True)
    is_recurring = models.BooleanField(default=False, db_index=True)
    is_one_off = models.BooleanField(default=False, db_index=True)
    is_first_visit = models.BooleanField(default=False, db_index=True)
    is_first_clean = models.BooleanField(default=False, db_index=True)
    is_deep_clean = models.BooleanField(default=False, db_index=True)
    all_day = models.BooleanField(default=False)
    duration_minutes = models.IntegerField(null=True, blank=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.CharField(max_length=255, blank=True, default="")
    instructions = models.TextField(blank=True, default="")
    line_item_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    price_per_visit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    service_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    team_leader = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="led_visits"
    )

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_visit_ext")]
        indexes = [
            models.Index(fields=["start_at"]),
            models.Index(fields=["completed_at"]),
            models.Index(fields=["visit_status", "start_at"]),
            models.Index(fields=["job", "start_at"]),
            models.Index(fields=["division", "service_type"]),
        ]

    def __str__(self):
        return self.title or self.external_id


class VisitAssignment(models.Model):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="assignments")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="assignments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["visit", "employee"], name="uniq_visit_employee")]


class JobLineItem(SourceRecord):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="line_items")
    visit = models.ForeignKey(Visit, on_delete=models.SET_NULL, null=True, blank=True, related_name="line_items")
    name = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_job_line_item_ext")]
        indexes = [models.Index(fields=["name"])]


class InvoiceLineItem(SourceRecord):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    name = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_invoice_line_item_ext")]


DIVISION_CHOICES = [
    ("residential", "Residential"),
    ("commercial", "Commercial"),
    ("specialized", "Specialized Services"),
    ("rentals", "Rentals"),
]

CANCELLATION_VISIT = "cancelled_visit"
CANCELLATION_JOB = "cancelled_job"
CANCELLATION_TYPE_CHOICES = [
    (CANCELLATION_VISIT, "Cancelled Visit"),
    (CANCELLATION_JOB, "Cancelled Job"),
]


class DivisionRule(models.Model):
    """Editable keyword → division mapping (Admin)."""

    keyword = models.CharField(max_length=120)
    division = models.CharField(max_length=64, choices=DIVISION_CHOICES, db_index=True)
    priority = models.IntegerField(default=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "keyword"]

    def __str__(self):
        return f"{self.keyword} → {self.division}"


class ServiceTypeMapping(models.Model):
    """Editable Job Title keyword → one-off service type (Admin)."""

    keyword = models.CharField(max_length=120)
    service_type = models.CharField(max_length=120, db_index=True)
    priority = models.IntegerField(default=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "keyword"]

    def __str__(self):
        return f"{self.keyword} → {self.service_type}"


class JobberTask(SourceRecord):
    """Jobber task used for cancellation tracking workflows."""

    title = models.CharField(max_length=500, blank=True, default="")
    instructions = models.TextField(blank=True, default="")
    task_status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    due_at = models.DateTimeField(null=True, blank=True)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    assigned_to = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )

    class Meta:
        constraints = [SourceRecord.unique_constraint("uniq_jobber_task_ext")]
        indexes = [
            models.Index(fields=["title"]),
            models.Index(fields=["due_at"]),
            models.Index(fields=["task_status"]),
        ]

    def __str__(self):
        return self.title or self.external_id


class CancellationRecord(models.Model):
    """Cancelled Visit or Cancelled Job — one row per Jobber Task ID."""

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="cancellations")
    jobber_task = models.OneToOneField(
        JobberTask, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancellation"
    )
    jobber_task_external_id = models.CharField(max_length=160, db_index=True)
    cancellation_type = models.CharField(max_length=32, choices=CANCELLATION_TYPE_CHOICES, db_index=True)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancellations")
    client_name = models.CharField(max_length=255, blank=True, default="")
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancellations")
    task_date = models.DateField(null=True, blank=True, db_index=True)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_lost_client = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    source_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "jobber_task_external_id"],
                name="uniq_cancellation_task",
            )
        ]
        indexes = [
            models.Index(fields=["cancellation_type", "task_date"]),
            models.Index(fields=["division", "cancellation_type"]),
        ]
        ordering = ["-task_date", "-id"]

    def __str__(self):
        return f"{self.cancellation_type} {self.client_name or self.jobber_task_external_id}"


class CustomerFeedback(models.Model):
    """Customer experience — ratings & feedback (manual or future sync)."""

    integration = models.ForeignKey(
        Integration, on_delete=models.CASCADE, null=True, blank=True, related_name="feedback"
    )
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="feedback")
    client_name = models.CharField(max_length=255, blank=True, default="")
    rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    feedback_text = models.TextField(blank=True, default="")
    responded = models.BooleanField(default=False)
    responded_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True, default="manual")
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    received_at = models.DateField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-id"]


class GoogleReview(models.Model):
    author = models.CharField(max_length=255, blank=True, default="")
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    review_text = models.TextField(blank=True, default="")
    reviewed_at = models.DateField(null=True, blank=True, db_index=True)
    reply_text = models.TextField(blank=True, default="")
    replied = models.BooleanField(default=False)
    external_id = models.CharField(max_length=160, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-reviewed_at", "-id"]
