"""Financials: expenses, payroll, recurring expenses, balances and the monthly review.

Everything that costs money ends up as an Expense row (pay runs and recurring
expenses create them too), so reports read one ledger. Monthly totals are the
one exception: when a total is entered for a category and month, it replaces
the individual expenses of that category and month in reports (see ledger.py).

`source` and `external_id` exist so bank, credit card, PayEvo or accounting
imports can be added later without changing the tables.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from operations.models import DIVISION_CHOICES

GROUP_COGS = "cogs"
GROUP_OPEX = "opex"
GROUP_VEHICLES = "vehicles"
GROUP_OTHER = "other"
GROUP_CHOICES = [
    (GROUP_COGS, "Cost of sales (COGS)"),
    (GROUP_OPEX, "Operating expenses"),
    (GROUP_VEHICLES, "Vehicles"),
    (GROUP_OTHER, "Other financial transactions"),
]

DIVISION_SHARED = "shared"
EXPENSE_DIVISION_CHOICES = [*DIVISION_CHOICES, (DIVISION_SHARED, "Shared")]

PAYMENT_METHOD_CHOICES = [
    ("credit_card", "Credit card"),
    ("debit", "Debit card"),
    ("bank_transfer", "Bank transfer"),
    ("pre_authorized", "Pre-authorized debit"),
    ("e_transfer", "Interac e-Transfer"),
    ("cheque", "Cheque"),
    ("cash", "Cash"),
    ("payroll", "Payroll (PayEvo)"),
    ("other", "Other"),
]

STATUS_PAID = "paid"
STATUS_UNPAID = "unpaid"
STATUS_CHOICES = [(STATUS_PAID, "Paid"), (STATUS_UNPAID, "Unpaid")]

SOURCE_MANUAL = "manual"
SOURCE_IMPORT = "import"
SOURCE_RECURRING = "recurring"
SOURCE_PAYROLL = "payroll"
SOURCE_HUB = "hub"
SOURCE_CHOICES = [
    (SOURCE_MANUAL, "Entered by hand"),
    (SOURCE_IMPORT, "CSV / Excel import"),
    (SOURCE_RECURRING, "Recurring expense"),
    (SOURCE_PAYROLL, "Pay run"),
    (SOURCE_HUB, "Contractor pay (Hub)"),
    # Reserved for future connections.
    ("bank", "Bank feed"),
    ("card", "Credit card feed"),
    ("payevo", "PayEvo"),
    ("accounting", "Accounting software"),
]

FREQUENCY_CHOICES = [
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 weeks"),
    ("monthly", "Monthly"),
    ("quarterly", "Quarterly"),
    ("yearly", "Yearly"),
]

ACCOUNT_BANK = "bank"
ACCOUNT_CARD = "credit_card"
ACCOUNT_LOAN = "loan"
ACCOUNT_LINE = "line_of_credit"
ACCOUNT_KIND_CHOICES = [
    (ACCOUNT_BANK, "Bank account"),
    (ACCOUNT_CARD, "Credit card"),
    (ACCOUNT_LOAN, "Loan"),
    (ACCOUNT_LINE, "Line of credit"),
]
DEBT_KINDS = {ACCOUNT_CARD, ACCOUNT_LOAN, ACCOUNT_LINE}

ROLE_ENTRY = "entry"
ROLE_FULL = "full"
ROLE_ADMIN = "admin"
ROLE_CHOICES = [
    (ROLE_ENTRY, "Enter expenses only"),
    (ROLE_FULL, "View all financials"),
    (ROLE_ADMIN, "Financials admin (also manages access and categories)"),
]

REVIEW_CHECKS = [
    ("bank", "Bank transactions reviewed"),
    ("credit_cards", "Credit card statements reviewed"),
    ("payroll", "Payroll entered for every pay run"),
    ("receipts", "Receipts attached"),
    ("expenses", "Expenses complete and categorized"),
]

MONEY = {"max_digits": 14, "decimal_places": 2}


class FinanceAccess(models.Model):
    """Who may use Financials. Superusers always have admin access."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="finance_access")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_ENTRY)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "finance access"
        verbose_name_plural = "finance access"

    def __str__(self):
        return f"{self.user} · {self.get_role_display()}"


