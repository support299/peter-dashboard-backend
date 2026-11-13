"""
The numbers each Master page shows, in one place. The page views build their cards from these lists,
and page access (accounts.access) uses them to decide which record lists and trend charts someone
limited to some pages may open. Keep a page's list here, not inline in its view.
"""

from __future__ import annotations

from insights.metrics import CHANGE_WATCH, KPIS, SCORECARD

# ——— Revenue ———
REVENUE_CARDS = ["revenue", "invoiced_revenue", "discounts", "completed_visit_value", "payments_received", "avg_revenue_per_visit"]
REVENUE_PROFITABILITY = [
    "cogs", "cogs_pct", "gross_profit", "gross_margin", "operating_expenses", "opex_pct",
    "vehicle_expenses", "operating_profit", "net_profit", "net_margin", "payroll", "payroll_pct",
]
REVENUE_CONCENTRATION = ["top5_share", "top10_share"]
REVENUE_RECEIVABLES = ["ar_balance", "ar_overdue"]
REVENUE_COLLECTIONS = ["payments_received", "days_to_payment"]

# ——— Clients & growth ———
CLIENTS_GROWTH = ["active_clients_start", "gained_clients", "lost_clients", "active_clients", "new_clients", "returning_clients", "net_client_growth"]
CLIENTS_RECURRING = ["active_recurring_clients", "paused_clients", "recurring_retention", "new_recurring_jobs", "cancelled_recurring_jobs", "net_recurring_jobs"]
CLIENTS_SALES = ["leads", "new_prospects", "lead_conversion", "days_to_first_clean", "avg_new_client_value"]

# ——— MRR ———
MRR_CARDS = [
    "mrr_starting", "mrr_new", "mrr_expansion", "mrr_reactivated", "mrr_contraction", "mrr_churned",
    "mrr_paused_net", "mrr_net_new", "mrr", "mrr_paused", "active_recurring_clients", "paused_clients",
]
# The waterfall bars open lists by their own key.
MRR_WATERFALL = ["mrr_starting", "mrr_new", "mrr_reactivated", "mrr_expansion", "mrr_contraction", "mrr_paused", "mrr_churned", "mrr_untracked", "mrr"]

# ——— Cancellations ———
CANCELLATION_CARDS = ["cancelled_visits", "cancelled_visits_value", "cancellations_internal", "cancelled_jobs", "cancelled_jobs_value", "service_cancellation_forms"]

# ——— Quality ———
QUALITY_REDOS = ["touch_ups", "touch_up_rate", "touch_up_hours", "touch_up_cost"]
QUALITY_FEEDBACK = ["feedback_score", "feedback_responses", "feedback_participation"]
QUALITY_COMPLAINTS = ["complaints", "complaint_rate", "complaints_open", "complaint_resolution_days"]
QUALITY_GOOGLE = ["google_rating", "google_reviews", "google_new_reviews", "google_five_star_new"]

# ——— Operations ———
OPERATIONS_WORK = ["visits_completed", "labour_hours", "revenue_per_labour_hour", "overtime_hours", "drive_time_hours", "drive_time_cost"]
OPERATIONS_PEOPLE = ["absence_days", "late_arrivals", "cancellations_internal"]
OPERATIONS_PAYROLL = ["payroll", "payroll_pct"]

# ——— Sales & leads ———
SALES_FUNNEL = ["leads", "new_prospects", "leads_quoted", "leads_accepted", "leads_first_clean", "leads_recurring"]
SALES_QUOTES = ["calculator_estimates", "jobber_quotes"]
SALES_RATES = ["lead_conversion", "days_to_first_clean"]
SALES_MARKETING = ["marketing_spend", "cost_per_lead", "cost_per_new_client", "new_clients"]

# ——— Trend charts on each page (the frontend asks for these) ———
PROFIT_SERIES = ["revenue", "gross_profit", "net_profit"]
TRENDS = {
    "master:overview": ["revenue", "mrr", "active_recurring_clients", "visits_completed", "avg_revenue_per_visit", "new_clients", "gross_profit", "net_profit", "payroll_pct", *PROFIT_SERIES],
    "master:revenue": ["revenue", "avg_revenue_per_visit", "gross_profit", "net_profit", *PROFIT_SERIES],
    "master:clients": ["active_recurring_clients", "new_clients", "new_recurring_jobs", "cancelled_recurring_jobs"],
    "master:mrr": ["mrr", "active_recurring_clients"],
    "master:cancellations": ["cancelled_visits_value"],
    "master:sales": ["leads", "lead_conversion", "payments_received"],
}


def _manager_keys():
    from insights.manager_views import CLIENT_EXPERIENCE, HEADLINE, RECURRING, SALES, SCHEDULING

    return HEADLINE + SALES + RECURRING + SCHEDULING + CLIENT_EXPERIENCE


def page_kpis(page: str) -> set[str]:
    """Every KPI a page shows (cards, what changed, trend charts)."""
    keys = {
        "master:overview": SCORECARD + CHANGE_WATCH,
        "master:revenue": REVENUE_CARDS + REVENUE_PROFITABILITY + REVENUE_CONCENTRATION + REVENUE_RECEIVABLES + REVENUE_COLLECTIONS,
        "master:clients": CLIENTS_GROWTH + CLIENTS_RECURRING + CLIENTS_SALES,
        "master:mrr": MRR_CARDS,
        "master:cancellations": CANCELLATION_CARDS,
        "master:quality": QUALITY_REDOS + QUALITY_FEEDBACK + QUALITY_COMPLAINTS + QUALITY_GOOGLE,
        "master:operations": OPERATIONS_WORK + OPERATIONS_PEOPLE + OPERATIONS_PAYROLL,
        "master:sales": SALES_FUNNEL + SALES_QUOTES + SALES_RATES + SALES_MARKETING,
        "master:manager": _manager_keys() if page == "master:manager" else [],
    }.get(page, [])
    return set(keys) | set(TRENDS.get(page, []))


# Lists opened by something other than a card (table rows, waterfall bars, the needs-attention strip).
EXTRA_DRILLS = {
    "master:revenue": {"revenue"},
    "master:mrr": set(MRR_WATERFALL),
    "master:cancellations": {"service_cancellations"},
    "master:manager": {"visits_to_rebook", "first_visits_to_confirm", "complaints_overdue", "revenue_expected", "capacity_day", "capacity_review"},
}


def page_drills(page: str) -> set[str]:
    drills = {KPIS[key]["drill"] for key in page_kpis(page) if key in KPIS and KPIS[key]["drill"]}
    return drills | EXTRA_DRILLS.get(page, set())
