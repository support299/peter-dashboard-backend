from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from analytics.hub_models import AdminInternalSyncRun, HubContractorPay
from analytics.sync import persist_payload
from financials import contractor_pay
from financials.models import SOURCE_HUB, Category, Expense, FinanceAccess, MonthlyTotal, MonthReview
from insights.tests import TODAY, MasterDashboardDataMixin
from operations.models import Visit

D = Decimal
START = "2026-09-06"  # a Sunday


def line(rid, kind, day, amount, contractor="h-lesly", visit="", **extra):
    return {
        "id": rid, "type": kind, "date": day, "amount_paid": amount, "calculated_amount": extra.pop("calculated", amount),
        "status": extra.pop("status", "active"), "week_start": "2026-09-06", "week_paid": extra.pop("week_paid", True),
        "pay_rule": extra.pop("pay_rule", "percent"), "changed_by": "", "reason": "", "flags": extra.pop("flags", []),
        "contractor_id": contractor, "contractor_name": {"h-lesly": "Lesly", "h-jaime": "Jaime Montoya"}.get(contractor, contractor),
        "jobber_user_id": "", "jobber_visit_id": visit, "client_name": "A client", "job_title": "A job",
    }


def persist(rows, available=True):
    payload = {"meta": {"from": "2026-08-01", "to": "2026-10-31"}, "contractor_pay": {"available": available, "records": rows}}
    persist_payload(payload, AdminInternalSyncRun.objects.create())


