from django.db import models


class AdminInternalSyncRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    source_generated_at = models.DateTimeField(null=True, blank=True)
    source_from = models.DateField(null=True, blank=True)
    source_to = models.DateField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AdminInternalSyncRun({self.status})"


class HubEmployee(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=64, blank=True, default="")
    role = models.CharField(max_length=64, blank=True, default="", db_index=True)
    status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    position = models.CharField(max_length=120, blank=True, default="", db_index=True)
    sectors = models.JSONField(default=list, blank=True)
    work_days = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    hire_date = models.DateField(null=True, blank=True)
    available_vacation_days = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vacation_balance_reset_on = models.DateField(null=True, blank=True)
    jobber_id = models.CharField(max_length=160, blank=True, default="")
    ghl_id = models.CharField(max_length=160, blank=True, default="")
    rates = models.JSONField(default=dict, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    # Performance proxy metrics (from employee_performance section)
    lock_in_bonus_count = models.PositiveIntegerField(default=0)
    lock_in_bonus_amount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    lock_in_bonus_amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    visits_count = models.PositiveIntegerField(default=0)
    leave_requests_count = models.PositiveIntegerField(default=0)
    absences_count = models.PositiveIntegerField(default=0)
    vacations_count = models.PositiveIntegerField(default=0)
    late_arrivals_count = models.PositiveIntegerField(default=0)
    attendance_days = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    performance_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    feedback_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["role", "status"]),
            models.Index(fields=["position", "status"]),
        ]

    def __str__(self):
        return self.name or self.email or self.external_id


class HubLeaveRequest(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    employee = models.ForeignKey(
        HubEmployee, on_delete=models.SET_NULL, null=True, blank=True, related_name="leave_requests"
    )
    employee_external_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    employee_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    leave_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    leave_type_raw = models.CharField(max_length=120, blank=True, default="")
    start_date = models.DateField(null=True, blank=True, db_index=True)
    end_date = models.DateField(null=True, blank=True)
    weekday_count = models.PositiveIntegerField(null=True, blank=True)
    vacation_days_deducted = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    jobber_task_id = models.CharField(max_length=160, blank=True, default="")
    jobber_sync_error = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [
            models.Index(fields=["status", "leave_type"]),
            models.Index(fields=["start_date", "end_date"]),
        ]

    def __str__(self):
        return f"{self.leave_type} · {self.employee_name or self.external_id}"


class HubPendingLockIn(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    quote_id = models.CharField(max_length=160, blank=True, default="")
    client_name = models.CharField(max_length=255, blank=True, default="")
    client_jobber_id = models.CharField(max_length=160, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    locked_in = models.BooleanField(default=False, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    frequency = models.CharField(max_length=64, blank=True, default="")
    quote_sent_at = models.DateTimeField(null=True, blank=True)
    quote_approved_at = models.DateTimeField(null=True, blank=True)
    eligibility_expires_at = models.DateTimeField(null=True, blank=True)
    expected_first_visit_at = models.DateTimeField(null=True, blank=True)
    first_recurring_visit_id = models.CharField(max_length=160, blank=True, default="")
    first_recurring_visit_at = models.DateTimeField(null=True, blank=True)
    expired_reason = models.TextField(blank=True, default="")
    technician_ids = models.JSONField(default=list, blank=True)
    technician_names = models.JSONField(default=list, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-quote_approved_at", "-id"]

    def __str__(self):
        return self.client_name or self.quote_id or self.external_id


class HubBonus(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    bonus_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    position_snapshot = models.CharField(max_length=120, blank=True, default="")
    bonus_confirmed = models.BooleanField(default=False)
    bonus_paid = models.BooleanField(default=False, db_index=True)
    paid_date = models.DateField(null=True, blank=True)
    confirmed_date = models.DateField(null=True, blank=True)
    in_process_date = models.DateTimeField(null=True, blank=True)
    payroll_reference = models.CharField(max_length=160, blank=True, default="")
    employee = models.ForeignKey(
        HubEmployee, on_delete=models.SET_NULL, null=True, blank=True, related_name="bonuses"
    )
    employee_external_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    employee_name = models.CharField(max_length=255, blank=True, default="")
    employee_email = models.CharField(max_length=255, blank=True, default="")
    employee_position = models.CharField(max_length=120, blank=True, default="")
    pending = models.ForeignKey(
        HubPendingLockIn, on_delete=models.SET_NULL, null=True, blank=True, related_name="bonuses"
    )
    pending_external_id = models.CharField(max_length=64, blank=True, default="")
    client_name = models.CharField(max_length=255, blank=True, default="")
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-source_created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "bonus_type"]),
            models.Index(fields=["bonus_paid", "status"]),
        ]

    def __str__(self):
        return f"{self.bonus_type} · {self.employee_name or self.external_id}"


class HubVisit(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    jobber_visit_id = models.CharField(max_length=160, blank=True, default="")
    title = models.CharField(max_length=500, blank=True, default="")
    client_name = models.CharField(max_length=255, blank=True, default="", db_index=True)
    client_jobber_id = models.CharField(max_length=160, blank=True, default="")
    job_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    start_at = models.DateTimeField(null=True, blank=True, db_index=True)
    technician_ids = models.JSONField(default=list, blank=True)
    technician_names = models.JSONField(default=list, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_at", "-id"]

    def __str__(self):
        return self.title or self.external_id


class HubAlert(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    message = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return (self.message or self.external_id)[:80]
