from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from insights import periods
from insights.metrics import SCORECARD, Metrics
from insights.models import STATUS_CANCELLED, RecurringPlan
from insights.rebuild import rebuild_insights
from insights.tests import TODAY, MasterDashboardDataMixin, at
from operations.models import Client, Invoice, Job


class PeriodTests(TestCase):
    def setUp(self):
        patcher = mock.patch("insights.periods.today", return_value=TODAY)  # Tuesday Oct 13, 2026
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_week_runs_sunday_to_saturday_and_is_cut_at_today(self):
        week = periods.build("week", TODAY)
        self.assertEqual((week.start, week.end, week.full_end), (date(2026, 10, 11), TODAY, date(2026, 10, 17)))
        self.assertTrue(week.partial)
        self.assertEqual(week.label, "Week of Oct 11, 2026 (Oct 11–13, 2026)")
        previous = week.previous()
        self.assertEqual((previous.start, previous.end), (date(2026, 10, 4), date(2026, 10, 6)))  # same 3 days
        last_year = week.last_year()
        self.assertEqual((last_year.start, last_year.start.weekday()), (date(2025, 10, 12), 6))  # a Sunday
        self.assertEqual(periods.build("week", date(2026, 10, 11)).start, date(2026, 10, 11))  # Sunday stays
        self.assertEqual(periods.build("week", date(2026, 10, 10)).start, date(2026, 10, 4))  # Saturday
        self.assertEqual(week.step(-1).start, date(2026, 10, 4))
        whole = periods.build("week", date(2026, 9, 30))
        self.assertEqual(whole.label, "Week of Sep 27, 2026 (Sep 27 – Oct 3, 2026)")

    def test_day_compares_with_yesterday_and_the_same_weekday_last_year(self):
        day = periods.build("day", TODAY)
        self.assertEqual((day.start, day.end, day.label), (TODAY, TODAY, "Tue, Oct 13, 2026"))
        self.assertEqual(day.previous().start, date(2026, 10, 12))
        self.assertEqual(day.last_year().start, date(2025, 10, 14))
        self.assertEqual(day.last_year().start.weekday(), TODAY.weekday())
        self.assertEqual(day.step(1).start, date(2026, 10, 14))

    def test_request_kinds(self):
        request = mock.Mock(GET={"period": "week", "anchor": "2026-09-30"})
        self.assertEqual(periods.from_request(request).start, date(2026, 9, 27))
        request = mock.Mock(GET={"period": "day", "anchor": "2026-09-30"})
        self.assertEqual(periods.from_request(request).kind, "day")


class WeeklyMetricsTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        self.metrics = Metrics(self.integration)
        self.n = 0

    def plan(self, client, start, end=None, value="400", status=""):
        self.n += 1
        job = Job.objects.create(integration=self.integration, external_id=f"wj{self.n}", client=client, title=f"Plan {self.n}", is_recurring=True)
        return RecurringPlan.objects.create(
            integration=self.integration, job=job, client=client, name=client.name, monthly_value=D(value), start_date=start, end_date=end,
            status_override=status,
        )

    def test_week_mrr_counts_starts_and_cancellations(self):
        new = Client.objects.create(integration=self.integration, external_id="w1", name="New Co")
        back = Client.objects.create(integration=self.integration, external_id="w2", name="Back Again")
        gone = Client.objects.create(integration=self.integration, external_id="w3", name="Gone")
        two = Client.objects.create(integration=self.integration, external_id="w4", name="Two Plans")
        self.plan(new, date(2026, 10, 6), value="500")
        self.plan(back, date(2025, 1, 1), end=date(2025, 6, 1), status=STATUS_CANCELLED)
        self.plan(back, date(2026, 10, 7), value="300")
        self.plan(gone, date(2025, 1, 1), end=date(2026, 10, 8), value="250", status=STATUS_CANCELLED)
        self.plan(two, date(2025, 1, 1), end=date(2026, 10, 9), value="100", status=STATUS_CANCELLED)
        self.plan(two, date(2025, 2, 1), value="200")  # still running: dropping one job is contraction, not lost
        switch = Client.objects.create(integration=self.integration, external_id="w5", name="Switched")
        self.plan(switch, date(2025, 1, 1), end=date(2026, 10, 5), value="150", status=STATUS_CANCELLED)
        self.plan(switch, date(2026, 10, 6), value="180")  # replaced inside the week: a price change, not lost + reactivated
        week = periods.build("week", date(2026, 10, 4))  # Oct 4–10
        self.assertEqual(self.metrics.value("mrr_new", week), D("500"))
        self.assertEqual(self.metrics.value("mrr_reactivated", week), D("300"))
        self.assertEqual(self.metrics.value("mrr_churned", week), D("250"))
        self.assertEqual(self.metrics.value("new_recurring_clients", week), 1)
        self.assertEqual(self.metrics.value("mrr_net_new", week), D("550"))  # 500 new + 300 back − 250 lost
        for key in ("mrr_expansion", "mrr_contraction", "recurring_retention"):
            self.assertIsNone(self.metrics.value(key, week), key)
        card = self.metrics.card("mrr_expansion", week, {})
        self.assertIn("by month only", card["note"])
        self.assertIsNotNone(self.metrics.value("mrr", week))  # current MRR is the month's
        day = periods.build("day", date(2026, 10, 8))
        self.assertEqual((self.metrics.value("mrr_churned", day), self.metrics.value("mrr_new", day)), (D("250"), D(0)))

        from insights.drill import HANDLERS as DRILLS

        drill = DRILLS["mrr_new"](self.metrics, week)
        self.assertEqual([r["client"] for r in drill["rows"]], ["New Co"])
        self.assertEqual(next(c for c in drill["columns"] if c["key"] == "month")["kind"], "date")

    def test_one_off_jobs_booked_in_the_period(self):
        def job(ext, day, total, client=None, **extra):
            return Job.objects.create(
                integration=self.integration, external_id=ext, client=client or self.alice, title=ext, is_one_off=True, total=D(total),
                source_created_at=at(day), **extra,
            )

        invoiced = job("o1", date(2026, 10, 5), "300")
        shared_a, shared_b = job("o2", date(2026, 10, 6), "100"), job("o3", date(2026, 10, 6), "100")
        job("o4", date(2026, 10, 7), "180")  # not invoiced: job price
        job("o5", date(2026, 10, 7), "0")  # no price: left out of the average
        job("o6", date(2026, 10, 1), "999")  # previous week
        internal = Client.objects.create(integration=self.integration, external_id="int", name="Clean on the Go", is_internal=True)
        job("o7", date(2026, 10, 7), "999", client=internal)
        bill = Invoice.objects.create(integration=self.integration, external_id="oi1", client=self.alice, invoice_status="paid", net_revenue=D("270"))
        bill.jobs.add(invoiced)
        pair = Invoice.objects.create(integration=self.integration, external_id="oi2", client=self.alice, invoice_status="paid", net_revenue=D("240"))
        pair.jobs.add(shared_a, shared_b)
        draft = Invoice.objects.create(integration=self.integration, external_id="oi3", client=self.alice, invoice_status="draft", net_revenue=D("1"))
        draft.jobs.add(invoiced)

        week = periods.build("week", date(2026, 10, 4))
        self.assertEqual(self.metrics.value("new_one_off_jobs", week), 5)
        self.assertEqual(self.metrics.value("new_one_off_revenue", week), D("690.00"))  # 270 + 120 + 120 + 180 + 0
        self.assertEqual(self.metrics.value("avg_one_off_price", week), D("172.50"))  # 690 / 4

        from insights.drill import HANDLERS as DRILLS

        drill = DRILLS["new_one_off_jobs"](self.metrics, week)
        self.assertEqual((len(drill["rows"]), drill["total"]), (5, D("690.00")))
        self.assertEqual({r["priced_from"] for r in drill["rows"]}, {"Invoice", "Job price (not invoiced yet)"})

    def test_ceo_view_has_the_new_cards_on_a_week(self):
        for key in ("mrr_new", "mrr_churned", "mrr_net_new", "new_recurring_clients", "new_one_off_jobs", "new_one_off_revenue", "avg_one_off_price", "cancelled_recurring_clients"):
            self.assertIn(key, SCORECARD)
        self.client.force_login(get_user_model().objects.create_superuser("boss", "b@x.ca", "pw"))
        for kind in ("week", "day"):
            response = self.client.get(f"/api/insights/overview/?period={kind}")
            self.assertEqual(response.status_code, 200, response.content[:300])
            self.assertIn("new_one_off_jobs", {card["key"] for card in response.json()["scorecard"]})
        for url in ("/api/insights/mrr/", "/api/insights/clients/", "/api/insights/cancellations/", "/api/insights/revenue/", "/api/insights/quality/", "/api/insights/sales/", "/api/insights/operations/", "/api/financials/summary/", "/api/financials/pnl/"):
            self.assertEqual(self.client.get(url + "?period=week").status_code, 200, url)


class WeeklyPagesTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        self.client.force_login(get_user_model().objects.create_superuser("boss", "b@x.ca", "pw"))

    def test_every_page_and_drill_down_works_on_a_day_and_a_week(self):
        from insights.drill import HANDLERS

        pages = [
            "/api/insights/overview/", "/api/insights/revenue/", "/api/insights/clients/", "/api/insights/mrr/", "/api/insights/cancellations/",
            "/api/insights/quality/", "/api/insights/sales/", "/api/insights/operations/", "/api/insights/alerts/",
            "/api/financials/summary/", "/api/financials/pnl/", "/api/financials/cash/", "/api/financials/report/", "/api/financials/expenses/",
            "/api/financials/payroll/", "/api/financials/contractor-pay/",
        ]
        for kind in ("day", "week"):
            for url in pages:
                response = self.client.get(f"{url}?period={kind}&anchor=2026-10-07")
                self.assertEqual(response.status_code, 200, f"{kind} {url} {response.content[:200]}")
            for metric in HANDLERS:
                response = self.client.get(f"/api/insights/drill/?metric={metric}&period={kind}&anchor=2026-10-07")
                self.assertIn(response.status_code, (200, 400, 403), f"{kind} {metric} {response.content[:200]}")


class PartialComparisonTests(MasterDashboardDataMixin, TestCase):
    def test_a_comparison_that_starts_before_its_data_is_na(self):
        from operations.models import CANCELLATION_VISIT, CancellationRecord, JobberTask

        for n, day in enumerate([date(2026, 7, 20), date(2026, 10, 5), date(2026, 10, 6)]):
            task = JobberTask.objects.create(integration=self.integration, external_id=f"pc{n}", title="Cancelled visit")
            CancellationRecord.objects.create(integration=self.integration, jobber_task=task, jobber_task_external_id=f"pc{n}", cancellation_type=CANCELLATION_VISIT, client=self.alice, task_date=day)
        from datetime import timedelta

        from insights.metrics import what_changed

        metrics = Metrics(self.integration)
        start = metrics.data_starts()["cancellations"]
        partial = periods.build("custom", start - timedelta(days=10), end=start + timedelta(days=20))  # half before the records begin
        later = periods.build("custom", start + timedelta(days=31), end=start + timedelta(days=61))
        self.assertIsNotNone(metrics.value("cancelled_visits", partial))
        self.assertIsNone(metrics.card("cancelled_visits", later, {"previous": partial})["previous"])
        self.assertNotIn("cancelled_visits", [item["key"] for item in what_changed(metrics, later, partial)])
        self.assertIsNotNone(metrics.card("cancelled_visits", later, {"previous": later.previous()})["previous"])  # fully covered
