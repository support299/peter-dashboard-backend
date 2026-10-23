from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from insights import periods
from insights.metrics import Metrics
from insights.models import MrrSnapshot, RecurringPlan
from insights.mrr import frequency_from_dates, movements, rebuild_mrr
from insights.rebuild import rebuild_insights
from integrations.models import Integration
from operations.models import (
    CANCELLATION_VISIT,
    CancellationRecord,
    Client,
    Invoice,
    InvoiceLineItem,
    Job,
    Visit,
)
from operations.revenue import classify_lines, reclassify_all

TODAY = date(2026, 10, 13)


def at(day: date, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class ClassificationTests(TestCase):
    def classify(self, *names, recurring=False, company=False):
        _, _, results = classify_lines([(n, Decimal("100")) for n in names], recurring=recurring, is_company=company)
        return results

    def test_real_line_item_names(self):
        cases = {
            "Dental Clinic Cleaning": ("commercial", "commercial"),
            "Service de nettoyage de bureau (1x par semaine)": ("commercial", "commercial"),
            "Maintenance Cleaning (Condo/Apartment Building)": ("commercial", "commercial"),
            "Equipment Rental (1 Day)": ("rentals", "rentals"),
            "Window Cleaning (Interior & Exterior)": ("specialized", "specialized"),
            "Move In/Out Cleaning": ("residential", "move"),
            "Deep Cleaning (First time Service)": ("residential", "first_deep"),
            "Residential: First Cleaning – Detailed": ("residential", "first_deep"),
            "Grand ménage (premier ménage)": ("residential", "first_deep"),
            "Bi-Weekly Cleaning - Detailed": ("residential", "residential_recurring"),
            "Ménage mensuel": ("residential", "residential_recurring"),
            "Post Construction Cleaning": ("specialized", "specialized"),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.classify(name)[0], expected)

    def test_add_ons_and_tips_follow_main_service(self):
        results = self.classify("Deep Cleaning, Detailed", "Inside Oven", "Interior oven cleaning, basic", "Tip")
        self.assertEqual(results[1], ("residential", "first_deep"))
        self.assertEqual(results[2], ("residential", "first_deep"))
        self.assertEqual(results[3], ("residential", "other"))

    def test_generic_cleaning_uses_job_type_and_company(self):
        self.assertEqual(self.classify("Cleaning service", recurring=True)[0], ("residential", "residential_recurring"))
        self.assertEqual(self.classify("Cleaning service", recurring=False)[0], ("residential", "residential_other"))
        self.assertEqual(self.classify("Bi-Weekly Cleaning Service", company=True)[0], ("commercial", "commercial"))
        # A company buying windows is still Specialized.
        self.assertEqual(self.classify("Window Cleaning", company=True)[0], ("specialized", "specialized"))


class PeriodTests(TestCase):
    def test_partial_month_compares_same_days(self):
        with mock.patch("insights.periods.today", return_value=TODAY):
            period = periods.build("month", TODAY)
            self.assertEqual((period.start, period.end), (date(2026, 10, 1), TODAY))
            prev, ly = period.previous(), period.last_year()
        self.assertEqual((prev.start, prev.end), (date(2026, 9, 1), date(2026, 9, 13)))
        self.assertEqual((ly.start, ly.end), (date(2025, 10, 1), date(2025, 10, 13)))

    def test_full_quarter_and_navigation(self):
        with mock.patch("insights.periods.today", return_value=TODAY):
            q3 = periods.build("quarter", date(2026, 8, 5))
            self.assertEqual((q3.start, q3.end, q3.label), (date(2026, 7, 1), date(2026, 9, 30), "Q3 2026"))
            self.assertEqual(q3.previous().label, "Q2 2026")
            self.assertEqual(q3.last_year().label, "Q3 2025")
            self.assertEqual(q3.step(1).start, date(2026, 10, 1))


class FrequencyTests(TestCase):
    def test_buckets(self):
        start = date(2026, 1, 5)
        weekly = [start + timedelta(days=7 * i) for i in range(6)]
        biweekly = [start + timedelta(days=14 * i) for i in range(6)]
        monthly = [date(2026, m, 10) for m in range(1, 7)]
        self.assertEqual(frequency_from_dates(weekly)[0], "Weekly")
        self.assertEqual(frequency_from_dates(biweekly)[0], "Bi-Weekly")
        self.assertEqual(frequency_from_dates(monthly), ("Monthly", Decimal(1)))
        self.assertIsNone(frequency_from_dates([start]))


class MasterDashboardDataMixin:
    """A small company: two recurring clients, one that cancelled, one company client, invoices, a cancellation."""

    @classmethod
    def setUpTestData(cls):
        cls.integration = Integration.objects.create(status=Integration.STATUS_ACTIVE, account_name="Test")
        cls.alice = Client.objects.create(integration=cls.integration, external_id="c1", name="Alice")
        cls.bob = Client.objects.create(integration=cls.integration, external_id="c2", name="Bob")
        cls.dental = Client.objects.create(integration=cls.integration, external_id="c3", name="Dental Co", is_company=True)

        # Alice: bi-weekly $150 since March, still active.
        cls.alice_job = Job.objects.create(
            integration=cls.integration, external_id="j1", client=cls.alice, title="Alice - Home - Bi-weekly",
            job_type="RECURRING", is_recurring=True, job_status="active",
        )
        cls._visits(cls.alice_job, date(2026, 3, 2), 14, date(2026, 12, 31), Decimal("150"))
        # Bob: monthly $200 from Feb to end of July, then stopped.
        cls.bob_job = Job.objects.create(
            integration=cls.integration, external_id="j2", client=cls.bob, title="Bob - Monthly",
            job_type="RECURRING", is_recurring=True, job_status="archived",
        )
        cls._visits(cls.bob_job, date(2026, 2, 10), 30, date(2026, 7, 20), Decimal("200"))

        cls._invoice("i1", cls.alice, cls.alice_job, date(2026, 9, 15), [("Bi-Weekly Cleaning - Detailed", "300"), ("Tip", "20")])
        cls._invoice("i2", cls.dental, None, date(2026, 9, 20), [("Dental Clinic Cleaning", "1000")], discount="100")
        cls._invoice("i3", cls.alice, cls.alice_job, date(2026, 10, 5), [("Bi-Weekly Cleaning - Detailed", "300")])
        cls._invoice("i4", cls.bob, None, date(2026, 10, 6), [("Equipment Rental (1 Day)", "149")])

        Visit.objects.create(
            integration=cls.integration, external_id="tu1", client=cls.alice, title="Alice - Touch-Up",
            start_at=at(date(2026, 9, 25)), is_complete=True,
        )
        CancellationRecord.objects.create(
            integration=cls.integration, jobber_task_external_id="t1", cancellation_type=CANCELLATION_VISIT,
            client=cls.alice, client_name="Alice", task_date=date(2026, 9, 18), value=Decimal("150"), division="residential",
        )

    @classmethod
    def _visits(cls, job, first, step, last, price):
        day, n = first, 0
        while day <= last:
            Visit.objects.create(
                integration=cls.integration, external_id=f"{job.external_id}-v{n}", client=job.client, job=job,
                title=job.title, start_at=at(day), is_complete=day < TODAY, is_recurring=True, price_per_visit=price,
                line_item_total=price,
            )
            day += timedelta(days=step)
            n += 1

    @classmethod
    def _invoice(cls, ext, client, job, day, lines, discount="0"):
        subtotal = sum(Decimal(total) for _, total in lines)
        invoice = Invoice.objects.create(
            integration=cls.integration, external_id=ext, client=client, invoice_number=ext, invoice_status="paid",
            issued_date=at(day), subtotal=subtotal, discount_amount=Decimal(discount), balance=Decimal(0),
        )
        if job:
            invoice.jobs.add(job)
        for n, (name, total) in enumerate(lines):
            InvoiceLineItem.objects.create(
                integration=cls.integration, external_id=f"{ext}-l{n}", invoice=invoice, name=name, total=Decimal(total)
            )
        return invoice


class MrrTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        self.today = mock.patch("django.utils.timezone.localdate", return_value=TODAY)
        self.today.start()
        self.addCleanup(self.today.stop)

    def test_plans_detected_from_jobber(self):
        rebuild_mrr(self.integration, today=TODAY)
        alice = RecurringPlan.objects.get(job=self.alice_job)
        bob = RecurringPlan.objects.get(job=self.bob_job)
        self.assertEqual((alice.frequency, alice.detected_status), ("Bi-Weekly", "active"))
        self.assertEqual(alice.monthly_value, Decimal("325.00"))  # 150 × 26 / 12
        self.assertEqual((bob.frequency, bob.detected_status, bob.end_date), ("Monthly", "cancelled", date(2026, 7, 10)))

    def test_movements_new_and_churn(self):
        rebuild_mrr(self.integration, today=TODAY)
        march = movements(self.integration, date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(march.totals["new"], Decimal("325.00"))
        self.assertEqual(march.starting, Decimal("200.00"))
        self.assertEqual(march.ending, Decimal("525.00"))
        july = movements(self.integration, date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(july.totals["churned"], Decimal("-200.00"))
        self.assertEqual(july.ending, Decimal("325.00"))

    def test_price_change_after_tracking_is_expansion_and_history_is_preserved(self):
        rebuild_mrr(self.integration, today=TODAY)  # tracking starts in October
        october = MrrSnapshot.objects.get(plan__job=self.alice_job, month=date(2026, 10, 1))
        self.assertEqual(october.source, MrrSnapshot.SOURCE_TRACKED)

        # Price goes up from November's visits; next run is in November.
        Visit.objects.filter(job=self.alice_job, start_at__gte=at(date(2026, 11, 1))).update(price_per_visit=Decimal("180"))
        november_day = date(2026, 11, 2)
        with mock.patch("django.utils.timezone.localdate", return_value=november_day):
            rebuild_mrr(self.integration, today=november_day)
            result = movements(self.integration, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(result.totals["expansion"], Decimal("65.00"))  # (180 - 150) × 26 / 12
        october.refresh_from_db()
        self.assertEqual(october.monthly_value, Decimal("325.00"))  # October stays as recorded
        self.assertTrue(RecurringPlan.objects.get(job=self.alice_job).changes.filter(change_type="price").exists())

    def test_paused_plan_leaves_active_mrr_but_stays_recurring(self):
        rebuild_mrr(self.integration, today=TODAY)
        plan = RecurringPlan.objects.get(job=self.alice_job)
        plan.status_override = "paused"
        plan.save()
        rebuild_mrr(self.integration, today=TODAY)
        result = movements(self.integration, date(2026, 10, 1), TODAY)
        self.assertEqual(result.ending, Decimal(0))
        self.assertEqual(result.paused_ending, Decimal("325.00"))
        self.assertEqual(len(result.recurring_end), 1)
        self.assertEqual(result.totals["paused"], Decimal("-325.00"))


class ApiTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        user = get_user_model().objects.create_user("boss", "boss@example.com", "pw", is_staff=True)
        self.client.force_login(user)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_revenue_split_by_division_and_category(self):
        data = self.get("/api/insights/revenue/?period=month&anchor=2026-09-01")
        cards = {c["key"]: c for c in data["cards"]}
        # 320 (Alice incl. tip) + 900 (dental after $100 discount)
        self.assertEqual(cards["revenue"]["current"], 1220.0)
        self.assertEqual(cards["invoiced_revenue"]["current"], 1320.0)
        divisions = {row["key"]: row["current"] for row in data["divisions"]}
        self.assertEqual(divisions["commercial"], 900.0)
        self.assertEqual(divisions["residential"], 320.0)
        categories = {row["key"]: row["current"] for row in data["categories"]}
        self.assertEqual(categories["other"], 20.0)
        self.assertFalse({c["key"]: c for c in data["profitability"]}["gross_profit"]["available"])

        october = self.get("/api/insights/revenue/?period=month&anchor=2026-10-01&division=rentals")
        self.assertEqual({c["key"]: c for c in october["cards"]}["revenue"]["current"], 149.0)
        self.assertEqual(october["period"]["end"], "2026-10-13")
        self.assertEqual(october["comparisons"]["previous"]["end"], "2026-09-13")

    def test_every_section_and_drill_down_responds(self):
        query = "period=month&anchor=2026-09-01"
        overview = self.get(f"/api/insights/overview/?{query}")
        self.assertEqual(len(overview["scorecard"]), 12)
        for section in ("clients", "mrr", "cancellations", "quality"):
            self.get(f"/api/insights/{section}/?{query}")
        self.get("/api/insights/meta/")
        trend = self.get("/api/insights/trend/?metrics=revenue,mrr,visits_completed,gross_profit&range=12m")
        self.assertEqual(len(trend["series"][0]["points"]), 12)
        self.assertFalse(trend["series"][3]["available"])
        for metric in (
            "revenue", "receivables", "receivables_overdue", "top_clients", "visits_completed", "touch_ups",
            "active_clients", "active_clients_start", "new_clients", "returning_clients", "lost_clients",
            "recurring_clients", "mrr_plans", "paused_clients", "new_recurring_jobs", "cancelled_recurring_jobs",
            "lost_recurring_clients", "mrr_new", "mrr_expansion", "mrr_reactivated", "mrr_contraction",
            "mrr_churned", "mrr_paused", "mrr_untracked", "cancelled_visits", "cancelled_visits_internal", "cancelled_jobs",
        ):
            with self.subTest(metric=metric):
                self.get(f"/api/insights/drill/?{query}&metric={metric}")

    def test_touch_ups_and_cancellation_reason(self):
        quality = self.get("/api/insights/quality/?period=month&anchor=2026-09-01")
        self.assertEqual({c["key"]: c for c in quality["redos"]}["touch_ups"]["current"], 1)

        record = CancellationRecord.objects.get()
        response = self.client.patch(
            f"/api/insights/cancellations/{record.id}/", data='{"reason": "employee_absence", "reason_notes": "Sick"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = self.get("/api/insights/cancellations/?period=month&anchor=2026-09-01")
        cards = {c["key"]: c for c in data["cards"]}
        self.assertEqual(cards["cancelled_visits_value"]["current"], 150.0)
        self.assertEqual(cards["cancellations_internal"]["current"], 1)
        # Before the first cancellation record, the number is unavailable rather than zero.
        self.assertIsNone(cards["cancelled_visits"]["last_year"])

    def test_plan_pause_via_api(self):
        plan = RecurringPlan.objects.get(job=self.alice_job)
        response = self.client.patch(
            f"/api/insights/mrr/plans/{plan.id}/",
            data='{"status_override": "paused", "pause_reason": "Travelling", "expected_return": "2026-12-01"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = self.get("/api/insights/mrr/?period=month&anchor=2026-10-01")
        cards = {c["key"]: c for c in data["cards"]}
        self.assertEqual(cards["mrr"]["current"], 0.0)
        self.assertEqual(cards["mrr_paused"]["current"], 325.0)
        self.assertEqual(cards["paused_clients"]["current"], 1)


class MetricsUnitTests(MasterDashboardDataMixin, TestCase):
    def test_active_clients_use_90_day_rule(self):
        reclassify_all(self.integration)
        metrics = Metrics(self.integration)
        # Bob's last visit was July 10: still active on Sep 1 (within 90 days), gone by Oct 13.
        self.assertIn(self.bob.id, metrics.active_client_ids(date(2026, 9, 1)))
        self.assertNotIn(self.bob.id, metrics.active_client_ids(date(2026, 10, 13)))


def hub_payload(**overrides):
    """Shape of the Hub analytics endpoint's new sections (sample from 22 Oct 2026)."""
    cleaner = {"id": "h-vic", "name": "Victoria Lee", "jobber_user_id": "U-vic"}

    def review(rid, source, when, client, client_id, rating, cleaners):
        return {
            "id": rid, "source": source, "form_slug": None, "submitted_at": when, "client_name": client, "client_email": None,
            "client_jobber_id": client_id, "rating": rating, "recommend_rating": None, "rebook_rating": None, "comment": "",
            "improvement_area": "", "service_area": "", "visit_date": None, "cleaners": cleaners,
        }

    def complaint(cid, when, incident, kind, status, resolved):
        return {
            "id": cid, "submitted_at": when, "client_name": "Alice", "client_email": None, "client_jobber_id": "c1",
            "incident_date": incident, "team": [cleaner], "responsible": [cleaner], "type": kind,
            "what_happened": "x", "how_fixed": "y", "status": status, "resolved_date": resolved,
        }

    def hours(hid, day, worked, overtime):
        return {
            "id": hid, "date": day, "employee_id": "h-vic", "employee_name": "Victoria Lee", "jobber_user_id": "U-vic",
            "hours_worked": worked, "regular_hours": worked, "drive_time_hours": "0.00", "overtime_hours": overtime, "source": "jobber",
        }

    payload = {
        "meta": {},
        "reviews": {
            "available": True,
            "records": [
                review("r1", "how_are_we_doing", "2026-09-21T17:47:54+00:00", "Alice", "c1", 5, [cleaner]),
                review("r2", "review_your_recent_experience", "2026-09-22T15:20:08+00:00", "Dental Co", "c3", 3, [cleaner]),
                review("google-1", "google", "2026-09-10T12:00:00+00:00", "Someone", None, 5, []),
            ],
        },
        "complaints": {
            "available": True,
            "records": [
                complaint("k1", "2026-09-14T18:18:39+00:00", "2026-09-11", "Missed Areas", "resolved", "2026-09-15"),
                complaint("k2", "2026-09-20T18:18:39+00:00", None, "Rushed Job", "open", None),
            ],
        },
        "service_cancellations": {
            "available": True,
            "records": [
                {
                    "id": "s1", "submitted_at": "2026-07-08T15:38:20+00:00", "client_name": "bob", "client_email": None,
                    "client_jobber_id": None, "frequency": "Monthly", "plan_type": "Detailed", "reason": "Found Cheaper Service",
                    "sub_reasons": ["More flexible scheduling"],
                    "sub_reasons_by_group": {"found_cheaper_service": ["More flexible scheduling"]}, "comments": "",
                },
            ],
        },
        "work_hours": {
            "available": True,
            "overtime_rule": "Hours over 40 per ISO week",
            "records": [hours("w1", "2026-09-01", "6.00", "0.00"), hours("w2", "2026-09-02", "4.00", "1.00")],
        },
    }
    payload.update(overrides)
    return payload


def persist_hub(payload):
    from analytics.hub_models import AdminInternalSyncRun
    from analytics.sync import persist_payload

    persist_payload(payload, AdminInternalSyncRun.objects.create())


class HubSyncTests(TestCase):
    def test_upsert_update_and_window_prune(self):
        from analytics.hub_models import HubComplaint, HubReview, HubWorkHours

        persist_hub(hub_payload())
        self.assertEqual(HubReview.objects.count(), 3)
        self.assertEqual(HubComplaint.objects.get(external_id="k2").event_date, date(2026, 9, 20))  # no incident date: submission date

        # Next night: k2 resolved, Sep 2 hours deleted in Jobber, payload covers Sep 2–30 only.
        payload = hub_payload()
        payload["complaints"]["records"][1].update(status="resolved", resolved_date="2026-09-25")
        payload["work_hours"]["records"] = [dict(payload["work_hours"]["records"][1], id="w3", date="2026-09-03")]
        payload["meta"] = {"from": "2026-09-02", "to": "2026-09-30"}
        persist_hub(payload)
        self.assertEqual(HubComplaint.objects.get(external_id="k2").status, "resolved")
        # w2 removed (inside the window), w1 kept (outside it).
        self.assertEqual(set(HubWorkHours.objects.values_list("external_id", flat=True)), {"w1", "w3"})

    def test_unavailable_section_is_left_alone(self):
        from analytics.hub_models import HubReview

        persist_hub(hub_payload())
        persist_hub(hub_payload(reviews={"available": False}))
        self.assertEqual(HubReview.objects.count(), 3)


class HubMetricsTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        persist_hub(hub_payload())
        rebuild_insights(self.integration)
        user = get_user_model().objects.create_user("boss", "boss@example.com", "pw", is_staff=True)
        self.client.force_login(user)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_quality_numbers(self):
        data = self.get("/api/insights/quality/?period=month&anchor=2026-09-01")
        cards = {c["key"]: c for group in ("feedback", "complaints", "google") for c in data[group]}
        self.assertEqual(cards["feedback_score"]["current"], 4.0)  # (5 + 3) / 2, Google excluded
        self.assertEqual(cards["feedback_responses"]["current"], 2)
        self.assertEqual(cards["complaints"]["current"], 2)
        self.assertEqual(cards["complaints_open"]["current"], 1)
        self.assertEqual(cards["complaint_resolution_days"]["current"], 4.0)  # Sep 11 to Sep 15
        self.assertEqual(cards["google_new_reviews"]["current"], 1)
        self.assertEqual(cards["google_five_star_new"]["current"], 1)
        victoria = next(p for p in data["people"] if p["employee"] == "Victoria Lee")
        self.assertEqual((victoria["reviews"], victoria["complaints"], victoria["hours"]), (2, 2, 10.0))
        # Division filter follows the matched Jobber client: Dental Co is Commercial.
        commercial = self.get("/api/insights/quality/?period=month&anchor=2026-09-01&division=commercial")
        self.assertEqual({c["key"]: c for c in commercial["feedback"]}["feedback_score"]["current"], 3.0)

    def test_operations_hours(self):
        data = self.get("/api/insights/operations/?period=month&anchor=2026-09-01")
        cards = {c["key"]: c for c in data["work"]}
        self.assertEqual(cards["labour_hours"]["current"], 10.0)
        self.assertEqual(cards["overtime_hours"]["current"], 1.0)
        self.assertEqual(cards["revenue_per_labour_hour"]["current"], 122.0)  # $1,220 / 10 h
        self.assertIsNone(cards["labour_hours"]["previous"])  # no hours in August: unavailable, not zero
        self.assertFalse({c["key"]: c for c in data["payroll"]}["payroll_pct"]["available"])

    def test_churn_reason_from_hub_form_and_new_drills(self):
        drill = self.get("/api/insights/drill/?period=month&anchor=2026-07-01&metric=mrr_churned")
        self.assertEqual(drill["rows"][0]["reason"], "Found Cheaper Service (More flexible scheduling)")
        for metric in (
            "feedback", "google_reviews", "google_new_reviews", "complaints", "complaints_open",
            "complaints_resolved", "labour_hours", "absences", "service_cancellations",
        ):
            with self.subTest(metric=metric):
                self.get(f"/api/insights/drill/?period=month&anchor=2026-09-01&metric={metric}")
        trend = self.get("/api/insights/trend/?metrics=labour_hours,complaints,feedback_score,google_new_reviews&range=12m")
        self.assertTrue(all(s["available"] for s in trend["series"]))
        cancellations = self.get("/api/insights/cancellations/?period=month&anchor=2026-07-01")
        self.assertEqual(cancellations["service_reasons"][0]["label"], "Found Cheaper Service")


class PricingAndInternalTests(MasterDashboardDataMixin, TestCase):
    """Client feedback (Oct 23): fixed monthly accounts, $0 visit prices, internal Clean on the Go jobs."""

    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)

    def recurring(self, ext, client, price, step=14):
        job = Job.objects.create(
            integration=self.integration, external_id=ext, client=client, title=f"{client.name} job",
            job_type="RECURRING", is_recurring=True, job_status="active",
        )
        self._visits(job, date(2026, 6, 1), step, date(2026, 12, 31), Decimal(price))
        return job

    def test_fixed_monthly_account_uses_monthly_invoice(self):
        globe = Client.objects.create(integration=self.integration, external_id="g", name="Globe Electric", is_company=True)
        # Like Globe Electric: $4,880 on every visit (3.5 visits a week), invoiced once a month.
        job = self.recurring("jg", globe, "4880", step=2)
        for n, month in enumerate([7, 8, 9, 10]):
            self._invoice(f"ig{n}", globe, job, date(2026, month, 1), [("Office Cleaning", "4880")])
        september = periods.build("month", date(2026, 9, 1))
        before = Metrics(self.integration).value("completed_visit_value", september)
        rebuild_insights(self.integration)
        plan = RecurringPlan.objects.get(job=job)
        self.assertEqual((plan.monthly_value, plan.pricing_source), (Decimal("4880.00"), "invoice"))
        # History is priced the same way, not $4,880 × 15 visits.
        self.assertEqual(MrrSnapshot.objects.get(plan=plan, month=date(2026, 9, 1)).monthly_value, Decimal("4880.00"))
        # Completed visit value: Globe's September visits add up to about one month ($4,880), not $4,880 each.
        after = Metrics(self.integration).value("completed_visit_value", september)
        other_visits = Visit.objects.filter(
            is_complete=True, start_at__gte=september.start_dt, start_at__lt=september.end_dt
        ).exclude(job=job)
        globe_share = after - sum((v.price_per_visit or Decimal(0) for v in other_visits), Decimal(0))
        self.assertLess(abs(globe_share - Decimal(4880)), Decimal(500))
        self.assertGreater(before - after, Decimal(50000))  # was ~15 visits × $4,880

    def test_recurring_job_without_visits_never_counts(self):
        ghost = Client.objects.create(integration=self.integration, external_id="gh", name="Ghost")
        job = Job.objects.create(
            integration=self.integration, external_id="jgh", client=ghost, title="No visits", job_type="RECURRING",
            is_recurring=True, job_status="archived", total=Decimal(300),
            start_at=at(date(2024, 1, 1)), end_at=at(date(2030, 1, 1)),
        )
        rebuild_insights(self.integration)
        plan = RecurringPlan.objects.get(job=job)
        self.assertEqual(plan.detected_status, "cancelled")
        self.assertFalse(MrrSnapshot.objects.filter(plan=plan).exists())

    def test_churn_is_reported_as_a_positive_loss(self):
        rebuild_insights(self.integration)
        july = periods.build("month", date(2026, 7, 1))
        self.assertEqual(Metrics(self.integration).value("mrr_churned", july), Decimal("200.00"))

    def test_zero_price_falls_back_to_invoices_or_is_flagged(self):
        carol = Client.objects.create(integration=self.integration, external_id="z1", name="Carol")
        dave = Client.objects.create(integration=self.integration, external_id="z2", name="Dave")
        priced_by_invoice = self.recurring("jz1", carol, "0")
        for n, month in enumerate([8, 9, 10]):
            self._invoice(f"iz{n}", carol, priced_by_invoice, date(2026, month, 3), [("Monthly Cleaning Service", "500")])
        no_price = self.recurring("jz2", dave, "0")
        rebuild_insights(self.integration)
        carol_plan = RecurringPlan.objects.get(job=priced_by_invoice)
        self.assertEqual((carol_plan.monthly_value, carol_plan.pricing_source), (Decimal("500.00"), "invoice"))
        self.assertEqual(RecurringPlan.objects.get(job=no_price).pricing_source, "none")
        # Per-visit residential pricing is unchanged.
        self.assertEqual(RecurringPlan.objects.get(job=self.alice_job).pricing_source, "visit")

        user = get_user_model().objects.create_user("boss", "boss@example.com", "pw", is_staff=True)
        self.client.force_login(user)
        rows = self.client.get("/api/insights/mrr/plans/?status=no_price").json()["results"]
        self.assertEqual([r["client"] for r in rows], ["Dave"])

    def test_internal_clean_on_the_go_is_excluded(self):
        internal = Client.objects.create(integration=self.integration, external_id="cotg", name="CLEAN ON THE GO INC.")
        job = self.recurring("jcotg", internal, "0", step=2)  # team leader tasks
        rebuild_insights(self.integration)
        internal.refresh_from_db()
        self.assertTrue(internal.is_internal)
        self.assertFalse(RecurringPlan.objects.filter(job=job).exists())

        september = periods.build("month", date(2026, 9, 1))
        metrics = Metrics(self.integration)
        expected = Visit.objects.filter(is_complete=True, start_at__gte=september.start_dt, start_at__lt=september.end_dt)
        self.assertTrue(expected.filter(client=internal).exists())
        self.assertEqual(metrics.value("visits_completed", september), expected.exclude(client=internal).count())
        self.assertNotIn(internal.id, metrics.active_client_ids(date(2026, 9, 30)))


class ExplanationTests(TestCase):
    def test_every_number_explains_how_it_is_calculated(self):
        from insights.metrics import KPIS

        self.assertEqual([key for key, meta in KPIS.items() if not meta["how"]], [])
