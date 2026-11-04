"""Cancellations from Jobber job closes (Nov 2026, agreed with the Hub) and the sync fixes behind them."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from insights import periods
from insights.drill import HANDLERS
from insights.metrics import Metrics
from insights.mrr import VisitRow, closed_on, detect, rebuild_mrr
from insights.models import STATUS_ACTIVE, STATUS_CANCELLED, RecurringPlan
from insights.tests import TODAY, MasterDashboardDataMixin, at
from integrations.client import JobberAPIError
from integrations.sync import JobberSyncService as JobberSync, top_level_fields
from operations.models import CANCELLATION_VISIT, CancellationRecord, Client, Job, JobberTask, Visit


def aware(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


class SelectionTests(TestCase):
    def test_top_level_fields_keep_nested_blocks_and_arguments(self):
        selection = "id\nclient { id }\nassignedUsers(first: 5) {\n  nodes { id }\n}\nvisitsInfo {\n firstVisit { id startAt }\n}\ncompletedAt"
        self.assertEqual(
            top_level_fields(selection),
            ["id", "client { id }", "assignedUsers(first: 5) { nodes { id } }", "visitsInfo { firstVisit { id startAt } }", "completedAt"],
        )

    def test_a_refused_field_no_longer_drops_the_close_date(self):
        sync = JobberSync.__new__(JobberSync)
        sync.counts = {}

        def execute(query, variables=None):
            if "defaultVisitTitle" in query:
                raise JobberAPIError("Field 'defaultVisitTitle' doesn't exist on type 'Job'")
            return {}

        sync.client = mock.Mock(execute=mock.Mock(side_effect=execute))
        working = sync._working_selection("jobs", "id title completedAt defaultVisitTitle client { id } visitsInfo { firstVisit { id } }", "")
        self.assertIn("completedAt", working)
        self.assertIn("visitsInfo { firstVisit { id } }", working)
        self.assertNotIn("defaultVisitTitle", working)
        self.assertEqual(sync.counts["fields_left_out"], {"jobs": ["defaultVisitTitle"]})

        # A webhook for one job then uses the same fields instead of failing.
        sync.integration = None
        sync._upsert_job = mock.Mock()
        sync.client.execute = mock.Mock(side_effect=lambda query, variables=None: execute(query) or {"job": {"id": "j1"}})
        self.assertTrue(sync.apply_remote_node("JOB", "j1"))
        sync._upsert_job.assert_called_once_with({"id": "j1"})


class VisitPruneTests(MasterDashboardDataMixin, TestCase):
    def sync(self):
        sync = JobberSync.__new__(JobberSync)
        sync.integration, sync.counts, sync.sync_run = self.integration, {}, None
        return sync

    def test_upcoming_visits_jobber_dropped_are_removed_past_ones_kept(self):
        now = timezone.now()
        for n in range(20):
            Visit.objects.create(integration=self.integration, external_id=f"pv{n}", client=self.alice, start_at=now + timedelta(days=n + 2))
        Visit.objects.create(integration=self.integration, external_id="gone-past", client=self.alice, start_at=now - timedelta(days=30))
        held = set(Visit.objects.values_list("external_id", flat=True))
        seen = held - {"pv0", "gone-past"}
        self.assertEqual(self.sync()._prune_visits(seen), 1)
        self.assertFalse(Visit.objects.filter(external_id="pv0").exists())
        self.assertTrue(Visit.objects.filter(external_id="gone-past").exists())  # history stays

    def test_a_short_response_removes_nothing(self):
        held = list(Visit.objects.values_list("external_id", flat=True))
        self.assertEqual(self.sync()._prune_visits(set(held[: len(held) // 2])), 0)


class CloseDateTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Oct 13, 2026
            patcher.start()
            self.addCleanup(patcher.stop)
        self.n = 0

    def job(self, client, **fields):
        self.n += 1
        fields.setdefault("source_payload", {"completedAt": None})
        return Job.objects.create(integration=self.integration, external_id=f"cj{self.n}", client=client, title=f"{client.name} bi-weekly", is_recurring=True, **fields)

    def visits(self, job, days, price="200", complete=True):
        for day in days:
            self.n += 1
            Visit.objects.create(integration=self.integration, external_id=f"cv{self.n}", client=job.client, job=job, start_at=at(day), is_complete=complete, price_per_visit=D(price))

    def test_closed_on_uses_completed_at_then_the_last_update_for_older_syncs(self):
        closed = Job(job_status="archived", completed_at=aware(date(2026, 9, 22)), source_payload={"completedAt": "x"})
        self.assertEqual(closed_on(closed), date(2026, 9, 22))
        old_sync = Job(job_status="archived", source_updated_at=aware(date(2026, 9, 22)), source_payload={"id": "x"})
        self.assertEqual(closed_on(old_sync), date(2026, 9, 22))
        self.assertIsNone(closed_on(Job(job_status="archived", source_payload={"completedAt": None})))
        self.assertIsNone(closed_on(Job(job_status="active", completed_at=aware(date(2026, 9, 22)))))

    def test_a_closed_job_ends_on_the_close_date_even_with_visits_left_over(self):
        job = Job(job_status="archived", completed_at=aware(date(2026, 9, 22)), source_payload={"completedAt": "x"})
        rows = [VisitRow(date(2026, 9, 2), D(180), True), VisitRow(date(2026, 9, 16), D(180), True), VisitRow(date(2026, 11, 4), D(180), False)]
        det = detect(job, rows, TODAY)
        self.assertEqual((det.status, det.end_date), (STATUS_CANCELLED, date(2026, 9, 22)))
        cleanup = Job(job_status="archived", completed_at=aware(date(2026, 9, 22)), source_payload={"completedAt": "x"})
        old_rows = [VisitRow(date(2025, 1, 6), D(180), True), VisitRow(date(2025, 3, 3), D(180), True)]
        self.assertEqual(detect(cleanup, old_rows, TODAY).end_date, date(2025, 3, 3))  # closed 18 months later: a clean-up
        open_job = Job(job_status="active", source_payload={"completedAt": None})
        self.assertEqual(detect(open_job, rows, TODAY).status, STATUS_ACTIVE)

    def test_lost_mrr_and_cancelled_jobs_count_in_the_close_month(self):
        gone = Client.objects.create(integration=self.integration, external_id="cg", name="Sandra Weir")
        sandra = self.job(gone, job_status="archived", completed_at=aware(date(2026, 9, 14)))
        self.visits(sandra, [date(2026, 6, 8), date(2026, 6, 22), date(2026, 7, 6), date(2026, 7, 20)])  # last cleaning in July
        switch = Client.objects.create(integration=self.integration, external_id="cs", name="New Frequency")
        old = self.job(switch, job_status="archived", completed_at=aware(date(2026, 9, 10)))
        self.visits(old, [date(2026, 7, 1), date(2026, 7, 15), date(2026, 8, 1), date(2026, 8, 15), date(2026, 9, 1)])
        new = self.job(switch, job_status="active")
        self.visits(new, [date(2026, 9, 12), date(2026, 9, 26), date(2026, 10, 10)])
        self.visits(new, [date(2026, 10, 24), date(2026, 11, 7)], complete=False)
        CancellationRecord.objects.create(  # logged as a cancelled visit, not a cancelled job
            integration=self.integration, jobber_task=JobberTask.objects.create(integration=self.integration, external_id="t-sw", title="Cancelled Visit"),
            jobber_task_external_id="t-sw", cancellation_type=CANCELLATION_VISIT, client=gone, task_date=date(2026, 9, 14), reason="client_cancellation",
        )
        rebuild_mrr(self.integration, today=TODAY)
        plan = RecurringPlan.objects.get(job=sandra)
        self.assertEqual((plan.detected_status, plan.end_date), (STATUS_CANCELLED, date(2026, 9, 14)))

        metrics = Metrics(self.integration)
        september, july = periods.build("month", date(2026, 9, 1)), periods.build("month", date(2026, 7, 1))
        self.assertEqual(metrics.value("cancelled_recurring_clients", september), 1)  # not the frequency change
        july_lost = [r["client"] for r in HANDLERS["lost_recurring_clients"](metrics, july)["rows"]]
        self.assertNotIn("Sandra Weir", july_lost)  # not the last-cleaning month
        self.assertEqual(metrics.value("mrr_churned", september), plan.effective_monthly_value)

        brief = Client.objects.create(integration=self.integration, external_id="cb", name="Started And Stopped")
        short = self.job(brief, job_status="archived", completed_at=aware(date(2026, 9, 22)))
        self.visits(short, [date(2026, 9, 2), date(2026, 9, 16)])  # inside one month: no month-end MRR
        rebuild_mrr(self.integration, today=TODAY)
        metrics = Metrics(self.integration)
        self.assertEqual(metrics.value("cancelled_recurring_clients", september), 2)
        listed = HANDLERS["cancelled_recurring_clients"](metrics, september)["rows"]
        self.assertEqual([(r["client"], r["closed"]) for r in listed], [("Sandra Weir", date(2026, 9, 14)), ("Started And Stopped", date(2026, 9, 22))])

        drill = HANDLERS["lost_recurring_clients"](metrics, september)
        self.assertEqual([r["client"] for r in drill["rows"]], ["Sandra Weir"])
        self.assertEqual(drill["rows"][0]["last_visit"], date(2026, 7, 20))
        self.assertTrue(drill["rows"][0]["reason"])  # from the "Cancelled visit" task, a secondary signal

        week = periods.build("week", date(2026, 9, 13))  # Sep 13-19
        self.assertEqual(Metrics(self.integration).value("cancelled_recurring_clients", week), 1)
