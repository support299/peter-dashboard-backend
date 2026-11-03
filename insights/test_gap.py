from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from analytics.hub_models import HubComplaint, HubEmployee, HubLateArrival, HubWorkHours
from analytics.pricing_models import PricingSubmission
from analytics.sync import _upsert_complaints, _upsert_late_arrivals
from financials import ledger
from financials.models import BalanceEntry, Category, Expense, FinancialAccount
from insights import periods
from insights.alerts import additive, evaluate, monthly_limit
from insights.metrics import Metrics
from insights.models import AlertRule, InsightsSetting, RecurringPlan
from insights.rebuild import rebuild_insights
from insights.team_leaders import assign_team_leaders, split_hours, team_leader_table
from insights.tests import TODAY, MasterDashboardDataMixin, at
from insights.trends import series
from operations.models import Client, Employee, Job, Quote, Visit, VisitAssignment

SEPTEMBER = date(2026, 9, 1)


class GapTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Oct 13
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        self.boss = get_user_model().objects.create_superuser("boss", "b@x.ca", "pw")
        self.client.force_login(self.boss)
        self.metrics = Metrics(self.integration)
        self.metrics.finance_visible = True
        self.september = periods.build("month", SEPTEMBER)

    def person(self, ext, name, position=""):
        employee = Employee.objects.create(integration=self.integration, external_id=ext, full_name=name)
        HubEmployee.objects.create(external_id=f"h-{ext}", name=name, position=position, jobber_id=ext)
        return employee

    def visit(self, ext, day, people=(), minutes=60, **fields):
        fields.setdefault("is_complete", True)
        fields.setdefault("client", self.alice)
        v = Visit.objects.create(integration=self.integration, external_id=ext, start_at=at(day), duration_minutes=minutes, **fields)
        for person in people:
            VisitAssignment.objects.create(visit=v, employee=person)
        return v

    def hours(self, person, day, amount):
        HubWorkHours.objects.create(external_id=f"{person.external_id}-{day}", date=day, jobber_user_id=person.external_id, hours_worked=amount)

    # ——— periods ———
    def test_presets(self):
        p = periods.preset("last_3y")
        self.assertEqual((p.start, p.end), (date(2023, 10, 14), TODAY))
        self.assertIn("Last 3 years", p.label)
        self.assertEqual(set(periods.comparisons(p)), {"previous", "last_year"})
        life = periods.preset("lifetime")
        self.assertEqual(life.start, date(2026, 2, 10))  # first visit
        self.assertEqual(periods.comparisons(life), {})

    def test_lifetime_starts_at_the_first_visit_when_that_comes_first(self):
        self.assertEqual(periods.preset("lifetime").start, date(2026, 2, 10))  # Bob's first visit, before any invoice in a fresh set

    def test_finance_pages_answer_for_presets(self):
        for kind in ("last_3y", "last_5y", "lifetime"):
            for page in ("summary", "pnl", "cash", "report"):
                with self.subTest(kind=kind, page=page):
                    response = self.client.get(f"/api/financials/{page}/?period={kind}")
                    self.assertEqual(response.status_code, 200, response.content[:200])

    def test_every_page_answers_for_presets(self):
        for kind in ("last_3y", "last_5y", "lifetime"):
            for page in ("overview", "revenue", "clients", "mrr", "cancellations", "quality", "sales", "operations", "alerts"):
                with self.subTest(kind=kind, page=page):
                    self.assertEqual(self.client.get(f"/api/insights/{page}/?period={kind}").status_code, 200)

    # ——— late arrivals ———
    def test_late_arrivals_by_period(self):
        late = {"id": "l1", "submission_id": "s1", "date": "2026-09-08", "employee_id": "e1", "employee_name": "Rose", "jobber_user_id": "U1", "comment": "Traffic", "submitted_at": "2026-09-08T12:00:00Z"}
        _upsert_late_arrivals([late, {**late, "id": "l2", "date": "2026-10-02"}, {"id": "", "date": "2026-10-02"}])
        self.assertEqual(HubLateArrival.objects.count(), 2)
        self.assertEqual(self.metrics.value("late_arrivals", self.september), 1)
        data = self.client.get("/api/insights/operations/?period=month&anchor=2026-09-01").json()
        card = next(c for c in data["people"] if c["key"] == "late_arrivals")
        self.assertEqual((card["available"], card["current"]), (True, 1))
        self.assertEqual(next(r for r in data["employees"] if r["employee"] == "Rose")["late"], 1)
        drill = self.client.get("/api/insights/drill/?metric=late_arrivals&period=month&anchor=2026-09-01").json()
        self.assertEqual(drill["rows"][0]["comment"], "Traffic")
        self.assertIsNone(Metrics(self.integration, "residential").value("late_arrivals", self.september))

    # ——— team leaders ———
    def test_visit_leader_is_the_one_team_leader_assigned(self):
        lee, ann = self.person("U1", "Victoria Lee", "Team Leader"), self.person("U2", "Leanna R", "team leader")
        cleaner = self.person("U3", "Rose", "Cleaning Technician")
        one = self.visit("v1", SEPTEMBER, [lee, cleaner])
        two = self.visit("v2", SEPTEMBER, [lee, ann])
        none = self.visit("v3", SEPTEMBER, [cleaner], team_leader=lee)  # a stale value is cleared
        stats = assign_team_leaders(self.integration)
        one.refresh_from_db(), two.refresh_from_db(), none.refresh_from_db()
        self.assertEqual((one.team_leader, two.team_leader, none.team_leader), (lee, None, None))
        self.assertEqual((stats["visits_with_leader"], stats["more_than_one_leader"]), (1, 1))

    def test_hours_split_by_visit_length_and_tie_to_labour_hours(self):
        lee, ann = self.person("U1", "Victoria", "Team Leader"), self.person("U2", "Leanna", "Team Leader")
        cleaner = self.person("U3", "Rose")
        day = date(2026, 9, 8)
        self.visit("v1", day, [lee, cleaner], minutes=120)
        self.visit("v2", day, [ann, cleaner], minutes=360)
        self.hours(cleaner, day, D(8))
        self.hours(self.person("U4", "Nobody"), day, D(5))  # no visit that day
        assign_team_leaders(self.integration)
        split = split_hours(self.metrics, self.september)
        self.assertEqual((split["U1"], split["U2"], split[""]), (D(2), D(6), D(5)))
        rows = team_leader_table(self.metrics, self.september)
        self.assertEqual(sum(r["hours"] for r in rows), self.metrics.value("labour_hours", self.september))
        self.assertEqual(rows[-1]["team_leader"], "No team leader")

    def test_rounded_hours_still_add_up_to_the_card(self):
        lee, ann = self.person("U1", "Victoria", "Team Leader"), self.person("U2", "Leanna", "Team Leader")
        cleaner = self.person("U3", "Rose")
        day = date(2026, 9, 8)
        self.visit("v1", day, [lee, cleaner], minutes=60)
        self.visit("v2", day, [ann, cleaner], minutes=60)
        self.visit("v3", day, [cleaner], minutes=60)
        self.hours(cleaner, day, D("1.00"))
        assign_team_leaders(self.integration)
        rows = team_leader_table(self.metrics, self.september)
        self.assertEqual(sum(r["hours"] for r in rows), D("1.00"))
        self.assertTrue(all(r["hours"] is not None for r in rows))

    def test_table_counts_complaints_and_touch_ups_and_ties_to_cards(self):
        lee = self.person("U1", "Victoria", "Team Leader")
        self.visit("tu2", date(2026, 9, 9), [lee, self.person("U3", "Rose")], minutes=90, title="Touch up", is_touch_up=True)
        assign_team_leaders(self.integration)
        row = {"submitted_at": "2026-09-10T12:00:00Z", "incident_date": "2026-09-10", "client_jobber_id": "c1", "status": "open"}
        _upsert_complaints([{**row, "id": "1", "team_leader": {"id": "h", "name": "Victoria", "jobber_user_id": "U1"}}, {**row, "id": "2", "team_leader": None}])
        self.assertEqual(HubComplaint.objects.get(external_id="1").team_leader["name"], "Victoria")
        rows = {r["team_leader"]: r for r in team_leader_table(self.metrics, self.september)}
        self.assertEqual((rows["Victoria"]["complaints"], rows["No team leader"]["complaints"]), (1, 1))
        self.assertEqual((rows["Victoria"]["touch_ups"], rows["Victoria"]["touch_up_cost"]), (1, D("90.00")))
        total = sum(r["touch_ups"] for r in rows.values())
        self.assertEqual(total, self.metrics.value("touch_ups", self.september))
        self.assertEqual(sum(r["touch_up_cost"] for r in rows.values()), self.metrics.value("touch_up_cost", self.september))

    # ——— new numbers ———
    def test_touch_up_cost_is_hours_times_30(self):
        people = [self.person("U1", "A"), self.person("U2", "B")]
        self.visit("tu2", date(2026, 9, 9), people, minutes=90, title="Touch-up", is_touch_up=True)
        self.visit("tu3", date(2026, 9, 10), [], minutes=30, title="Touch up", is_touch_up=True)  # nobody assigned: still one person
        self.visit("tu4", date(2026, 9, 11), people, minutes=60, title="Touch up", is_touch_up=True, is_complete=False)  # not done yet
        self.assertEqual(self.metrics.value("touch_up_hours", self.september), D("3.50"))
        self.assertEqual(self.metrics.value("touch_up_cost", self.september), D("105.00"))

    def test_average_new_client_value_uses_the_first_visit_only(self):
        def new_client(ext, price):
            client = Client.objects.create(integration=self.integration, external_id=ext, name=ext)
            self.visit(f"{ext}-1", date(2026, 9, 3), client=client, price_per_visit=price)
            self.visit(f"{ext}-2", date(2026, 9, 17), client=client, price_per_visit=D(999))

        new_client("n1", D(100))
        new_client("n2", D(300))
        new_client("n3", D(0))  # no price: left out, not counted as $0
        new_client("n4", None)
        self.assertEqual(self.metrics.value("new_clients", self.september), 4)
        self.assertEqual(self.metrics.value("avg_new_client_value", self.september), D("200.00"))

    def test_average_new_client_value_with_monthly_invoice_share(self):
        client = Client.objects.create(integration=self.integration, external_id="n1", name="Globe")
        job = Job.objects.create(integration=self.integration, external_id="gj", client=client, title="Globe")
        RecurringPlan.objects.create(integration=self.integration, job=job, client=client, name="Globe", pricing_source="invoice", monthly_value=D(4000), visits_per_month=D(4))
        self.visit("g1", date(2026, 9, 3), client=client, job=job, price_per_visit=D(4880))
        self.assertEqual(self.metrics.value("avg_new_client_value", self.september), D("1000.00"))

    def test_touch_up_hours_use_start_and_end_without_a_duration(self):
        visit = self.visit("tu2", date(2026, 9, 9), minutes=None, title="Touch up", is_touch_up=True)
        Visit.objects.filter(pk=visit.pk).update(end_at=at(date(2026, 9, 9), 12))
        self.assertEqual(self.metrics.value("touch_up_hours", self.september), D("2.00"))

    def test_alert_limits_scale_only_for_totals(self):
        self.assertFalse(additive("avg_new_client_value"))
        self.assertTrue(additive("touch_up_hours"))
        self.assertTrue(additive("touch_up_cost"))
        self.assertTrue(additive("late_arrivals"))

    def test_limits_on_totals_are_monthly(self):
        whole = periods.build("year", date(2025, 6, 1))
        self.assertEqual(monthly_limit(D(100), whole), D(1200))
        self.assertEqual(monthly_limit(D(100), periods.build("quarter", date(2026, 7, 1))), D(300))
        self.assertEqual(monthly_limit(D(100), periods.build("month", date(2026, 9, 1))), D(100))
        running = periods.build("quarter", TODAY)  # Oct 1-13 of a 92-day quarter
        self.assertEqual(monthly_limit(D(92), running), D("39.00"))
        years = monthly_limit(D(100), periods.preset("last_3y"))
        self.assertTrue(D(3590) < years < D(3610), years)
        self.assertEqual(monthly_limit(D(100), periods.build("custom", date(2026, 7, 1), end=date(2026, 8, 31))), D(200))
        two_weeks = periods.build("custom", date(2026, 9, 1), end=date(2026, 9, 14))
        self.assertEqual(monthly_limit(D(100), two_weeks), D("46.00"))

    def test_monthly_limit_does_not_fire_over_a_year(self):
        year = periods.build("year", date(2025, 1, 1))
        rule = AlertRule(metric="visits_completed", condition="above", threshold=D(50))
        Visit.objects.bulk_create(
            [Visit(integration=self.integration, external_id=f"y{n}", client=self.alice, start_at=at(date(2025, 3, 1)), is_complete=True) for n in range(100)]
        )
        result = evaluate(self.metrics, year, {}, rules=[rule])[0]  # 100 in a year is under 50 a month
        self.assertFalse(result["hit"])
        self.assertEqual(result["limit"], D(600))
        self.assertIn("monthly limit", result["message"])
        rate = evaluate(self.metrics, year, {}, rules=[AlertRule(metric="touch_up_rate", condition="above", threshold=D(5))])[0]
        self.assertNotIn("monthly limit", rate["message"])

    def test_a_month_without_expenses_is_not_all_profit(self):
        category = Category.objects.filter(group="opex", parent__isnull=False, in_profit_and_loss=True).first() or Category.objects.filter(group="opex").first()
        Expense.objects.create(date=date(2026, 8, 5), category=category, amount=D(100), total=D(100))
        self.assertIsNone(self.metrics.value("net_profit", self.september))  # expenses exist, none in September
        card = self.metrics.card("net_profit", self.september, {})
        self.assertIn("No expenses entered for this period", card["note"])
        self.assertIsNotNone(self.metrics.value("total_expenses", self.september))
        points = {p["start"][:7]: p["value"] for p in series(self.metrics, "net_profit")["points"]}
        self.assertIsNone(points["2026-09"])
        self.assertIsNotNone(points["2026-08"])
        august_and_september = periods.build("custom", date(2026, 8, 1), end=date(2026, 9, 30))
        self.assertEqual(self.metrics.value("total_expenses", august_and_september), D(100))

    def test_drive_time_cost_uses_each_rate_and_skips_contractors_and_leaders(self):
        def staff(ext, role="employee", position="Cleaning Technician", rate=None):
            HubEmployee.objects.create(external_id=ext, name=ext, role=role, position=position, rates={"drive_time": rate, "regular": "25.00"})

        staff("e-rate", rate="20.00")
        staff("e-none")  # no drive rate: $18
        staff("e-contractor", role="contractor", rate="20.00")
        staff("e-leader", position="Team Leader", rate="22.00")
        staff("e-office", position="Marketing Manager", rate="20.00")
        staff("e-admin", role="admin", position="", rate="20.00")
        staff("e-blank", position="", rate="20.00")  # no position: not on the cleaners list
        staff("e-specialist", position="Specialty Cleaner")
        day = date(2026, 9, 8)
        for ext in ("e-rate", "e-none", "e-contractor", "e-leader", "e-office", "e-admin", "e-blank", "e-specialist", "e-unknown"):
            HubWorkHours.objects.create(external_id=f"d-{ext}", date=day, employee_external_id=ext, hours_worked=D(8), drive_time_hours=D(2))
        # Cleaners only: 2 × $20 + 2 × $18 + 2 × $18 (specialty cleaner without a rate)
        self.assertEqual(self.metrics.value("drive_time_cost", self.september), D("112.00"))
        self.assertIsNone(self.metrics.value("drive_time_cost", periods.build("month", date(2026, 8, 1))))  # before any hours

    def test_cost_percentages(self):
        parts = {key: D(0) for key in ledger.PARTS} | {"cogs": D(40), "opex": D(25)}
        self.assertEqual(ledger.kpi("cogs_pct", parts, D(200)), D("20.0"))
        self.assertEqual(ledger.kpi("opex_pct", parts, D(200)), D("12.5"))
        self.assertTrue({"cogs_pct", "opex_pct"} <= ledger.REVENUE_KPIS)

    def test_estimates_and_quotes_are_counted_separately(self):
        def quote(ext, email, status="sent"):
            PricingSubmission.objects.create(external_id=ext, customer_email=email, status=status, source_created_at=at(date(2026, 9, 5)))

        quote("p1", "real@mail.ca")
        quote("p2", "real@mail.ca")  # same person: still two estimates
        quote("p3", "test@test.com")
        quote("p4", "other@mail.ca", status="draft")
        Quote.objects.create(integration=self.integration, external_id="q1", client=self.alice, quote_status="awaiting_response", sent_at=at(date(2026, 9, 6)))
        Quote.objects.create(integration=self.integration, external_id="q2", client=self.alice, quote_status="draft", sent_at=at(date(2026, 9, 6)))
        Quote.objects.create(integration=self.integration, external_id="q3", client=self.alice, quote_status="approved", sent_at=at(date(2026, 8, 6)))
        Quote.objects.create(integration=self.integration, external_id="q4", client=self.alice, quote_status="sent", source_created_at=at(date(2026, 9, 7)))  # no sent date
        metrics = Metrics(self.integration)
        self.assertEqual(metrics.value("calculator_estimates", self.september), 2)
        self.assertIsNone(metrics.value("jobber_quotes", self.september))  # not synced yet is not zero
        InsightsSetting.put("jobber_sales_sync", {"quotes": {"ok": True}, "requests": {"ok": True}})
        metrics = Metrics(self.integration)
        self.assertEqual(metrics.value("jobber_quotes", self.september), 2)
        self.assertIsNone(Metrics(self.integration, "residential").value("jobber_quotes", self.september))

    def test_recurring_job_trend_matches_the_cards(self):
        monthly = {p["start"][:7]: p["value"] for p in series(self.metrics, "new_recurring_jobs")["points"]}
        for month in ("2026-02", "2026-03"):
            period = periods.build("month", date.fromisoformat(f"{month}-01"))
            self.assertEqual(monthly[month], self.metrics.value("new_recurring_jobs", period))
        lost = {p["start"][:7]: p["value"] for p in series(self.metrics, "cancelled_recurring_jobs")["points"]}
        self.assertEqual(sum(v or 0 for v in lost.values()), self.metrics.value("cancelled_recurring_jobs", periods.preset("last_3y")))

    def test_opening_and_closing_cash(self):
        account = FinancialAccount.objects.create(name="Bank", kind="bank")
        BalanceEntry.objects.create(account=account, date=date(2026, 8, 31), balance=D(1000))
        BalanceEntry.objects.create(account=account, date=date(2026, 9, 30), balance=D(1500))
        data = self.client.get("/api/financials/cash/?period=month&anchor=2026-09-01").json()
        self.assertEqual((data["totals"]["opening"]["cash"], data["totals"]["balance"]["cash"]), (1000.0, 1500.0))
        self.assertEqual(data["opening_missing"], 0)
        late = FinancialAccount.objects.create(name="New bank", kind="bank")
        BalanceEntry.objects.create(account=late, date=date(2026, 9, 15), balance=D(50))
        data = self.client.get("/api/financials/cash/?period=month&anchor=2026-09-01").json()
        self.assertEqual((data["totals"]["opening"]["cash"], data["opening_missing"]), (1000.0, 1))

    # ——— alerts comparing two numbers ———
    def rule(self, condition="above_metric", metric="mrr_churned", other="mrr_new"):
        return AlertRule(metric=metric, condition=condition, compare_metric=other)

    def test_alert_compares_two_numbers(self):
        comps = periods.comparisons(self.september)
        self.assertGreater(self.metrics.value("visits_completed", self.september), self.metrics.value("touch_ups", self.september))
        higher = self.rule(metric="visits_completed", other="touch_ups")
        result = evaluate(self.metrics, self.september, comps, rules=[higher])[0]
        self.assertTrue(result["hit"])
        self.assertIn("Touch-up / redo visits", result["message"])
        lower = self.rule("below_metric", metric="visits_completed", other="touch_ups")
        self.assertFalse(evaluate(self.metrics, self.september, comps, rules=[lower])[0]["hit"])

    def test_comparison_is_not_scaled_and_missing_data_is_not_a_hit(self):
        rule = self.rule(metric="revenue", other="net_profit")
        result = evaluate(self.metrics, self.september, periods.comparisons(self.september), rules=[rule])[0]
        self.assertIsNone(result["hit"])  # no expenses entered
        self.assertIn("No data", result["message"])

    def test_comparison_alert_validation(self):
        def post(status, **body):
            response = self.client.post("/api/insights/alerts/", {"metric": "mrr_churned", "condition": "above_metric", **body}, content_type="application/json")
            self.assertEqual(response.status_code, status, response.content[:200])
            return response.json()

        post(400)
        post(400, compare_metric="mrr_churned")
        post(400, compare_metric="visits_completed")  # dollars vs a count
        rule = AlertRule.objects.get(pk=post(200, compare_metric="mrr_new")["id"])
        self.assertEqual((rule.compare_metric, rule.threshold), ("mrr_new", D(0)))
        response = self.client.patch(f"/api/insights/alerts/{rule.id}/", {"is_active": False}, content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_finance_comparison_hidden_without_access(self):
        metrics = Metrics(self.integration)
        rule = self.rule(metric="revenue", other="total_expenses")
        self.assertEqual(evaluate(metrics, self.september, periods.comparisons(self.september), rules=[rule]), [])
