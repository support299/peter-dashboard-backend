"""Manager Overview for Cassidy (Nov 2026) and the capacity groundwork behind it."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from analytics.hub_models import AdminInternalSyncRun, HubComplaint, HubEmployee, HubLeaveRequest
from analytics.sync import persist_payload
from insights import periods
from insights.attention import overdue_before, overdue_complaints, visits_to_rebook
from insights.capacity import capacity_days, is_capacity_task, parse_hours, task_cleaners, week_totals
from insights.manager_views import history_periods, last_completed_week
from insights.metrics import Metrics
from insights.tests import TODAY, MasterDashboardDataMixin
from insights.targets import month_forecast
from operations.models import Client, Job, JobberTask, Quote, Visit


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class HoursTests(SimpleTestCase):
    def test_hours_written_in_instructions(self):
        cases = {
            "\U0001fae74hr job": (D(4), "ok"),
            "5h job, key under the mat": (D(5), "ok"),
            "4.5h": (D("4.5"), "ok"),
            "3.5 hours": (D("3.5"), "ok"),
            "4,5 heures": (D("4.5"), "ok"),
            "arrive at 9h30, 4 hours": (D(4), "ok"),  # a time of day isn't hours
            "8-10h job": (D(10), "range"),  # the upper number: free time is never overstated
            "6.5h, or 7.5h with the basement": (None, "unclear"),
            "Code 1234, dog friendly": (None, "missing"),
            "": (None, "missing"),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_hours(text), expected, text)

    def test_available_cleaners_task(self):
        self.assertTrue(is_capacity_task("Available Cleaners: Vicky ATL, Leanna TL"))
        self.assertFalse(is_capacity_task("Jasmine (Absence)"))
        by_title = JobberTask(title="Available Cleaners: Vicky ATL, Leanna TL, Rose, vicky", source_payload={})
        self.assertEqual(task_cleaners(by_title), (["Vicky", "Leanna", "Rose"], "title"))
        assigned = JobberTask(title="Available Cleaners: Vicky", source_payload={"assignedUsers": {"nodes": [{"id": "U2"}, {"id": "U1"}, {"id": "U2"}]}})
        self.assertEqual(task_cleaners(assigned), (["U1", "U2"], "assigned"))  # exact people beat names


class CapacityTests(MasterDashboardDataMixin, TestCase):
    def test_booked_available_bookable_and_shortfall(self):
        monday, tuesday = date(2026, 11, 16), date(2026, 11, 17)
        for n, (day, text, division) in enumerate(
            [(monday, "4h job", "residential"), (monday, "8-10h job", "commercial"), (monday, "no hours", "residential"), (tuesday, "5h", "residential")]
        ):
            Visit.objects.create(integration=self.integration, external_id=f"cap{n}", client=self.alice, start_at=at(day), instructions=text, division=division)
        Visit.objects.create(integration=self.integration, external_id="cap-x", client=self.alice, start_at=at(monday), instructions="20h", is_cancelled=True)
        JobberTask.objects.create(integration=self.integration, external_id="t-mon", title="Available Cleaners: Vicky ATL", due_at=at(monday, 7), instructions="John on call - specialty only")
        days = capacity_days(self.integration, monday, tuesday)
        mon, tue = days
        self.assertEqual((mon["booked"], mon["available"], mon["bookable"], mon["shortfall"]), (D(14), D("6.5"), D(0), D("7.5")))
        self.assertEqual(mon["booked_by_division"], {"residential": D(4), "commercial": D(10)})
        self.assertEqual(sorted(r["status"] for r in mon["review"]), ["missing", "range"])
        self.assertEqual(mon["task_notes"], "John on call - specialty only")
        self.assertEqual((tue["availability"], tue["bookable"]), ("unconfirmed", None))
        totals = week_totals(days)
        self.assertEqual((totals["booked"], totals["shortfall"], totals["unconfirmed_days"], totals["to_review"]), (D(19), D("7.5"), 1, 2))
        commercial = capacity_days(self.integration, monday, monday, division="commercial")[0]
        self.assertEqual((commercial["booked"], commercial["bookable"]), (D(10), None))  # available is company-wide
        self.assertEqual(week_totals([commercial])["bookable"], None)  # not a misleading 0


class LeaveAndTaskSyncTests(TestCase):
    def test_leave_role_and_affected_visits(self):
        rows = [{
            "submission_id": "sub-1", "employee_id": "e1", "employee_name": "Rose", "status": "pending", "leave_type": "Vacation",
            "start_date": "2026-11-20", "end_date": "2026-11-21", "employee_role": "employee",
            "affected_visits": [{"jobber_visit_id": "V1", "date": "2026-11-20", "client_name": "A client", "job_title": "Bi-weekly", "headcount": 2}],
        }]
        persist_payload({"meta": {}, "leave": {"all_requests": rows}}, AdminInternalSyncRun.objects.create())
        leave = HubLeaveRequest.objects.get()
        self.assertEqual((leave.submission_id, leave.employee_role, leave.affected_visits[0]["headcount"]), ("sub-1", "employee", 2))

    def test_task_sync_keeps_capacity_tasks(self):
        from integrations.sync import JobberSyncService

        sync = JobberSyncService.__new__(JobberSyncService)
        sync.counts, sync.sync_run, sync.query_fields = {}, None, set()
        stored = []
        nodes = [{"id": "1", "title": "Available Cleaners: Vicky"}, {"id": "2", "title": "Buy supplies"}, {"id": "3", "title": "Cancelled visit"}]
        sync._paginate = lambda *a, **k: iter(nodes)
        sync._upsert_task = lambda node: stored.append(node["title"]) or mock.Mock(title=node["title"])
        sync._sync_tasks(cancellation_only=True)
        self.assertEqual(stored, ["Available Cleaners: Vicky", "Cancelled visit"])


class ManagerPageTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Tuesday Oct 13, 2026
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(get_user_model().objects.create_user("cassidy", "c@x.ca", "pw"))

    def test_periods(self):
        self.assertEqual(last_completed_week(TODAY), date(2026, 10, 4))  # Sun Oct 4 – Sat Oct 10
        months = history_periods("month", TODAY)
        self.assertEqual((months[0].start, months[-1].start, months[-1].partial), (date(2025, 10, 1), date(2026, 9, 1), False))
        weeks = history_periods("week", TODAY)
        self.assertEqual((len(weeks), weeks[-1].start, weeks[0].start), (12, date(2026, 10, 4), date(2026, 7, 19)))

    def test_planned_visits_and_open_leads(self):
        metrics = Metrics(self.integration)
        october = periods.build("month", TODAY)
        before = metrics.value("visits_planned", october)
        self.assertTrue(october.partial)  # month to date: Oct 1–13
        self.assertGreaterEqual(before, metrics.value("visits_completed", october))
        # A visit booked later this month is planned, even though the month-to-date view stops today.
        Visit.objects.create(integration=self.integration, external_id="planned-later", client=self.alice, start_at=at(TODAY + timedelta(days=10)))
        self.assertEqual(Metrics(self.integration).value("visits_planned", october), before + 1)

    def test_page_and_history(self):
        HubEmployee.objects.create(external_id="e1", name="Rose", role="employee", status="active")
        HubEmployee.objects.create(external_id="c1", name="Lesly", role="contractor", status="active")
        HubEmployee.objects.create(external_id="c2", name="Gone", role="contractor", status="inactive")
        HubLeaveRequest.objects.create(external_id="sub-1", submission_id="sub-1", employee_name="Rose", employee_role="employee", status="pending", start_date=TODAY + timedelta(days=5), end_date=TODAY + timedelta(days=6))
        HubLeaveRequest.objects.create(external_id="sub-2", employee_name="Lesly", employee_role="contractor", status="approved", start_date=TODAY + timedelta(days=3), end_date=TODAY + timedelta(days=3))
        HubLeaveRequest.objects.create(external_id="sub-3", employee_name="Old", employee_role="employee", status="approved", start_date=TODAY - timedelta(days=20), end_date=TODAY - timedelta(days=19))
        page = self.client.get("/api/insights/manager/").json()
        self.assertEqual([c["key"] for c in page["headline"]][:3], ["leads", "bookings_won", "new_recurring_clients"])
        self.assertEqual({c["key"] for c in page["sales"]}, {"avg_booking_price", "open_leads"})
        self.assertEqual(set(page["attention"]), {"first_visits_to_confirm", "visits_to_rebook", "visits_to_rebook_older", "rebook_days", "overdue_complaints"})
        self.assertIn("still_to_book", page["target"])
        staff = page["staffing"]
        self.assertEqual((staff["employees"], staff["contractors"]), (1, 1))
        self.assertEqual([r["employee"] for r in staff["pending_time_off"]], ["Rose"])
        self.assertEqual(staff["pending_time_off"][0]["review_url"], "https://hub.cleanonthego.com/admin/calendar?request=sub-1")
        self.assertEqual([r["employee"] for r in staff["contractor_unavailable"]], ["Lesly"])
        self.assertEqual(staff["approved_time_off"], [])  # old leave and contractors aren't listed here
        self.assertEqual(page["last_completed_week"], "2026-10-04")
        for grain in ("month", "week"):
            history = self.client.get(f"/api/insights/manager/history/?grain={grain}").json()
            self.assertEqual((len(history["labels"]), len(history["titles"])), (12, 12))
            self.assertEqual("mrr" in [s["key"] for s in history["series"]], grain == "month")
        self.assertEqual(self.client.get("/api/insights/manager/capacity-check/").status_code, 403)  # staff only for now


class PeterAnswersTests(MasterDashboardDataMixin, TestCase):
    """Peter's definitions of Nov 10, 2026."""

    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Tuesday Oct 13, 2026
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_overdue_after_one_business_day(self):
        self.assertEqual(overdue_before(date(2026, 10, 13)), date(2026, 10, 12))  # Tue: Monday's are not overdue yet
        self.assertEqual(overdue_before(date(2026, 10, 12)), date(2026, 10, 9))  # Mon: Friday's wait until Tuesday
        rows = [("old", date(2026, 10, 9), "open"), ("fresh", date(2026, 10, 12), "open"), ("busy", date(2026, 10, 1), "in_progress"), ("done", date(2026, 10, 1), "resolved"), ("done2", date(2026, 10, 1), "Resolved")]
        for ext, day, status in rows:
            HubComplaint.objects.create(external_id=ext, submitted_date=day, event_date=day, status=status)
        overdue = overdue_complaints(Metrics(self.integration))
        self.assertEqual(sorted(overdue.values_list("external_id", flat=True)), ["busy", "old"])
        from insights.drill import drill

        rows = drill(Metrics(self.integration), "complaints_overdue", periods.build("month", TODAY))["rows"]
        self.assertEqual({row["open"] for row in rows}, {"https://hub.cleanonthego.com/admin/submissions/old", "https://hub.cleanonthego.com/admin/submissions/busy"})

    def test_visits_to_rebook(self):
        make = lambda ext, day, **kw: Visit.objects.create(integration=self.integration, external_id=ext, client=self.alice, start_at=at(day), **kw)  # noqa: E731
        make("sick", TODAY - timedelta(days=1))
        make("cancelled", TODAY - timedelta(days=2), is_cancelled=True)
        make("done", TODAY - timedelta(days=2), is_complete=True)
        make("today", TODAY)
        make("ancient", TODAY - timedelta(days=60))
        listed, older = visits_to_rebook(Metrics(self.integration))
        self.assertEqual((list(listed.values_list("external_id", flat=True)), older), (["sick"], 1))
        # Each row opens the visit's job in Jobber, or the client when there's no job.
        from insights.drill import drill

        Visit.objects.filter(external_id="sick").update(job=self.alice_job)
        Job.objects.filter(pk=self.alice_job.pk).update(jobber_web_uri="https://secure.getjobber.com/work_orders/1")
        rows = drill(Metrics(self.integration), "visits_to_rebook", periods.build("month", TODAY))["rows"]
        self.assertEqual(rows[0]["open"], "https://secure.getjobber.com/work_orders/1")
        # Job without a link: the client's page. A non-https link is never used.
        Job.objects.filter(pk=self.alice_job.pk).update(jobber_web_uri="")
        Client.objects.filter(pk=self.alice.pk).update(jobber_web_uri="javascript:alert(1)")
        rows = drill(Metrics(self.integration), "visits_to_rebook", periods.build("month", TODAY))["rows"]
        self.assertEqual(rows[0]["open"], "")
        Client.objects.filter(pk=self.alice.pk).update(jobber_web_uri="https://secure.getjobber.com/clients/9")
        rows = drill(Metrics(self.integration), "visits_to_rebook", periods.build("month", TODAY))["rows"]
        self.assertEqual(rows[0]["open"], "https://secure.getjobber.com/clients/9")

    def test_bookings_won_are_accepted_quotes_not_recurring(self):
        from insights.models import InsightsSetting

        InsightsSetting.put("jobber_sales_sync", {"quotes": {"ok": True}})
        accepted = at(date(2026, 10, 5))
        for n, (job_recurring, status, amount) in enumerate([(True, "converted", "900"), (False, "converted", "400"), (None, "approved", "200"), (None, "awaiting_response", "999"), (None, "approved", "0")]):
            client = Client.objects.create(integration=self.integration, external_id=f"q-c{n}", name=f"Client {n}")
            Quote.objects.create(
                integration=self.integration, external_id=f"q{n}", client=client, quote_status=status, subtotal=D(amount),
                source_created_at=accepted - timedelta(days=3), transitioned_at=accepted,
            )
            if job_recurring is not None:
                Job.objects.create(integration=self.integration, external_id=f"q-j{n}", client=client, is_recurring=job_recurring, source_created_at=accepted + timedelta(days=2))
        october = periods.build("month", TODAY)
        metrics = Metrics(self.integration)
        self.assertEqual((metrics.value("bookings_won", october), metrics.value("avg_booking_price", october)), (3, D("300.00")))  # the $0 one counts, but not in the average
        self.assertIsNone(Metrics(self.integration, division="residential").value("bookings_won", october))  # like leads

    def test_target_forecast_and_still_to_book(self):
        self._invoice("ly", self.bob, None, date(2025, 10, 20), [("Cleaning", "1000")])
        oct_visits = Visit.objects.filter(client=self.alice, start_at__gte=at(date(2026, 10, 1)), start_at__lt=at(date(2026, 11, 1)))
        expected = 150 * oct_visits.count()
        # Not expected to be invoiced: never completed (to rebook), cancelled, already on an invoice.
        Visit.objects.create(integration=self.integration, external_id="missed", client=self.alice, start_at=at(TODAY - timedelta(days=1)), price_per_visit=D(500))
        Visit.objects.create(integration=self.integration, external_id="cxl", client=self.alice, start_at=at(TODAY + timedelta(days=3)), price_per_visit=D(500), is_cancelled=True)
        invoice = self._invoice("oct-x", self.alice, None, date(2026, 10, 2), [("Cleaning", "60")])
        Visit.objects.create(integration=self.integration, external_id="billed", client=self.alice, start_at=at(date(2026, 10, 2)), price_per_visit=D(60), is_complete=True, invoice=invoice)
        Visit.objects.create(integration=self.integration, external_id="no-job", client=self.bob, start_at=at(TODAY + timedelta(days=5)), price_per_visit=D(80))
        forecast = month_forecast(Metrics(self.integration))
        invoiced = D(300 + 149 + 60)  # Oct invoices so far
        self.assertEqual((forecast["target"], forecast["invoiced"]), (D("1200.00"), invoiced))
        self.assertEqual(forecast["expected"], D(expected + 80))
        self.assertEqual(forecast["forecast"], invoiced + expected + 80)
        self.assertEqual((forecast["still_to_book"], forecast["on_track"]), (max(D(1200) - forecast["forecast"], D(0)), forecast["forecast"] >= 1200))
        self.assertIsNone(Metrics(self.integration).value("revenue_target", periods.build("week", TODAY)))  # monthly only


