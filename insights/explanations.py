"""Plain-language "How it's calculated" text for every Master Dashboard number (shown on hover)."""

ACTIVE = "a completed visit in the last 90 days or a visit booked in the next 60 days"
EXCLUDES = "Clean on the Go's own jobs are left out."

HOW = {
    # Revenue & profitability
    "revenue": "Jobber invoices dated in the period, before tax (GST/QST), minus discounts. Draft invoices are not counted.",
    "invoiced_revenue": "Jobber invoices dated in the period, before tax and before discounts.",
    "discounts": "Invoiced revenue minus revenue: the discounts given on the period's invoices.",
    "completed_visit_value": "Price of the visits completed in the period (visit line items). For accounts billed a fixed monthly price, each visit counts as its share of that monthly price.",
    "payments_received": "Money actually received in the period, from Jobber payments. Coming in a later stage.",
    "gross_profit": "Revenue minus cost of sales (cleaner wages and payroll costs, supplies, subcontractors). Needs the Financials section.",
    "gross_margin": "Gross profit ÷ revenue. Needs the Financials section.",
    "net_profit": "Revenue minus all costs. Needs the Financials section.",
    "net_margin": "Net profit ÷ revenue. Needs the Financials section.",
    "cogs": "Cleaner wages and payroll costs, supplies and subcontractors entered in Financials.",
    "payroll": "Payroll totals entered per pay run in Financials, counted in the month of the pay date.",
    "payroll_pct": "Payroll ÷ revenue. Needs the Financials section.",
    "operating_expenses": "Office salaries, rent, software, marketing, insurance and other overhead entered in Financials.",
    # Visits
    "visits_completed": f"Jobber visits marked completed, dated in the period. {EXCLUDES}",
    "avg_revenue_per_visit": "Revenue ÷ completed visits. Uses invoiced revenue, not visit line items.",
    # Clients
    "active_clients": f"Clients with {ACTIVE}, on the last day of the period. {EXCLUDES}",
    "active_clients_start": f"Clients with {ACTIVE}, on the day before the period starts.",
    "new_clients": "Clients whose first ever completed visit falls in the period.",
    "returning_clients": "Clients who were not active at the start of the period, are active at the end, and had been clients before.",
    "lost_clients": "Clients active at the start of the period but no longer active at the end (no completed visit in 90 days and nothing booked).",
    "net_client_growth": "New clients minus lost clients.",
    # Recurring
    "active_recurring_clients": "Clients with an active or paused recurring job in Jobber at the end of the period. Paused clients are included.",
    "paused_clients": "Recurring clients marked Paused or Scheduled to resume in Insights (MRR → Edit).",
    "recurring_retention": "Of the recurring clients at the start of the period, the share still recurring (active or paused) at the end.",
    "new_recurring_jobs": "Jobber recurring jobs whose first visit falls in the period.",
    "cancelled_recurring_jobs": "Jobber recurring jobs that ended in the period: no visit booked and the last visit more than 35 days ago (or archived), or marked Cancelled in Insights.",
    "net_recurring_jobs": "New recurring jobs minus cancelled recurring jobs.",
    # MRR
    "mrr": "Monthly recurring revenue of all active recurring clients at the end of the period. Each client = price per visit × visits per month (weekly × 52 ÷ 12, bi-weekly × 26 ÷ 12, monthly × 1). Accounts billed a fixed monthly price use their latest monthly invoice. Paused clients are not included.",
    "mrr_starting": "MRR at the end of the month before the period.",
    "mrr_new": "MRR from clients who started their first recurring service in the period.",
    "mrr_expansion": "Extra MRR from existing clients: price increases, more frequent visits or added services. Tracked from the first full month after tracking started.",
    "mrr_reactivated": "MRR from former recurring clients who came back.",
    "mrr_contraction": "MRR lost from existing clients who now pay less (price decrease or fewer visits) but stayed.",
    "mrr_churned": "MRR lost from recurring clients who stopped completely.",
    "mrr_paused_net": "MRR moved out of Active MRR by pauses (negative) or back in by resumed clients (positive).",
    "mrr_untracked": "Price or frequency changes in months before tracking started, which can't be split into expansion or contraction.",
    "mrr_net_new": "Ending MRR minus starting MRR (new + expansion + reactivated − contraction − churned ± pauses).",
    "mrr_paused": "MRR of clients currently marked Paused or Scheduled to resume. Not included in Active MRR.",
    # Cancellations
    "cancelled_visits": "'Cancelled Visit' tasks created in Jobber, dated in the period. Records start in July 2026.",
    "cancelled_visits_value": "For each cancelled visit, the client's average price per visit.",
    "cancelled_jobs": "'Cancelled Job' tasks created in Jobber, dated in the period.",
    "cancelled_jobs_value": "For each cancelled recurring job, the client's monthly recurring value.",
    "cancellations_internal": "Cancelled visits whose reason (set in Insights) is Employee absence or Company scheduling issue.",
    "service_cancellation_forms": "Hub Service Cancellation Forms submitted in the period.",
    # Quality
    "touch_ups": "Visits with 'Touch-up' or 'Touch up' in the Jobber title (any capitalisation).",
    "touch_up_rate": "Touch-up visits ÷ completed visits × 100.",
    "feedback_score": "Average rating (1–5) of the Hub forms 'How are we doing?' and 'Review your recent experience!' submitted in the period. Google reviews are not included.",
    "feedback_responses": "Number of Hub feedback forms submitted in the period.",
    "feedback_participation": "Hub feedback responses ÷ completed visits.",
    "complaints": "Hub complaint forms, counted on the incident date (or the submission date if none).",
    "complaint_rate": "Complaints ÷ completed visits × 100.",
    "complaints_open": "Complaints in the period whose status in the Hub is not Resolved.",
    "complaint_resolution_days": "Average days from incident to the resolved date, for complaints resolved in the period.",
    "google_rating": "Average rating of all Google reviews up to the end of the period (collected by the Hub through UMB / GoHighLevel).",
    "google_reviews": "Total Google reviews up to the end of the period.",
    "google_new_reviews": "Google reviews posted in the period.",
    "google_five_star_new": "5-star Google reviews posted in the period.",
    # Operations
    "labour_hours": "Hours from Jobber timesheets (clock in/out), sent by the Hub per employee per day. Regular hours plus drive time.",
    "overtime_hours": "Hours over 40 in an employee's Monday–Sunday week, calculated by the Hub.",
    "drive_time_hours": "Time on the Jobber 'Driving' timer. Zero until the team starts using it.",
    "revenue_per_labour_hour": "Revenue ÷ labour hours. Only shown when hours cover the whole period.",
    "absence_days": "Weekdays covered by Hub leave requests of type Absent (rejected requests excluded).",
    "late_arrivals": "Not available per period yet: the Hub only sends a running total.",
    # Concentration & receivables
    "top5_share": "Revenue from the 5 biggest clients in the period ÷ total revenue.",
    "top10_share": "Revenue from the 10 biggest clients in the period ÷ total revenue.",
    "ar_balance": "Unpaid balance on Jobber invoices right now (drafts and bad debt excluded).",
    "ar_overdue": "Unpaid balance on invoices past their due date, right now.",
    # Sales (later stage)
    "leads": "Pricing Calculator quotes plus Jobber requests, without duplicates. Coming in a later stage.",
    "lead_conversion": "Leads who became clients. Coming in a later stage.",
}
