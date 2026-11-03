from django.contrib import admin

from financials.models import (
    BalanceEntry,
    Category,
    Expense,
    FinanceAccess,
    FinancialAccount,
    ImportBatch,
    MonthlyTotal,
    MonthReview,
    PayRun,
    RecurringExpense,
)


@admin.register(FinanceAccess)
class FinanceAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "granted_by", "updated_at")
    list_filter = ("role",)
    raw_id_fields = ("user", "granted_by")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "parent", "counts_as_payroll", "counts_as_labour", "in_profit_and_loss", "is_active", "system_key")
    list_filter = ("group", "is_active")
    search_fields = ("name",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("date", "vendor", "description", "amount", "total", "category", "division", "status", "source", "pending_verification")
    list_filter = ("source", "status", "division", "pending_verification", "category__group")
    search_fields = ("vendor", "description", "notes")
    date_hierarchy = "date"
    raw_id_fields = ("pay_run", "recurring", "import_batch", "created_by", "updated_by")


@admin.register(PayRun)
class PayRunAdmin(admin.ModelAdmin):
    list_display = ("pay_date", "period_start", "period_end", "cleaning_amount", "office_amount")
    date_hierarchy = "pay_date"


@admin.register(RecurringExpense)
class RecurringExpenseAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "frequency", "category", "start_date", "end_date", "is_active")
    list_filter = ("frequency", "is_active")


@admin.register(MonthlyTotal)
class MonthlyTotalAdmin(admin.ModelAdmin):
    list_display = ("month", "category", "division", "amount")
    list_filter = ("month",)


class BalanceInline(admin.TabularInline):
    model = BalanceEntry
    extra = 0


@admin.register(FinancialAccount)
class FinancialAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "institution", "is_active")
    list_filter = ("kind", "is_active")
    inlines = [BalanceInline]


@admin.register(MonthReview)
class MonthReviewAdmin(admin.ModelAdmin):
    list_display = ("month", "status", "reviewed_by", "reviewed_at")


admin.site.register(ImportBatch)