class BookingsTests(MasterDashboardDataMixin, TestCase):
    """Peter, Nov 11 2026: calculator bookings count too; first visits to confirm."""

    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        from insights.models import InsightsSetting

        InsightsSetting.put("jobber_sales_sync", {"quotes": {"ok": True}})

    def _calc(self, ext, email, day, total="300", status="approved"):
        from analytics.pricing_models import PricingSubmission

        return PricingSubmission.objects.create(
            external_id=ext, status=status, customer_email=email, customer_first_name="Cal", final_total=D(total),
            service_names=["Residential: First Cleaning"], source_created_at=at(day - timedelta(days=1)), source_updated_at=at(day),
        )

    def test_calculator_bookings_count_once_with_their_quote(self):
        from insights.bookings import approved_bookings

        day = date(2026, 10, 5)
        self._calc("calc-new", "new@client.ca", day, "250")  # booked themselves, no Jobber client yet
        self._calc("calc-pending", "maybe@client.ca", day, status="submitted")  # not approved
        both = Client.objects.create(integration=self.integration, external_id="both", name="Both", email="both@client.ca")
        self._calc("calc-both", "both@client.ca", day, "999")
        Quote.objects.create(integration=self.integration, external_id="q-both", client=both, quote_status="converted", subtotal=D(350),
                             source_created_at=at(day - timedelta(days=2)), transitioned_at=at(day + timedelta(days=1)))
        october = periods.build("month", TODAY)
        rows = approved_bookings(self.integration, october.start_dt, october.whole().end_dt)
        self.assertEqual(sorted((b.source, str(b.amount)) for b in rows), [("calculator", "250.00"), ("quote", "350.00")])
        metrics = Metrics(self.integration)
        self.assertEqual((metrics.value("bookings_won", october), metrics.value("avg_booking_price", october)), (2, D("300.00")))

    def test_first_visits_to_confirm(self):
        from insights.bookings import to_confirm

        now = at(TODAY)
        self._calc("calc-waiting", "wait@client.ca", TODAY - timedelta(days=3))  # nothing in Jobber yet
        done = Client.objects.create(integration=self.integration, external_id="done", name="Done", email="done@client.ca")
        self._calc("calc-done", "done@client.ca", TODAY - timedelta(days=3))
        job = Job.objects.create(integration=self.integration, external_id="j-done", client=done, source_created_at=at(TODAY - timedelta(days=2)))
        Visit.objects.create(integration=self.integration, external_id="v-done", client=done, job=job, start_at=at(TODAY + timedelta(days=4)), source_created_at=at(TODAY - timedelta(days=2)))
        quoted = Client.objects.create(integration=self.integration, external_id="quoted", name="Quoted")
        Quote.objects.create(integration=self.integration, external_id="q-wait", client=quoted, quote_status="approved", subtotal=D(500),
                             source_created_at=at(TODAY - timedelta(days=6)), transitioned_at=at(TODAY - timedelta(days=5)))
        self._calc("calc-old", "old@client.ca", TODAY - timedelta(days=90))  # older than 60 days: not listed
        # A visit added to a job the client already had counts as scheduled too.
        regular = Client.objects.create(integration=self.integration, external_id="regular", name="Regular", email="regular@client.ca")
        old_job = Job.objects.create(integration=self.integration, external_id="j-old", client=regular, source_created_at=at(TODAY - timedelta(days=300)))
        self._calc("calc-regular", "regular@client.ca", TODAY - timedelta(days=4))
        Visit.objects.create(integration=self.integration, external_id="v-added", client=regular, job=old_job, start_at=at(TODAY + timedelta(days=2)), source_created_at=at(TODAY - timedelta(days=2)))
        waiting = to_confirm(self.integration, now=now)
        self.assertEqual(sorted(b.source for b in waiting), ["calculator", "quote"])
        self.assertEqual({b.kind for b in waiting}, {"One-off"})


