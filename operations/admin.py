from django.contrib import admin

from operations.models import (
    CancellationRecord,
    Client,
    CustomerFeedback,
    DivisionRule,
    Employee,
    GoogleReview,
    Invoice,
    Job,
    JobberTask,
    Property,
    ServiceTypeMapping,
    Visit,
    VisitAssignment,
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "status", "is_admin")
    search_fields = ("full_name", "email", "external_id")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "division", "is_lead", "is_archived", "balance", "average_price_per_visit")
    search_fields = ("name", "email", "company_name", "external_id")
    list_filter = ("is_lead", "is_archived", "is_company", "division")


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "client")
    search_fields = ("name", "street", "city", "external_id")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("job_number", "title", "job_status", "job_type", "division", "service_type", "total", "client")
    list_filter = ("job_status", "job_type", "is_recurring", "is_one_off", "division", "service_type")
    search_fields = ("title", "job_number", "external_id")


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ("title", "visit_status", "start_at", "client", "division", "service_type", "is_complete", "is_cancelled")
    list_filter = ("visit_status", "is_complete", "is_cancelled", "is_recurring", "is_first_clean", "is_deep_clean", "division")
    search_fields = ("title", "external_id")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "invoice_status", "issued_date", "total", "client")
    list_filter = ("invoice_status",)
    search_fields = ("invoice_number", "subject", "external_id")


@admin.register(VisitAssignment)
class VisitAssignmentAdmin(admin.ModelAdmin):
    list_display = ("visit", "employee")


@admin.register(ServiceTypeMapping)
class ServiceTypeMappingAdmin(admin.ModelAdmin):
    list_display = ("keyword", "service_type", "priority", "active")
    list_editable = ("priority", "active")
    search_fields = ("keyword", "service_type")


@admin.register(DivisionRule)
class DivisionRuleAdmin(admin.ModelAdmin):
    list_display = ("keyword", "division", "priority", "active")
    list_editable = ("priority", "active")
    list_filter = ("division", "active")


@admin.register(JobberTask)
class JobberTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "due_at", "client", "task_status")
    search_fields = ("title", "external_id", "client__name")


@admin.register(CancellationRecord)
class CancellationRecordAdmin(admin.ModelAdmin):
    list_display = ("cancellation_type", "client_name", "task_date", "value", "division", "is_lost_client")
    list_filter = ("cancellation_type", "division", "is_lost_client")
    search_fields = ("client_name", "jobber_task_external_id")


@admin.register(CustomerFeedback)
class CustomerFeedbackAdmin(admin.ModelAdmin):
    list_display = ("client_name", "rating", "responded", "received_at", "division")
    list_filter = ("responded", "division")


@admin.register(GoogleReview)
class GoogleReviewAdmin(admin.ModelAdmin):
    list_display = ("author", "rating", "reviewed_at", "replied")
    list_filter = ("replied",)