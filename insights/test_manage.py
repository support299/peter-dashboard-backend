from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from analytics.mrr_models import MrrBooking
from insights import periods
from insights.alerts import evaluate
from insights.metrics import Metrics
from insights.models import AlertRule, InsightsSetting, RecurringPlan
from insights.mrr_merge import link_bookings
from insights.rebuild import rebuild_insights
from insights.tests import TODAY, MasterDashboardDataMixin
from operations.models import InvoiceLineItem, LineItemRule


class ManageTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Oct 13
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        self.boss = get_user_model().objects.create_user("boss", "b@x.ca", "pw", is_staff=True)
        self.client.force_login(self.boss)

    def post(self, url, data, status=200, method="post"):
        response = getattr(self.client, method)(url, data, content_type="application/json")
        self.assertEqual(response.status_code, status, response.content[:300])
        return response.json()

    # ——— alerts ———
    def test_limits_scale_for_a_month_in_progress(self):
        m = Metrics(self.integration)
        october = periods.build("month", TODAY)  # Oct 1–13 of 31 days
        revenue = m.value("revenue", october)  # $449
        rule = AlertRule(metric="revenue", condition=AlertRule.BELOW, threshold=D(1000))
        result = evaluate(m, october, periods.comparisons(october), rules=[rule])[0]
        # $1,000 a month → $419.35 for 13 days: $449 is above it, so no alert yet.
        self.assertEqual(result["limit"], D("419.35"))
        self.assertEqual((result["hit"], revenue), (False, D("449.00")))
        september = periods.build("month", date(2026, 9, 1))
        self.assertTrue(evaluate(m, september, periods.comparisons(september), rules=[rule])[0]["hit"] is False)  # $1,220

    def test_change_alert_and_card_flag(self):
        self.post("/api/insights/alerts/", {"metric": "revenue", "condition": "down_pct", "threshold": "50", "compare_to": "previous"})
        data = self.client.get("/api/insights/overview/?period=month").json()
        # Oct 1–13 ($449) vs Sep 1–13 ($320 … invoices on the 15th and 20th fall outside): up, not down.
        self.assertEqual(data["alerts"], [])
        self.post("/api/insights/alerts/", {"metric": "revenue", "condition": "above", "threshold": "100"})
        data = self.client.get("/api/insights/overview/?period=month").json()
        self.assertEqual(len(data["alerts"]), 1)
        card = next(c for c in data["scorecard"] if c["key"] == "revenue")
        self.assertIn("above", card["alerts"][0])

    def test_alert_validation_and_permissions(self):
        self.post("/api/insights/alerts/", {"metric": "nope", "condition": "above", "threshold": "1"}, status=400)
        self.post("/api/insights/alerts/", {"metric": "revenue", "condition": "up_pct", "threshold": "0"}, status=400)
        viewer = get_user_model().objects.create_user("v", "v@x.ca", "pw")
        self.client.force_login(viewer)
        self.post("/api/insights/alerts/", {"metric": "revenue", "condition": "above", "threshold": "1"}, status=403)

    def test_finance_alerts_hidden_without_access(self):
        AlertRule.objects.create(metric="net_profit", condition=AlertRule.BELOW, threshold=D(0))
        rules = self.client.get("/api/insights/alerts/").json()["rules"]
        self.assertEqual(rules, [])

    # ——— line-item rules ———
    def test_rule_change_test_and_apply(self):
        test = self.post("/api/insights/rules/test/", {"name": "Zorblat polishing"})
        self.assertIsNone(test["rule"])
        rule = self.post("/api/insights/rules/", {"keyword": "zorblat", "division": "specialized", "revenue_category": "specialized", "priority": 5})["rule"]
        test = self.post("/api/insights/rules/test/", {"name": "Zorblat polishing"})
        self.assertEqual(test["rule"]["id"], rule["id"])
        self.assertEqual(test["results"]["recurring"]["division"], "Specialized Services")
        self.assertTrue(self.client.get("/api/insights/rules/").json()["status"]["pending_changes"])
        with mock.patch("insights.tasks.rebuild_insights_task.delay") as delay:
            self.post("/api/insights/rules/apply/", {})
        delay.assert_called_once()

    def test_unmatched_lines_listed_first(self):
        InvoiceLineItem.objects.filter(name="Tip").update(name="Mystery thing")
        lines = self.client.get("/api/insights/rules/lines/?unmatched=1").json()["lines"]
        self.assertEqual([l["name"] for l in lines], ["Mystery thing"])

    def test_nightly_rebuild_clears_pending(self):
        LineItemRule.objects.create(keyword="zzz", division="residential")
        InsightsSetting.put("line_item_rules_changed_at", {"at": "2026-10-13T00:00:00+00:00"})
        rebuild_insights(self.integration)
        self.assertFalse(self.client.get("/api/insights/rules/").json()["status"]["pending_changes"])

    # ——— old MRR tab ———
    def test_bookings_link_by_name_and_review_list(self):
        plan = RecurringPlan.objects.get(job=self.alice_job)
        MrrBooking.objects.create(name="ALICE", status="Active", visit_value=D(150), frequency="Bi-Weekly", monthly_value=D(325), start_date=date(2026, 3, 2))
        MrrBooking.objects.create(name="Zed Unknown", status="Active", monthly_value=D(200))
        MrrBooking.objects.create(name="Bob", status="Inactive", monthly_value=D(200), cancellation_reason="Moved away")
        link_bookings(self.integration)
        alice = MrrBooking.objects.get(name="ALICE")
        self.assertEqual((alice.plan, alice.plan_match), (plan, "auto"))
        data = self.client.get("/api/insights/mrr/old-tab/").json()
        self.assertEqual(data["summary"]["needs_review"], 1)
        self.assertEqual(D(str(data["summary"]["old_active_unlinked_mrr"])), D(200))
        zed = MrrBooking.objects.get(name="Zed Unknown")
        self.post(f"/api/insights/mrr/old-tab/{zed.id}/", {"ignore": True}, method="patch")
        link_bookings(self.integration)  # hand decisions survive the nightly matching
        self.assertEqual(MrrBooking.objects.get(pk=zed.pk).plan_match, "ignored")
        # The plan list shows the old tab's reason for Bob's plan.
        rows = self.client.get("/api/insights/mrr/plans/?status=cancelled").json()["results"]
        bob = next(r for r in rows if r["client"].startswith("Bob"))
        self.assertEqual(bob["old_tab"]["cancellation_reason"], "Moved away")


