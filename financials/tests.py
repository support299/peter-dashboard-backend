import io
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from financials import importer, ledger, recurring
from financials.views import sync_payrun_expenses
from financials.models import (
    Category,
    Expense,
    FinanceAccess,
    MonthlyTotal,
    MonthReview,
    PayRun,
    RecurringExpense,
)
from insights.rebuild import rebuild_insights
from insights.tests import TODAY, MasterDashboardDataMixin

D = Decimal


def cat(key=None, name=None):
    return Category.objects.get(system_key=key) if key else Category.objects.get(name=name, parent=None)


def spend(day, amount, category, **kw):
    return Expense.objects.create(date=day, amount=D(amount), category=category, vendor=kw.pop("vendor", "Vendor"), **kw)


class LedgerTests(TestCase):
    def setUp(self):
        self.supplies = cat(name="Cleaning supplies")
        self.rent = cat(name="Rent and utilities")
        self.fuel = cat(name="Fuel")
        self.interest = cat("loan_interest")
        self.principal = cat("loan_principal")

    def test_groups_before_tax_and_financial_transactions(self):
        spend(date(2026, 9, 3), "100", self.supplies, gst=D("5"), qst=D("9.98"))
        spend(date(2026, 9, 4), "1000", self.rent)
        spend(date(2026, 9, 5), "80", self.fuel)
        spend(date(2026, 9, 6), "50", self.interest)
        spend(date(2026, 9, 6), "900", self.principal)  # not a cost
        parts = ledger.summary(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((parts["cogs"], parts["opex"], parts["vehicles"], parts["financial"], parts["excluded"]), (D(100), D(1000), D(80), D(50), D(900)))
        self.assertEqual(ledger.total_expenses(parts), D(1230))
        self.assertEqual(ledger.kpi("gross_profit", parts, D(5000)), D(4900))
        self.assertEqual(ledger.kpi("operating_profit", parts, D(5000)), D(3820))
        self.assertEqual(ledger.kpi("net_profit", parts, D(5000)), D(3770))
        self.assertEqual(ledger.kpi("net_margin", parts, D(5000)), D("75.4"))
        self.assertIsNone(ledger.kpi("net_profit", parts, None))

    def test_pending_recurring_charges_do_not_count(self):
        spend(date(2026, 9, 3), "100", self.rent, pending_verification=True)
        self.assertEqual(ledger.summary(date(2026, 9, 1), date(2026, 9, 30))["opex"], D(0))

    def test_monthly_total_replaces_individual_expenses_and_is_prorated(self):
        spend(date(2026, 9, 3), "40", self.supplies)
        spend(date(2026, 9, 20), "35", self.supplies)
        spend(date(2026, 9, 20), "500", self.rent)
        MonthlyTotal.objects.create(month=date(2026, 9, 1), category=self.supplies, amount=D("300"))
        full = ledger.summary(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((full["cogs"], full["opex"]), (D(300), D(500)))
        half = ledger.summary(date(2026, 9, 1), date(2026, 9, 15))
        self.assertEqual(half["cogs"], D("150.00"))
        self.assertEqual(half["opex"], D(0))


class RecurringTests(TestCase):
    def setUp(self):
        self.rec = RecurringExpense.objects.create(
            name="Office rent", amount=D("1500"), gst=D("75"), category=cat(name="Rent and utilities"), frequency="monthly", start_date=date(2026, 1, 31)
        )

    def test_month_end_dates_do_not_drift(self):
        dates = recurring.due_dates(self.rec, date(2026, 5, 1))
        self.assertEqual(dates, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_generate_is_idempotent_skip_and_verify(self):
        self.assertEqual(recurring.generate(self.rec, date(2026, 3, 1)), 2)  # Jan 31, Feb 28 (+7-day lookahead)
        self.assertEqual(recurring.generate(self.rec, date(2026, 3, 1)), 0)
        feb = self.rec.expenses.get(due_date=date(2026, 2, 28))
        self.assertTrue(feb.pending_verification)
        self.assertEqual(feb.total, D("1575"))

        user = get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")
        self.client.force_login(user)
        jan = self.rec.expenses.get(due_date=date(2026, 1, 31))
        self.assertEqual(self.client.post(f"/api/financials/expenses/{jan.pk}/skip/").status_code, 200)
        self.rec.refresh_from_db()
        self.assertEqual(recurring.generate(self.rec, date(2026, 3, 1)), 0)  # skipped date isn't recreated

        response = self.client.post(f"/api/financials/expenses/{feb.pk}/verify/", {"amount": "1510"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        feb.refresh_from_db()
        self.assertEqual((feb.pending_verification, feb.amount), (False, D("1510")))

    def test_editing_updates_pending_entries(self):
        recurring.generate(self.rec, date(2026, 3, 1))
        self.rec.amount = D("1600")
        self.rec.save()
        recurring.sync_pending(self.rec)
        self.assertEqual(set(self.rec.expenses.values_list("amount", flat=True)), {D("1600")})


class ImportTests(TestCase):
    def upload(self, text, name="expenses.csv"):
        return SimpleUploadedFile(name, text.encode("utf-8"))

    def test_french_headers_dates_amounts_and_duplicates(self):
        spend(date(2026, 9, 5), "100", cat(name="Fuel"), vendor="Shell", gst=D("5"), qst=D("9.98"))
        csv_text = (
            "Date;Fournisseur;Description;Montant;TPS;TVQ;Catégorie;Division\n"
            "05/09/2026;Shell;Gas;100,00;5,00;9,98;Fuel;\n"  # already entered → duplicate
            "06/09/2026;Costco;Supplies;250;;;Cleaning supplies;Residential\n"
            "07/09/2026;Costco;Supplies;250;;;Cleaning supplies;Residential\n"
            "2026-09-08;Unknown;Thing;10;;;Not a category;\n"
        )
        parsed = importer.parse(self.upload(csv_text))
        rows = parsed["rows"]
        self.assertTrue(rows[0]["duplicate"])
        self.assertEqual((rows[1]["date"], rows[1]["amount"], rows[1]["division"]), (date(2026, 9, 6), D("250.00"), "residential"))
        self.assertFalse(rows[2]["duplicate"])  # different date
        self.assertTrue(rows[3]["errors"])
        batch = importer.commit(parsed, file_name="expenses.csv", user=None)
        self.assertEqual((batch.rows_imported, batch.rows_skipped), (2, 2))

    def test_total_only_and_default_category(self):
        parsed = importer.parse(self.upload("Date,Vendor,Total,GST,QST\n2026-09-01,Bell,114.98,5,9.98\n"), default_category=cat(name="Software and subscriptions"))
        row = parsed["rows"][0]
        self.assertEqual((row["amount"], row["total"], row["errors"]), (D("100.00"), D("114.98"), []))


class ApiAccessTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        users = get_user_model().objects
        self.owner = users.create_superuser("owner", "owner@example.com", "pw")
        self.clerk = users.create_user("clerk", "clerk@example.com", "pw")
        FinanceAccess.objects.create(user=self.clerk, role="entry")
        self.viewer = users.create_user("viewer", "viewer@example.com", "pw", is_staff=True)
        self.supplies = cat(name="Cleaning supplies")

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data, status=200):
        response = self.client.post(url, data, content_type="application/json")
        self.assertEqual(response.status_code, status, response.content[:400])
        return response.json()

    def test_roles(self):
        self.as_user(self.viewer)
        self.assertEqual(self.client.get("/api/financials/expenses/").status_code, 403)
        self.assertEqual(self.client.get("/api/financials/meta/").json()["role"], "")

        self.as_user(self.clerk)
        mine = self.post("/api/financials/expenses/", {"date": "2026-10-02", "vendor": "Costco", "amount": "80", "category_id": self.supplies.id})
        spend(date(2026, 10, 3), "999", self.supplies, vendor="Someone else's")
        listed = self.client.get("/api/financials/expenses/").json()
        self.assertEqual([r["id"] for r in listed["results"]], [mine["expense"]["id"]])
        self.assertNotIn("sums", listed)
        self.assertEqual(self.client.get("/api/financials/summary/").status_code, 403)
        self.assertEqual(self.client.get("/api/financials/payroll/").status_code, 403)

    def test_profit_on_master_dashboard_only_for_finance_users(self):
        spend(date(2026, 10, 1), "100", self.supplies)
        self.as_user(self.viewer)
        keys = {c["key"] for c in self.client.get("/api/insights/overview/").json()["scorecard"]}
        self.assertNotIn("gross_profit", keys)

        self.as_user(self.owner)
        cards = {c["key"]: c for c in self.client.get("/api/insights/overview/").json()["scorecard"]}
        october_revenue = D(str(cards["revenue"]["current"]))
        self.assertEqual(D(str(cards["gross_profit"]["current"])), october_revenue - 100)
        self.assertIn("Not reviewed yet", cards["gross_profit"]["note"])
        # September has no expenses yet, so there's nothing to compare with.
        self.assertIsNone(cards["gross_profit"]["previous"])

    def test_duplicate_warning_and_pay_run(self):
        self.as_user(self.owner)
        body = {"date": "2026-10-02", "vendor": "Costco", "amount": "80", "category_id": self.supplies.id}
        self.post("/api/financials/expenses/", body)
        dup = self.post("/api/financials/expenses/", {**body, "date": "2026-10-03"}, status=409)
        self.assertEqual(len(dup["duplicates"]), 1)
        self.post("/api/financials/expenses/", {**body, "date": "2026-10-03", "confirm_duplicate": True})

        run = self.post("/api/financials/payroll/", {"pay_date": "2026-10-09", "cleaning_amount": "6000", "office_amount": "1500"})["pay_run"]
        parts = ledger.summary(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((parts["payroll"], parts["labour"]), (D(7500), D(6000)))
        response = self.client.patch(f"/api/financials/payroll/{run['id']}/", {"office_amount": "0"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Expense.objects.filter(pay_run_id=run["id"]).count(), 1)

        pnl = self.client.get("/api/financials/pnl/").json()
        lines = {line["key"]: line for line in pnl["lines"]}
        self.assertEqual(D(str(lines["cogs"]["current"])), D(6160))

    def test_reviewed_month_is_locked(self):
        self.as_user(self.owner)
        MonthReview.objects.create(month=date(2026, 9, 1), status=MonthReview.STATUS_REVIEWED)
        self.post("/api/financials/expenses/", {"date": "2026-09-02", "vendor": "X", "amount": "1", "category_id": self.supplies.id}, status=409)
        august = self.post("/api/financials/reviews/2026-08/close/", {"checks": {"bank": True}}, status=400)
        self.assertIn("Tick every item", august["error"])
        all_checks = {key: True for key in ("bank", "credit_cards", "payroll", "receipts", "expenses")}
        self.assertEqual(self.post("/api/financials/reviews/2026-08/close/", {"checks": all_checks})["review"]["status"], "reviewed")

    def test_receipt_upload_goes_to_storage(self):
        self.as_user(self.owner)
        pdf = SimpleUploadedFile("bill.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        fake = mock.MagicMock()
        with mock.patch("financials.storage.configured", return_value=True), mock.patch("financials.storage._client", return_value=fake):
            response = self.client.post(
                "/api/financials/expenses/",
                {"date": "2026-10-02", "vendor": "Bell", "amount": "50", "category_id": self.supplies.id, "receipt": pdf},
            )
        self.assertEqual(response.status_code, 200, response.content)
        expense = Expense.objects.get(pk=response.json()["expense"]["id"])
        self.assertTrue(expense.receipt_key.startswith("receipts/2026/10/"))
        fake.upload_fileobj.assert_called_once()

        fake_txt = SimpleUploadedFile("bill.pdf", b"not a pdf", content_type="application/pdf")
        with mock.patch("financials.storage.configured", return_value=True), mock.patch("financials.storage._client", return_value=fake):
            response = self.client.post(f"/api/financials/expenses/{expense.pk}/receipt/", {"receipt": fake_txt})
        self.assertEqual(response.status_code, 400)

    def test_cash_and_debt(self):
        self.as_user(self.owner)
        bank = self.post("/api/financials/accounts/", {"name": "Desjardins", "kind": "bank", "balance": "25000", "balance_date": "2026-10-01"})["account"]
        self.post("/api/financials/accounts/", {"name": "Truck loan", "kind": "loan", "balance": "18000", "balance_date": "2026-10-01"})
        self.post(f"/api/financials/accounts/{bank['id']}/balances/", {"date": "2026-10-10", "balance": "27000"})
        totals = self.client.get("/api/financials/cash/").json()["totals"]["balance"]
        self.assertEqual((D(str(totals["cash"])), D(str(totals["debt"])), D(str(totals["net"]))), (D(27000), D(18000), D(9000)))

    def test_every_endpoint_responds(self):
        self.as_user(self.owner)
        spend(date(2026, 9, 2), "100", self.supplies)
        MonthlyTotal.objects.create(month=date(2026, 9, 1), category=cat(name="Fuel"), amount=D("400"))
        sync_payrun_expenses(PayRun.objects.create(pay_date=date(2026, 9, 11), cleaning_amount=D("5000")), self.owner)
        q = "period=month&anchor=2026-09-01"
        for url in (
            "/api/financials/meta/",
            f"/api/financials/expenses/?{q}&receipt=missing",
            f"/api/financials/summary/?{q}",
            f"/api/financials/pnl/?{q}",
            f"/api/financials/cash/?{q}",
            "/api/financials/report/?period=year&anchor=2026-01-01",
            "/api/financials/reviews/",
            "/api/financials/reviews/2026-09/",
            "/api/financials/monthly-totals/?month=2026-09",
            "/api/financials/recurring/",
            f"/api/financials/payroll/?{q}",
            "/api/financials/import/batches/",
            "/api/financials/access/",
            "/api/financials/categories/",
            f"/api/insights/drill/?{q}&metric=expenses_cogs",
            "/api/insights/trend/?metric=net_profit&range=12m",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"{url}: {response.content[:300]}")
        export = self.client.get(f"/api/financials/expenses/export/?{q}")
        self.assertIn(b"Cleaning supplies", export.content)
        report = self.client.get("/api/financials/report/?period=year&anchor=2026-01-01").json()
        september = next(c for c in report["columns"] if c["month"] == "2026-09-01")
        self.assertEqual(D(str(september["values"]["cogs"])), D(5100))
        drill = self.client.get(f"/api/insights/drill/?{q}&metric=expenses_vehicles").json()
        self.assertEqual(drill["rows"][0]["vendor"], "Monthly total")


class AuditTests(MasterDashboardDataMixin, TestCase):
    """Numbers must agree everywhere: P&L, Reports, Master Dashboard, drill-downs."""

    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)  # Oct 13, 2026
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        self.owner = get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")
        self.client.force_login(self.owner)
        self.fuel, self.rent = cat(name="Fuel"), cat(name="Rent and utilities")
        spend(date(2026, 10, 1), "100", self.fuel)
        spend(date(2026, 10, 13), "50", self.fuel)
        spend(date(2026, 10, 20), "999", self.fuel)  # after today: not in "October so far"
        spend(date(2026, 10, 5), "-20", self.fuel)  # refund
        MonthlyTotal.objects.create(month=date(2026, 10, 1), category=self.rent, amount=D("3100"))
        spend(date(2026, 10, 2), "777", self.rent)  # replaced by the monthly total
        spend(date(2026, 9, 30), "40", self.fuel, division="residential")
        spend(date(2026, 9, 15), "300", cat("loan_interest"))
        spend(date(2026, 9, 15), "5000", cat("loan_principal"))

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def test_report_matches_pnl(self):
        for query in ("period=month", "period=month&anchor=2026-09-01", "period=quarter&anchor=2026-07-01", "period=custom&from=2026-09-10&to=2026-10-10"):
            with self.subTest(query=query):
                pnl = {line["key"]: line["current"] for line in self.get(f"/api/financials/pnl/?{query}")["lines"]}
                report = self.get(f"/api/financials/report/?{query}")
                for key, value in report["total"]["values"].items():
                    self.assertEqual(value, pnl[key], key)
                # Monthly columns add up to the total for amounts.
                for key in ("revenue", "cogs", "operating_expenses", "vehicle_expenses", "financial_costs", "net_profit"):
                    if pnl[key] is None:
                        continue
                    columns = [c["values"][key] for c in report["columns"] if c["values"][key] is not None]
                    self.assertAlmostEqual(sum(columns), pnl[key] or 0, places=2, msg=key)

    def test_october_so_far(self):
        lines = {line["key"]: line for line in self.get("/api/financials/pnl/?period=month")["lines"]}
        # Fuel: 100 + 50 - 20 (the Oct 20 one is after today). Rent: 3100 × 13/31 (monthly total, part of the month).
        self.assertEqual(D(str(lines["vehicle_expenses"]["current"])), D("130"))
        self.assertEqual(D(str(lines["operating_expenses"]["current"])), (D(3100) * 13 / 31).quantize(D("0.01")))
        # September 1–13 is before the first expense (Sep 15): "no data", not $0.
        self.assertIsNone(lines["vehicle_expenses"]["previous"])

    def test_september_full_and_excluded(self):
        lines = {line["key"]: line for line in self.get("/api/financials/pnl/?period=month&anchor=2026-09-01")["lines"]}
        self.assertEqual(D(str(lines["financial_costs"]["current"])), D(300))  # principal is not a cost
        revenue = D(str(lines["revenue"]["current"]))
        self.assertEqual(D(str(lines["net_profit"]["current"])), revenue - 40 - 300)
        pnl = self.get("/api/financials/pnl/?period=month&anchor=2026-09-01")
        self.assertEqual(D(str(pnl["company"]["contribution"])), D(str(lines["net_profit"]["current"])))
        self.assertEqual(sum(D(str(d["revenue"])) for d in pnl["divisions"]), revenue)

    def test_master_dashboard_and_drill_agree(self):
        q = "period=month&anchor=2026-09-01"
        cards = {c["key"]: c for c in self.get(f"/api/insights/revenue/?{q}")["profitability"]}
        pnl = {line["key"]: line for line in self.get(f"/api/financials/pnl/?{q}")["lines"]}
        for key in ("cogs", "gross_profit", "operating_expenses", "net_profit", "net_margin"):
            self.assertEqual(cards[key]["current"], pnl[key]["current"], key)
        drill = self.get(f"/api/insights/drill/?period=month&metric=expenses_opex")
        self.assertEqual(D(str(drill["total"])), (D(3100) * 13 / 31).quantize(D("0.01")))

    def test_summary_breakdowns_add_up(self):
        data = self.get("/api/financials/summary/?period=month&anchor=2026-09-01")
        cards = {c["key"]: c for c in data["cards"]}
        counted = [r for r in data["categories"] if r["part"] != "excluded"]
        self.assertAlmostEqual(sum(r["current"] for r in counted), cards["total_expenses"]["current"], places=2)
        self.assertAlmostEqual(sum(r["current"] for r in data["divisions"]), cards["total_expenses"]["current"], places=2)

    def test_month_can_only_be_closed_after_it_ends(self):
        checks = {key: True for key in ("bank", "credit_cards", "payroll", "receipts", "expenses")}
        response = self.client.post("/api/financials/reviews/2026-10/close/", {"checks": checks}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_entry_user_duplicate_hides_details(self):
        clerk = get_user_model().objects.create_user("clerk", "c@example.com", "pw")
        FinanceAccess.objects.create(user=clerk, role="entry")
        self.client.force_login(clerk)
        body = {"date": "2026-10-01", "vendor": "Shell", "amount": "100", "category_id": self.fuel.id}
        dup = self.client.post("/api/financials/expenses/", body, content_type="application/json").json()
        self.assertEqual(dup["duplicates"][0]["vendor"], "")

    def test_recurring_does_not_flood_old_charges(self):
        rec = RecurringExpense.objects.create(name="Rent", amount=D(100), category=self.rent, start_date=date(2023, 1, 1))
        recurring.generate(rec, TODAY)
        self.assertEqual(sorted(rec.expenses.values_list("due_date", flat=True)), [date(2026, 9, 1), date(2026, 10, 1)])


class ImportAmbiguityTests(TestCase):
    def test_same_name_in_two_groups(self):
        Category.objects.create(group="vehicles", name="Insurance")
        text = "Date,Total,Category\n2026-09-01,100,Insurance\n2026-09-02,100,Vehicles: Insurance\n"
        parsed = importer.parse(SimpleUploadedFile("x.csv", text.encode()))
        self.assertIn("more than one group", parsed["rows"][0]["errors"][0])
        self.assertEqual(parsed["rows"][1]["category"].group, "vehicles")
        self.assertTrue(parsed["taxes_missing"])


class CoverageTests(MasterDashboardDataMixin, TestCase):
    def test_no_fake_profit_before_expenses_start(self):
        with mock.patch("django.utils.timezone.localdate", return_value=TODAY), mock.patch("insights.periods.today", return_value=TODAY):
            rebuild_insights(self.integration)
            spend(date(2026, 10, 3), "100", cat(name="Fuel"))  # first expense ever
            self.client.force_login(get_user_model().objects.create_superuser("o", "o@example.com", "pw"))
            year = {l["key"]: l for l in self.client.get("/api/financials/pnl/?period=year").json()["lines"]}
            self.assertIsNone(year["net_profit"]["current"])
            self.assertIn("can't be calculated", year["net_profit"]["note"])
            self.assertEqual(D(str(year["vehicle_expenses"]["current"])), D(100))  # costs still shown
            month = {l["key"]: l for l in self.client.get("/api/financials/pnl/?period=month").json()["lines"]}
            self.assertIsNotNone(month["net_profit"]["current"])