class Category(models.Model):
    """Category (parent empty) or subcategory (parent set). Subcategories share their parent's group."""

    group = models.CharField(max_length=16, choices=GROUP_CHOICES, db_index=True)
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
    # Payroll KPI = categories marked payroll. Labour cost % = categories marked labour.
    counts_as_payroll = models.BooleanField(default=False)
    counts_as_labour = models.BooleanField(default=False)
    # "Other financial transactions" stay out of profit, except costs like loan interest.
    in_profit_and_loss = models.BooleanField(default=True)
    # Stable key for categories the app relies on (payroll, loan interest…). Empty for user-made ones.
    system_key = models.CharField(max_length=40, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["group", "sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return f"{self.parent.name} › {self.name}" if self.parent_id else self.name

    def save(self, *args, **kwargs):
        if self.parent_id:
            self.group = self.parent.group
        super().save(*args, **kwargs)


class ImportBatch(models.Model):
    """One CSV / Excel upload, so a whole import can be reviewed or undone."""

    file_name = models.CharField(max_length=255)
    rows_imported = models.PositiveIntegerField(default=0)
    rows_skipped = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"

    def __str__(self):
        return self.file_name


class ReceiptFields(models.Model):
    """S3 receipt / invoice attachment (key only; files are served through short-lived links)."""

    receipt_key = models.CharField(max_length=500, blank=True, default="")
    receipt_name = models.CharField(max_length=255, blank=True, default="")
    receipt_type = models.CharField(max_length=100, blank=True, default="")
    receipt_size = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True

    @property
    def has_receipt(self) -> bool:
        return bool(self.receipt_key)


class PayRun(ReceiptFields):
    """One PayEvo pay run, entered as two totals. Counted in the month of the pay date."""

    pay_date = models.DateField(db_index=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    # Total employer cost: gross wages + employer contributions + vacation pay.
    cleaning_amount = models.DecimalField(**MONEY, default=0)
    office_amount = models.DecimalField(**MONEY, default=0)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-pay_date", "-id"]

    def __str__(self):
        return f"Pay run {self.pay_date}"

    @property
    def total(self) -> Decimal:
        return (self.cleaning_amount or 0) + (self.office_amount or 0)


class RecurringExpense(models.Model):
    """Rent, insurance, software… Creates an expense on each due date, waiting to be verified."""

    name = models.CharField(max_length=200)
    vendor = models.CharField(max_length=200, blank=True, default="")
    description = models.CharField(max_length=500, blank=True, default="")
    amount = models.DecimalField(**MONEY)
    gst = models.DecimalField(**MONEY, default=0)
    qst = models.DecimalField(**MONEY, default=0)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="+")
    subcategory = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    division = models.CharField(max_length=32, choices=EXPENSE_DIVISION_CHOICES, default=DIVISION_SHARED)
    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHOD_CHOICES, blank=True, default="")
    frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES, default="monthly")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    # Due dates marked "not charged this time" (ISO dates), so they aren't created again.
    skipped_dates = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @property
    def total(self) -> Decimal:
        return (self.amount or 0) + (self.gst or 0) + (self.qst or 0)


class Expense(ReceiptFields):
    date = models.DateField(db_index=True)
    vendor = models.CharField(max_length=200, blank=True, default="")
    description = models.CharField(max_length=500, blank=True, default="")
    amount = models.DecimalField(**MONEY, help_text="Before tax")
    gst = models.DecimalField(**MONEY, default=0)
    qst = models.DecimalField(**MONEY, default=0)
    total = models.DecimalField(**MONEY, default=0)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="expenses")
    subcategory = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True, related_name="sub_expenses")
    division = models.CharField(max_length=32, choices=EXPENSE_DIVISION_CHOICES, default=DIVISION_SHARED, db_index=True)
    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHOD_CHOICES, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PAID)
    notes = models.TextField(blank=True, default="")

    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, db_index=True)
    external_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses")
    pay_run = models.ForeignKey(PayRun, on_delete=models.CASCADE, null=True, blank=True, related_name="expenses")
    recurring = models.ForeignKey(RecurringExpense, on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses")
    due_date = models.DateField(null=True, blank=True)
    # Created by a recurring expense and not yet checked against the real charge: left out of totals.
    pending_verification = models.BooleanField(default=False, db_index=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["date", "category"])]
        constraints = [
            models.UniqueConstraint(fields=["recurring", "due_date"], condition=Q(recurring__isnull=False), name="uniq_recurring_due_date"),
        ]

    def __str__(self):
        return f"{self.date} {self.vendor or self.description} {self.total}"

    def save(self, *args, **kwargs):
        self.total = (self.amount or 0) + (self.gst or 0) + (self.qst or 0)
        super().save(*args, **kwargs)


class MonthlyTotal(models.Model):
    """A month's total for a category (e.g. from a statement). Replaces that category's individual expenses for the month in reports."""

    month = models.DateField(db_index=True)  # first day of month
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="monthly_totals")
    division = models.CharField(max_length=32, choices=EXPENSE_DIVISION_CHOICES, default=DIVISION_SHARED)
    amount = models.DecimalField(**MONEY, help_text="Before tax")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month", "category__group", "category__name"]
        constraints = [models.UniqueConstraint(fields=["month", "category", "division"], name="uniq_monthly_total")]

    def __str__(self):
        return f"{self.month:%b %Y} {self.category} {self.amount}"


class FinancialAccount(models.Model):
    """Bank account, credit card, loan or line of credit whose balance is updated by hand."""

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=24, choices=ACCOUNT_KIND_CHOICES)
    institution = models.CharField(max_length=120, blank=True, default="")
    credit_limit = models.DecimalField(**MONEY, null=True, blank=True)
    interest_rate = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True, help_text="Annual %")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    external_id = models.CharField(max_length=255, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def is_debt(self) -> bool:
        return self.kind in DEBT_KINDS


class BalanceEntry(models.Model):
    """Balance on a date. Debts are entered as positive amounts owed."""

    account = models.ForeignKey(FinancialAccount, on_delete=models.CASCADE, related_name="balances")
    date = models.DateField(db_index=True)
    balance = models.DecimalField(**MONEY)
    notes = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [models.UniqueConstraint(fields=["account", "date"], name="uniq_balance_account_date")]
        verbose_name_plural = "balance entries"


class MonthReview(models.Model):
    """Monthly reconciliation checklist."""

    STATUS_OPEN = "open"
    STATUS_REVIEWED = "reviewed"
    STATUS_CHOICES = [(STATUS_OPEN, "Open"), (STATUS_REVIEWED, "Reviewed")]

    month = models.DateField(unique=True)  # first day of month
    checks = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month"]

    def __str__(self):
        return f"{self.month:%B %Y} · {self.get_status_display()}"