class ManageAuditTests(ManageTests):
    def test_alerts_not_checked_per_division(self):
        AlertRule.objects.create(metric="revenue", condition=AlertRule.ABOVE, threshold=D(1))
        data = self.client.get("/api/insights/overview/?period=month&division=rentals").json()
        self.assertEqual(data["alerts"], [])
        page = self.client.get("/api/insights/alerts/?period=month&division=rentals").json()
        self.assertTrue(page["division_note"])
        self.assertTrue(page["rules"][0]["hit"])  # the page itself shows the company-wide result

    def test_custom_range_past_today_is_scaled(self):
        m = Metrics(self.integration)
        span = periods.build("custom", date(2026, 10, 1), end=date(2026, 10, 31))  # today is Oct 13
        rule = AlertRule(metric="revenue", condition=AlertRule.BELOW, threshold=D(3100))
        self.assertEqual(evaluate(m, span, periods.comparisons(span), rules=[rule])[0]["limit"], D("1300.00"))

    def test_stuck_recalculation_offers_the_button_again(self):
        InsightsSetting.put("reclassify_status", {"state": "running", "started_at": "2026-10-01T00:00:00+00:00"})
        InsightsSetting.put("line_item_rules_changed_at", {"at": "2026-10-02T00:00:00+00:00"})
        status = self.client.get("/api/insights/rules/").json()["status"]
        self.assertEqual(status["state"], "failed")
        with mock.patch("insights.tasks.rebuild_insights_task.delay") as delay:
            self.post("/api/insights/rules/apply/", {})
        delay.assert_called_once()

    def test_financial_alert_needs_financials_access(self):
        self.post("/api/insights/alerts/", {"metric": "net_profit", "condition": "below", "threshold": "0"}, status=400)

    def test_two_bookings_on_one_plan_compared_once(self):
        plan = RecurringPlan.objects.get(job=self.alice_job)  # $325/month in Jobber
        for value in (100, 150):
            MrrBooking.objects.create(name="Alice", status="Active", monthly_value=D(value), start_date=date(2026, 3, 2))
        link_bookings(self.integration)
        data = self.client.get("/api/insights/mrr/old-tab/").json()
        self.assertEqual(D(str(data["summary"]["jobber_mrr_of_linked"])), plan.effective_monthly_value)
        self.assertEqual(data["summary"]["shared_plans"], 1)
        self.assertEqual(len(data["differences"]), 1)
        self.assertEqual(D(str(data["differences"][0]["old_tab"])), D(250))
