from datetime import date, datetime, time
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from analytics.pricing_models import PricingSubmission
from financials.cashflow import money_out
from financials.models import Category, Expense, MonthlyTotal
from insights import periods
from insights.metrics import Metrics
from insights.models import InsightsSetting
from insights.rebuild import rebuild_insights
from insights.tests import TODAY, MasterDashboardDataMixin
from integrations.client import JobberAPIError
from integrations.sales_sync import STATUS_KEY, SalesSync
from operations.models import Client, Invoice, JobberRequest, PaymentRecord, Quote


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def calc(n, day, email="", phone="", status="submitted", heard="google", name="Pat"):
    return PricingSubmission.objects.create(
        external_id=f"s{n}", status=status, customer_email=email, customer_phone=phone, heard_about_us=heard,
        customer_first_name=name, source_created_at=at(day),
    )


class SalesFunnelTests(MasterDashboardDataMixin, TestCase):
    def setUp(self):
        for target in ("django.utils.timezone.localdate", "insights.periods.today"):
            patcher = mock.patch(target, return_value=TODAY)
            patcher.start()
            self.addCleanup(patcher.stop)
        rebuild_insights(self.integration)
        Client.objects.filter(pk=self.alice.pk).update(email="alice@mail.ca", phone="(514) 555-0101")
        self.newbie = Client.objects.create(integration=self.integration, external_id="c9", name="Newbie", email="new@mail.ca")
        from operations.models import Visit

        Visit.objects.create(integration=self.integration, external_id="nv1", client=self.newbie, title="First", start_at=at(date(2026, 9, 20)), is_complete=True)

    def metrics(self):
        m = Metrics(self.integration)
        m.finance_visible = True
        return m

    def test_people_are_counted_once_and_matched_to_clients(self):
        calc(1, date(2026, 9, 2), email="NEW@mail.ca", name="Newbie")
        calc(2, date(2026, 9, 10), phone="514 555 0199", email="new@mail.ca")  # same person, same lead
        calc(3, date(2026, 9, 5), phone="+1 514-555-0101")  # Alice: already a client
        calc(4, date(2026, 9, 6), email="stranger@mail.ca", status="approved", heard="facebook")
        calc(5, date(2026, 9, 7), email="draft@mail.ca", status="draft")  # unfinished: not a lead
        m = self.metrics()
        september = periods.build("month", date(2026, 9, 1))
        self.assertEqual(m.value("leads", september), 3)
        self.assertEqual(m.value("new_prospects", september), 2)
        self.assertEqual(m.value("leads_quoted", september), 2)
        self.assertEqual(m.value("leads_accepted", september), 2)  # approved calculator + Newbie cleaned
        self.assertEqual(m.value("leads_first_clean", september), 1)
        self.assertEqual(m.value("lead_conversion", september), D("50.0"))
        self.assertEqual(m.value("days_to_first_clean", september), D("18.0"))  # Sep 2 → Sep 20
        sources = {row["label"]: row for row in m.lead_sources(september)}
        self.assertEqual(sources["Calculator · Facebook"]["leads"], 1)

    def test_new_lead_after_90_quiet_days(self):
        calc(1, date(2026, 3, 1), email="x@mail.ca")
        calc(2, date(2026, 5, 1), email="x@mail.ca")  # 61 days later: same lead
        calc(3, date(2026, 9, 1), email="x@mail.ca")  # 123 days after last contact: new lead
        self.assertEqual([lead.date for lead in self.metrics().leads()], [date(2026, 3, 1), date(2026, 9, 1)])

    def test_jobber_request_and_quote(self):
        request = JobberRequest.objects.create(integration=self.integration, external_id="r1", client=self.newbie, source_created_at=at(date(2026, 9, 1)))
        Quote.objects.create(integration=self.integration, external_id="q1", client=self.newbie, request=request, quote_status="approved", sent_at=at(date(2026, 9, 3)))
        lead = self.metrics().leads()[0]
        self.assertEqual((lead.source, lead.quoted, lead.accepted, lead.first_clean), ("jobber_request", True, True, date(2026, 9, 20)))

    def test_payments_refunds_and_days_to_pay(self):
        invoice = Invoice.objects.get(external_id="i2")  # issued Sep 20
        PaymentRecord.objects.create(integration=self.integration, external_id="p1", client=self.dental, invoice=invoice, entry_date=at(date(2026, 9, 30)), amount=D("1035"))
        PaymentRecord.objects.create(integration=self.integration, external_id="p2", client=self.dental, invoice=invoice, entry_date=at(date(2026, 10, 2)), amount=D("-35"))
        m = self.metrics()
        self.assertEqual(m.value("payments_received", periods.build("month", date(2026, 9, 1))), D("1035"))
        self.assertEqual(m.value("days_to_payment", periods.build("month", date(2026, 9, 1))), D("10.0"))
        self.assertEqual(m.value("payments_received", periods.build("month", date(2026, 10, 1))), D("-35"))
        self.assertIsNone(m.value("payments_received", periods.build("month", date(2026, 8, 1))))  # before data: unavailable

    def test_cash_out_skips_internal_moves(self):
        cat = lambda **kw: Category.objects.get(parent=None, **kw)  # noqa: E731
        Expense.objects.create(date=date(2026, 9, 3), vendor="Shell", amount=D(100), gst=D(5), qst=D("9.98"), category=cat(name="Fuel"))
        Expense.objects.create(date=date(2026, 9, 4), vendor="BDC", amount=D(500), category=cat(system_key="loan_principal"))
        Expense.objects.create(date=date(2026, 9, 5), vendor="Visa", amount=D(900), category=cat(system_key="credit_card_payments"))
        Expense.objects.create(date=date(2026, 9, 6), vendor="X", amount=D(50), category=cat(name="Office supplies"), status="unpaid")
        Expense.objects.create(date=date(2026, 9, 7), vendor="Y", amount=D(70), category=cat(name="Cleaning supplies"))
        MonthlyTotal.objects.create(month=date(2026, 9, 1), category=cat(name="Cleaning supplies"), amount=D(300))
        self.assertEqual(money_out(date(2026, 9, 1), date(2026, 9, 30)), D("914.98"))  # 114.98 + 500 + 300

    def test_sales_page_and_drills(self):
        calc(1, date(2026, 10, 2), email="new@mail.ca")
        self.client.force_login(get_user_model().objects.create_superuser("o", "o@mail.ca", "pw"))
        data = self.client.get("/api/insights/sales/?period=month").json()
        funnel = {c["key"]: c for c in data["funnel"]}
        self.assertEqual(funnel["leads"]["current"], 1)
        self.assertTrue(data["sync_notes"])  # requests not synced in tests
        for metric in ("leads", "leads_won", "payments"):
            response = self.client.get(f"/api/insights/drill/?period=month&metric={metric}")
            self.assertEqual(response.status_code, 200, response.content[:200])