@override_settings(CONTRACTOR_PAY_START=START)
class ContractorPayTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.subcontractors = Category.objects.get(system_key="subcontractors")
        Visit.objects.create(integration=self.integration, external_id="V-office", client=self.dental, title="Office", division="commercial")

    def hub_expenses(self):
        return {(e.date, e.division): e.amount for e in Expense.objects.filter(source=SOURCE_HUB)}

    def test_subcontractors_counts_as_labour_not_payroll(self):
        self.assertTrue(self.subcontractors.counts_as_labour)
        self.assertFalse(self.subcontractors.counts_as_payroll)

    def test_sync_stores_lines_and_updates_by_id(self):
        persist([line("pay:V1:h-lesly", "pay", "2026-09-08", "60.00", visit="V1", flags={"missing_percent": False, "override": True})])
        persist([line("pay:V1:h-lesly", "pay", "2026-09-08", "72.00", visit="V1")])
        self.assertEqual(HubContractorPay.objects.count(), 1)
        pay = HubContractorPay.objects.get()
        self.assertEqual((pay.amount_paid, pay.line_type, pay.contractor_name), (D("72.00"), "pay", "Lesly"))
        persist([], available=False)  # older Hub: keep what we have
        self.assertEqual(HubContractorPay.objects.count(), 1)

    def test_daily_expense_per_division_from_start_date_without_deductions(self):
        persist([
            line("pay:V1:h-lesly", "pay", "2026-09-08", "60.00", visit="V1"),
            line("tip:V1:h-lesly", "tip", "2026-09-08", "10.00", visit="V1"),
            line("pay:V-office:h-jaime", "pay", "2026-09-08", "100.00", contractor="h-jaime", visit="V-office"),
            line("adj:7", "adjustment", "2026-09-09", "5.00"),
            line("ded:supplies:h-lesly:2026-09-12", "supplies_deduction", "2026-09-12", "-25.00"),
            line("ded:3", "restock_deduction", "2026-09-10", "-15.00"),
            line("pay:V0:h-lesly", "pay", "2026-09-05", "50.00", visit="V0"),  # before the start date
            line("pay:V2:h-lesly", "pay", "2026-09-10", "0.00", visit="V2", flags=["missing_percent"]),
            line("pay:V3:h-lesly", "pay", "2026-09-11", "40.00", visit="V3", status="void"),
        ])
        self.assertEqual(
            self.hub_expenses(),
            {(date(2026, 9, 8), "shared"): D("70.00"), (date(2026, 9, 8), "commercial"): D("100.00"), (date(2026, 9, 9), "shared"): D("5.00")},
        )
        expense = Expense.objects.get(source=SOURCE_HUB, date=date(2026, 9, 8), division="commercial")
        self.assertEqual((expense.category, expense.gst, expense.status), (self.subcontractors, D(0), "paid"))

        # Run again: nothing changes. Then the visit is voided and the amount goes away.
        before = set(Expense.objects.filter(source=SOURCE_HUB).values_list("id", flat=True))
        self.assertEqual(contractor_pay.book_expenses()["created"], 0)
        self.assertEqual(before, set(Expense.objects.filter(source=SOURCE_HUB).values_list("id", flat=True)))
        persist([line("pay:V1:h-lesly", "pay", "2026-09-08", "60.00", visit="V1", status="void")])
        self.assertNotIn((date(2026, 9, 8), "shared"), self.hub_expenses())

    def test_open_week_is_still_a_paid_cost_with_no_receipt_needed(self):
        persist([line("pay:V1:h-lesly", "pay", "2026-10-06", "60.00", visit="V1", week_paid=False, status="ACTIVE")])
        self.assertEqual(Expense.objects.get(source=SOURCE_HUB).status, "paid")  # never stuck "unpaid" in a reviewed month
        from financials.views import review_hints

        hints = review_hints(date(2026, 10, 1))
        self.assertEqual((hints["missing_receipts"], hints["unpaid"]), (0, 0))

    @override_settings(CONTRACTOR_PAY_START="")
    def test_off_without_a_start_date(self):
        persist([line("pay:V1:h-lesly", "pay", "2026-09-08", "60.00", visit="V1")])
        self.assertFalse(Expense.objects.filter(source=SOURCE_HUB).exists())
        self.assertEqual(contractor_pay.duplicate_warning(date(2026, 10, 1), self.subcontractors.id), "")

    def test_reviewed_month_is_left_alone_and_the_difference_is_booked_today(self):
        persist([line("pay:V1:h-lesly", "pay", "2026-09-29", "60.00", visit="V1", week_paid=False)])
        MonthReview.objects.create(month=date(2026, 9, 1), status=MonthReview.STATUS_REVIEWED)
        persist([line("pay:V1:h-lesly", "pay", "2026-09-29", "75.00", visit="V1")])
        self.assertEqual(self.hub_expenses(), {(date(2026, 9, 29), "shared"): D("60.00"), (TODAY, "shared"): D("15.00")})
        late = Expense.objects.get(source=SOURCE_HUB, date=TODAY)
        self.assertIn("reviewed month", late.notes)
        persist([line("pay:V1:h-lesly", "pay", "2026-09-29", "75.00", visit="V1")])  # again: no double booking
        self.assertEqual(Expense.objects.filter(source=SOURCE_HUB).count(), 2)
        persist([line("pay:V1:h-lesly", "pay", "2026-09-29", "50.00", visit="V1")])  # lower again: today's line goes negative
        self.assertEqual(self.hub_expenses()[(TODAY, "shared")], D("-10.00"))

        # September reopened: the day holds the full amount again.
        MonthReview.objects.filter(month=date(2026, 9, 1)).update(status=MonthReview.STATUS_OPEN)
        contractor_pay.book_expenses()
        self.assertEqual(self.hub_expenses(), {(date(2026, 9, 29), "shared"): D("50.00")})

    def test_hand_entered_subcontractors_are_flagged_after_the_start(self):
        users = get_user_model().objects
        owner = users.create_superuser("owner", "owner@example.com", "pw")
        self.client.force_login(owner)
        body = {"date": "2026-10-02", "vendor": "Lesly", "amount": "300", "category_id": self.subcontractors.id}
        blocked = self.client.post("/api/financials/expenses/", body, content_type="application/json")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("counted twice", blocked.json()["error"])
        saved = self.client.post("/api/financials/expenses/", {**body, "confirm_duplicate": True}, content_type="application/json").json()["expense"]
        self.assertIn("counted twice", saved["contractor_pay_warning"])
        early = {**body, "date": "2026-09-01"}
        self.assertEqual(self.client.post("/api/financials/expenses/", early, content_type="application/json").status_code, 200)

        total = MonthlyTotal.objects.create(month=date(2026, 9, 1), category=self.subcontractors, amount=D(900))
        self.assertIn("counted twice", contractor_pay.monthly_total_warning(total))
        old = MonthlyTotal.objects.create(month=date(2026, 8, 1), category=self.subcontractors, amount=D(900))
        self.assertEqual(contractor_pay.monthly_total_warning(old), "")

    def test_automatic_expenses_cannot_be_edited_and_page_shows_pay(self):
        persist([
            line("pay:V1:h-lesly", "pay", "2026-10-06", "60.00", visit="V1", week_paid=False),
            line("tip:V1:h-lesly", "tip", "2026-10-06", "10.00", visit="V1", week_paid=False),
            line("pay:V2:h-lesly", "pay", "2026-10-07", "0.00", visit="V2", flags=["missing_percent"], week_paid=False),
            line("ded:supplies:h-lesly:2026-10-10", "supplies_deduction", "2026-10-10", "-25.00", week_paid=False),
            line("pay:V-office:h-jaime", "pay", "2026-10-07", "100.00", contractor="h-jaime", visit="V-office"),
            line("pay:V1:h-jaime", "pay", "2026-10-06", "0.00", contractor="h-jaime", visit="V1"),  # shared visit: counted once
            line("bonus:1", "bonus", "2026-10-06", "999.00"),  # unknown type: not on the page, not in expenses
        ])
        owner = get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")
        clerk = get_user_model().objects.create_user("clerk", "clerk@example.com", "pw")
        FinanceAccess.objects.create(user=clerk, role="entry")
        self.client.force_login(owner)
        expense = Expense.objects.filter(source=SOURCE_HUB).first()
        response = self.client.patch(f"/api/financials/expenses/{expense.id}/", {"amount": "1"}, content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.delete(f"/api/financials/expenses/{expense.id}/").status_code, 409)

        page = self.client.get("/api/financials/contractor-pay/?period=month&anchor=2026-10-01").json()
        lesly = next(r for r in page["rows"] if r["contractor"] == "Lesly")
        self.assertEqual((lesly["visits"], lesly["pay"], lesly["tips"], lesly["deductions"]), (2, 60.0, 10.0, -25.0))
        self.assertEqual((lesly["expense"], lesly["net"], lesly["missing_percent"]), (70.0, 45.0, 1))
        self.assertEqual((page["totals"]["expense"], page["booked"], page["totals"]["unpaid"]), (170.0, 170.0, 70.0))
        self.assertEqual(page["totals"]["visits"], 3)
        detail = self.client.get("/api/financials/contractor-pay/?period=month&anchor=2026-10-01&contractor=h-lesly").json()
        self.assertEqual(len(detail["lines"]), 4)

        self.client.force_login(clerk)
        self.assertEqual(self.client.get("/api/financials/contractor-pay/").status_code, 403)
        self.assertEqual(self.client.get("/api/financials/expenses/").json()["results"], [])
