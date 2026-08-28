from django.contrib import admin

from analytics.hub_models import (
    AdminInternalSyncRun,
    HubAlert,
    HubBonus,
    HubEmployee,
    HubLeaveRequest,
    HubPendingLockIn,
    HubVisit,
)
from analytics.models import Dashboard, KpiDefinition, MetricFact, Widget
from analytics.pricing_models import (
    PricingAddon,
    PricingBundle,
    PricingCoupon,
    PricingLocation,
    PricingPackage,
    PricingService,
    PricingSubmission,
    PricingSyncRun,
)


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active")


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("title", "dashboard", "widget_type", "sort_order")


@admin.register(KpiDefinition)
class KpiDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "category", "unit", "source_model", "is_active")
    list_filter = ("category", "is_active")


@admin.register(MetricFact)
class MetricFactAdmin(admin.ModelAdmin):
    list_display = ("kpi_key", "bucket_date", "value", "integration")
    list_filter = ("kpi_key",)


@admin.register(AdminInternalSyncRun)
class AdminInternalSyncRunAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "started_at", "finished_at", "source_generated_at")
    list_filter = ("status",)


@admin.register(HubEmployee)
class HubEmployeeAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "status", "position", "email")
    list_filter = ("role", "status", "position")
    search_fields = ("name", "email", "external_id")


@admin.register(HubLeaveRequest)
class HubLeaveRequestAdmin(admin.ModelAdmin):
    list_display = ("employee_name", "leave_type", "status", "start_date", "end_date")
    list_filter = ("status", "leave_type")
    search_fields = ("employee_name", "external_id")


@admin.register(HubBonus)
class HubBonusAdmin(admin.ModelAdmin):
    list_display = ("employee_name", "bonus_type", "status", "amount", "bonus_paid")
    list_filter = ("status", "bonus_type", "bonus_paid")


@admin.register(HubPendingLockIn)
class HubPendingLockInAdmin(admin.ModelAdmin):
    list_display = ("client_name", "status", "locked_in", "quote_id")
    list_filter = ("status", "locked_in")


@admin.register(HubVisit)
class HubVisitAdmin(admin.ModelAdmin):
    list_display = ("title", "client_name", "job_type", "start_at")
    list_filter = ("job_type",)


@admin.register(HubAlert)
class HubAlertAdmin(admin.ModelAdmin):
    list_display = ("message", "active", "sort_order")
    list_filter = ("active",)


@admin.register(PricingSyncRun)
class PricingSyncRunAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "started_at", "finished_at", "source_generated_at")
    list_filter = ("status",)


@admin.register(PricingSubmission)
class PricingSubmissionAdmin(admin.ModelAdmin):
    list_display = ("customer_email", "status", "property_type", "location_name", "final_total", "source_created_at")
    list_filter = ("status", "property_type", "coupon_applied")
    search_fields = ("customer_email", "customer_first_name", "customer_last_name", "external_id")


@admin.register(PricingService)
class PricingServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "is_residential", "is_commercial")
    list_filter = ("is_active",)


@admin.register(PricingPackage)
class PricingPackageAdmin(admin.ModelAdmin):
    list_display = ("name", "service_name", "base_price", "is_active")
    list_filter = ("is_active", "service_name")


@admin.register(PricingLocation)
class PricingLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "trip_surcharge", "is_active")
    list_filter = ("is_active",)


@admin.register(PricingCoupon)
class PricingCouponAdmin(admin.ModelAdmin):
    list_display = ("code", "used_count", "is_active")
    list_filter = ("is_active",)


@admin.register(PricingAddon)
class PricingAddonAdmin(admin.ModelAdmin):
    list_display = ("name", "base_price", "is_global")


@admin.register(PricingBundle)
class PricingBundleAdmin(admin.ModelAdmin):
    list_display = ("name", "discount_type", "is_active")
    list_filter = ("is_active",)