class FakeJobber:
    """Answers the schema questions and pages like Jobber; `denied` roots raise a permission error."""

    TYPES = {
        "RequestConnection": [("nodes", "LIST", "Request")],
        "QuoteConnection": [("nodes", "LIST", "Quote")],
        "PaymentRecordConnection": [("nodes", "LIST", "PaymentRecord")],
        "Request": [("id", "SCALAR", "ID"), ("title", "SCALAR", "String"), ("createdAt", "SCALAR", "ISO8601DateTime"), ("client", "OBJECT", "Client")],
        "Quote": [("id", "SCALAR", "ID"), ("quoteStatus", "ENUM", "QuoteStatus"), ("createdAt", "SCALAR", "X"), ("client", "OBJECT", "Client"), ("request", "OBJECT", "Request"), ("amounts", "OBJECT", "QuoteAmounts")],
        "PaymentRecord": [("id", "SCALAR", "ID"), ("amount", "SCALAR", "Float"), ("entryDate", "SCALAR", "X"), ("adjustmentType", "ENUM", "A"), ("invoice", "OBJECT", "Invoice")],
    }

    def __init__(self, denied=()):
        self.denied = set(denied)
        self.queries = []

    def execute(self, query, variables=None):
        self.queries.append(query)
        if "RootFields" in query:
            return {"__type": {"fields": [{"name": n, "type": {"kind": "OBJECT", "name": t}} for n, t in (("requests", "RequestConnection"), ("quotes", "QuoteConnection"), ("paymentRecords", "PaymentRecordConnection"))]}}
        if "RootType" in query:
            name = variables["name"]
            fields = [{"name": f, "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": kind, "name": t}}} for f, kind, t in self.TYPES.get(name, [])]
            return {"__type": {"name": name, "kind": "OBJECT", "fields": fields, "possibleTypes": None}}
        root = next(r for r in ("requests", "quotes", "paymentRecords") if f"{r}(first" in query)
        if root in self.denied:
            raise JobberAPIError("Access denied: missing scope")
        nodes = {
            "requests": [{"id": "r1", "title": "Need a clean", "createdAt": "2026-09-01T10:00:00Z", "client": {"id": "c1"}}],
            "quotes": [{"id": "q1", "quoteStatus": "APPROVED", "createdAt": "2026-09-02T10:00:00Z", "client": {"id": "c1"}, "request": {"id": "r1"}, "amounts": {"subtotal": 100, "total": 115}}],
            "paymentRecords": [{"id": "p1", "amount": 50, "entryDate": "2026-09-03T10:00:00Z", "adjustmentType": "REFUND", "invoice": {"id": "i1"}}],
        }[root]
        return {root: {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}


class SalesSyncTests(MasterDashboardDataMixin, TestCase):
    def service(self, fake):
        from integrations.sync import JobberSyncService

        with mock.patch("integrations.sync.JobberClient"):
            service = JobberSyncService(self.integration)
        service.client = fake
        return service

    def test_sync_builds_selection_from_schema(self):
        fake = FakeJobber()
        status = SalesSync(self.service(fake)).run()
        self.assertTrue(all(status[r]["ok"] for r in ("requests", "quotes", "paymentRecords")))
        quote = Quote.objects.get(external_id="q1")
        self.assertEqual((quote.quote_status, quote.request.external_id, quote.client, quote.total), ("approved", "r1", self.alice, D(115)))
        payment = PaymentRecord.objects.get(external_id="p1")
        self.assertEqual((payment.amount, payment.client), (D(-50), self.alice))  # refund; client from the invoice
        page = next(q for q in fake.queries if "paymentRecords(first" in q)
        self.assertNotIn("paymentType", page)  # not in this schema, so not asked for

    def test_permission_error_is_recorded_not_raised(self):
        SalesSync(self.service(FakeJobber(denied={"paymentRecords"}))).run()
        status = InsightsSetting.get(STATUS_KEY)
        self.assertTrue(status["requests"]["ok"])
        self.assertFalse(status["paymentRecords"]["ok"])
        self.assertIn("Developer Center", status["paymentRecords"]["error"])


class NewClientRegressionTests(SalesFunnelTests):
    def test_new_client_who_booked_ahead_counts_as_new(self):
        # Newbie's first visit (Sep 20) was booked before September, so they were "active" on Aug 31 already.
        m = self.metrics()
        september = periods.build("month", date(2026, 9, 1))
        self.assertIn(self.newbie.pk, m.client_flows(september)["start"])
        self.assertEqual(m.value("new_clients", september), 1)
        start, gained, lost, end = (m.value(k, september) for k in ("active_clients_start", "gained_clients", "lost_clients", "active_clients"))
        self.assertEqual(start + gained - lost, end)
        self.assertEqual(m.value("net_client_growth", september), end - start)


class SalesAuditTests(SalesFunnelTests):
    def test_late_link_merges_into_one_person(self):
        calc(1, date(2026, 9, 1), email="a@mail.ca")
        calc(2, date(2026, 9, 2), phone="4185550123")
        calc(3, date(2026, 9, 3), email="a@mail.ca", phone="4185550123")  # links the first two
        self.assertEqual(len(self.metrics().leads()), 1)

    def test_conversion_belongs_to_the_latest_lead_only(self):
        calc(1, date(2026, 3, 1), email="new@mail.ca")
        calc(2, date(2026, 9, 1), email="new@mail.ca")  # new lead after 90 quiet days; first clean Sep 20
        march, september = self.metrics().leads()
        self.assertIsNone(march.first_clean)
        self.assertEqual(september.first_clean, date(2026, 9, 20))

    def test_test_quotes_and_placeholder_phones(self):
        calc(1, date(2026, 9, 1), email="test123@gmail.com")
        calc(2, date(2026, 9, 1), email="bob@test.com")
        calc(3, date(2026, 9, 2), email="one@mail.ca", phone="1111111111")
        calc(4, date(2026, 9, 3), email="two@mail.ca", phone="111-111-1111")  # same placeholder: different people
        self.assertEqual(len(self.metrics().leads()), 2)

    def test_phone_with_extension_and_country_code(self):
        from insights.sales import norm_phone

        self.assertEqual(norm_phone("+1 (514) 555-0101 ext. 23"), "5145550101")
        self.assertEqual(norm_phone("514.555.0101"), "5145550101")
        self.assertEqual(norm_phone("555-0101"), "")


class SalesSyncAuditTests(SalesSyncTests):
    def test_deleted_records_are_removed_and_bad_debt_not_counted(self):
        PaymentRecord.objects.create(integration=self.integration, external_id="gone", amount=D(99))
        fake = FakeJobber()
        fake_nodes = fake.execute

        def execute(query, variables=None):
            data = fake_nodes(query, variables)
            if "paymentRecords(first" in query:
                data["paymentRecords"]["nodes"].append({"id": "p2", "amount": 300, "entryDate": "2026-09-04T10:00:00Z", "adjustmentType": "BAD_DEBT"})
            return data

        fake.execute = execute
        status = SalesSync(self.service(fake)).run()
        self.assertFalse(PaymentRecord.objects.filter(external_id="gone").exists())
        self.assertEqual(status["paymentRecords"]["removed"], 1)
        self.assertEqual(PaymentRecord.objects.get(external_id="p2").amount, 0)

    def test_short_listing_removes_nothing(self):
        for n in range(5):
            PaymentRecord.objects.create(integration=self.integration, external_id=f"old{n}", amount=D(1))
        SalesSync(self.service(FakeJobber())).run()  # Jobber listed 1 of 6: suspicious, keep everything
        self.assertEqual(PaymentRecord.objects.filter(external_id__startswith="old").count(), 5)