class ManagerOnlyAccessTests(MasterDashboardDataMixin, TestCase):
    """Peter, Nov 11 2026: Cassidy sees the Manager overview only."""

    def setUp(self):
        from accounts.models import UserAccess

        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cassidy = get_user_model().objects.create_user("cassidy", "c@x.ca", "pw")
        UserAccess.objects.create(user=self.cassidy, pages=["master:manager"])
        self.client.force_login(self.cassidy)

    def test_only_the_manager_page_and_its_lists(self):
        me = self.client.get("/api/accounts/me/").json()["user"]
        self.assertEqual(me["pages"], ["master:manager"])
        self.assertEqual(self.client.get("/api/insights/manager/").status_code, 200)
        self.assertEqual(self.client.get("/api/insights/manager/history/").status_code, 200)
        self.assertEqual(self.client.get("/api/insights/drill/?metric=visits_to_rebook").status_code, 200)
        self.assertEqual(self.client.get("/api/insights/drill/?metric=leads").status_code, 200)  # New leads card
        self.assertEqual(self.client.get("/api/insights/drill/?metric=expenses_cogs").status_code, 403)  # not on her page
        for path in ("/api/insights/overview/", "/api/insights/revenue/", "/api/operations/dashboard/", "/api/admin-internal/status/", "/api/pricing-calculator/status/", "/api/financials/expenses/"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.post("/api/jobber/sync/").status_code, 403)
        self.assertEqual(self.client.get("/api/jobber/status/").status_code, 200)

    def test_everyone_else_keeps_full_access(self):
        other = get_user_model().objects.create_user("peter", "p@x.ca", "pw")
        self.client.force_login(other)
        self.assertIsNone(self.client.get("/api/accounts/me/").json()["user"]["pages"])
        self.assertEqual(self.client.get("/api/insights/overview/").status_code, 200)
