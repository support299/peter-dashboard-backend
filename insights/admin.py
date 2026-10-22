from django.contrib import admin

from insights.models import InsightsSetting, MrrChange, MrrSnapshot, RecurringPlan


@admin.register(RecurringPlan)
class RecurringPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "division", "frequency", "visit_value", "monthly_value", "detected_status", "status_override", "start_date", "end_date")
    list_filter = ("detected_status", "status_override", "division", "frequency")
    search_fields = ("name", "service")
    raw_id_fields = ("job", "client")


@admin.register(MrrChange)
class MrrChangeAdmin(admin.ModelAdmin):
    list_display = ("plan", "changed_on", "change_type", "old_value", "new_value", "monthly_delta")
    list_filter = ("change_type",)
    raw_id_fields = ("plan",)


@admin.register(MrrSnapshot)
class MrrSnapshotAdmin(admin.ModelAdmin):
    list_display = ("plan", "month", "monthly_value", "status", "source")
    list_filter = ("source", "status", "month")
    raw_id_fields = ("plan",)


admin.site.register(InsightsSetting)
