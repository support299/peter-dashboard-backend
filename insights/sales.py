"""Sales funnel and money received (mixed into insights.metrics.Metrics).

Leads come from two places: Pricing Calculator quotes (finished ones, with an
email or phone) and Jobber requests. One person is one lead, however many quotes
they make: people are matched by email, phone and Jobber client. A new lead
starts only if the person had no contact for LEAD_GAP_DAYS.

Funnel (for the period's leads who weren't already clients):
lead → quoted → accepted → first cleaning → recurring.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Min, Q, Sum
from django.utils import timezone

from analytics.pricing_models import PricingSubmission
from insights.models import RecurringPlan
from operations.models import Client, JobberRequest, PaymentRecord, Quote

LEAD_GAP_DAYS = 90  # contact again after this long = a new lead
QUOTE_WINDOW_DAYS = 90  # a Jobber quote within this many days of the lead belongs to it
CALC_ACCEPTED = {"approved"}
QUOTE_ACCEPTED = {"approved", "converted"}
REQUEST_CONVERTED = {"converted"}

SOURCE_LABELS = {
    "google": "Google",
    "facebook": "Facebook",
    "friend-family": "Friend or family",
    "company-car": "Company car",
    "referral": "Referral",
    "returning": "Returning customer",
    "other": "Other",
    "": "Not given",
}


# Test quotes made while building and checking the calculator are not leads.
TEST_EMAIL = re.compile(r"(^|[._+\-\d])test|@(test|example|123|fdsfdsf)\.|@cleanonthego\.")


def norm_email(value) -> str:
    value = str(value or "").strip().lower()
    return value if "@" in value else ""


def is_test_email(email: str) -> bool:
    return bool(email and TEST_EMAIL.search(email))


def norm_phone(value) -> str:
    """10-digit North American number; drops +1 and anything after it (extensions). Placeholders don't count."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 11 and digits[0] == "1":
        digits = digits[1:]
    digits = digits[:10]
    if len(digits) < 10 or len(set(digits)) <= 2 or digits in ("1234567890", "0123456789"):
        return ""
    return digits


def _local(dt) -> date | None:
    return timezone.localtime(dt).date() if dt else None


@dataclass
class Lead:
    date: date
    source: str  # calculator | jobber_request
    channel: str  # how they heard about us / request source
    name: str
    client_ids: set = field(default_factory=set)
    calculator_accepted: bool = False
    request_converted: bool = False
    existing_client: bool = False
    quoted: bool = False
    accepted: bool = False
    first_clean: date | None = None
    recurring: bool = False
    link: str = ""
    touches: int = 1


class _People:
    """Union-find over identifiers (email, phone, Jobber client)."""

    def __init__(self):
        self.parent = {}

    def find(self, key):
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, keys):
        keys = [k for k in keys if k]
        for other in keys[1:]:
            a, b = self.find(keys[0]), self.find(other)
            if a != b:
                self.parent[b] = a
        return self.find(keys[0]) if keys else None


class SalesMetrics:
    # ——— leads ———
    def leads(self) -> list[Lead]:
        """Every lead episode ever, oldest first (cached per request)."""
        key = ("leads",)
        if key in self._cache:
            return self._cache[key]
        people = _People()
        clients = {}
        for c in Client.objects.filter(integration=self.integration).values("id", "email", "phone", "is_internal", "name", "company_name", "jobber_web_uri"):
            clients[c["id"]] = c
            people.join([f"c{c['id']}", norm_email(c["email"]) and f"e{norm_email(c['email'])}", norm_phone(c["phone"]) and f"p{norm_phone(c['phone'])}"])

        # People are only final once every identifier is joined, so touches keep an identifier
        # and are resolved to their person afterwards.
        touches = []  # (date, identifier, Lead-ish dict)
        for s in PricingSubmission.objects.filter(is_deleted=False).exclude(status="draft").values(
            "external_id", "source_created_at", "customer_email", "customer_phone", "heard_about_us", "status", "customer_first_name", "customer_last_name", "customer_company", "quote_url"
        ):
            email, phone = norm_email(s["customer_email"]), norm_phone(s["customer_phone"])
            day = _local(s["source_created_at"])
            if not day or not (email or phone) or is_test_email(email):
                continue
            ident = people.join([email and f"e{email}", phone and f"p{phone}"])
            name = " ".join(p for p in (s["customer_first_name"], s["customer_last_name"]) if p).strip() or s["customer_company"] or email
            touches.append((day, ident, {"source": "calculator", "channel": SOURCE_LABELS.get(s["heard_about_us"] or "", s["heard_about_us"].replace("-", " ").capitalize()),
                                         "name": name, "accepted": s["status"] in CALC_ACCEPTED, "link": s["quote_url"] or ""}))
        for r in JobberRequest.objects.filter(integration=self.integration).values("id", "source_created_at", "client_id", "source", "request_status", "title", "jobber_web_uri"):
            day = _local(r["source_created_at"])
            if not day:
                continue
            ident = f"c{r['client_id']}" if r["client_id"] else f"r{r['id']}"
            client = clients.get(r["client_id"]) or {}
            touches.append((day, ident, {"source": "jobber_request", "channel": r["source"] or "Jobber request", "name": client.get("name") or client.get("company_name") or r["title"] or "—",
                                         "converted": r["request_status"] in REQUEST_CONVERTED, "link": r["jobber_web_uri"] or client.get("jobber_web_uri") or ""}))
        touches = [(day, people.find(ident), info) for day, ident, info in touches]

        client_ids_of = defaultdict(set)
        for cid in clients:
            client_ids_of[people.find(f"c{cid}")].add(cid)
        internal = {people.find(f"c{cid}") for cid, c in clients.items() if c["is_internal"]}

        first_visit = self.first_visit_dates()
        quotes = defaultdict(list)
        for q in Quote.objects.filter(integration=self.integration, client__isnull=False).values("client_id", "source_created_at", "sent_at", "quote_status"):
            quotes[q["client_id"]].append((_local(q["sent_at"] or q["source_created_at"]), q["quote_status"]))
        recurring_starts = defaultdict(list)
        for cid, start in RecurringPlan.objects.filter(integration=self.integration, client__isnull=False, start_date__isnull=False).values_list("client_id", "start_date"):
            recurring_starts[cid].append(start)

        episodes, episode_people = [], []
        last_touch = {}
        current = {}
        for day, person, info in sorted(touches, key=lambda t: t[0]):
            if person in internal:
                continue
            lead = current.get(person)
            if lead is None or day > last_touch[person] + timedelta(days=LEAD_GAP_DAYS):
                lead = Lead(date=day, source=info["source"], channel=info["channel"], name=info["name"], client_ids=client_ids_of.get(person, set()), link=info["link"])
                current[person] = lead
                episodes.append(lead)
                episode_people.append(person)
            else:
                lead.touches += 1
            lead.calculator_accepted |= info.get("accepted", False)
            lead.request_converted |= info.get("converted", False)
            last_touch[person] = day

        # A person's next lead starts a new episode: what happens after that belongs to the newer lead,
        # so a conversion is never credited to two leads.
        next_start = {}
        by_person = defaultdict(list)
        for lead, person in zip(episodes, episode_people):
            by_person[person].append(lead)
        for leads in by_person.values():
            for lead, following in zip(leads, leads[1:]):
                next_start[id(lead)] = following.date

        for lead in episodes:
            until = next_start.get(id(lead))
            firsts = [first_visit[c] for c in lead.client_ids if c in first_visit]
            lead.existing_client = any(d < lead.date for d in firsts)
            later = [d for d in firsts if d >= lead.date and (until is None or d < until)]
            lead.first_clean = min(later) if later and not lead.existing_client else None
            window_end = lead.date + timedelta(days=QUOTE_WINDOW_DAYS)
            if until is not None:
                window_end = min(window_end, until - timedelta(days=1))
            lead_quotes = [(d, status) for c in lead.client_ids for d, status in quotes.get(c, []) if d and lead.date <= d <= window_end]
            lead.accepted = (
                lead.calculator_accepted or lead.request_converted or any(status in QUOTE_ACCEPTED for _, status in lead_quotes) or bool(lead.first_clean)
            )
            # Each stage includes the ones after it, so the funnel never widens.
            lead.quoted = lead.source == "calculator" or bool(lead_quotes) or lead.accepted
            lead.recurring = bool(lead.first_clean) and any(
                st >= lead.date and (until is None or st < until) for c in lead.client_ids for st in recurring_starts.get(c, [])
            )
        self._cache[key] = episodes
        return episodes

    def period_leads(self, period) -> list[Lead]:
        return [lead for lead in self.leads() if period.start <= lead.date <= period.end]

    def prospects(self, period) -> list[Lead]:
        return [lead for lead in self.period_leads(period) if not lead.existing_client]

    def _leads_ok(self, period):
        return self._has("leads", period) and not self.division

    def _calculator_estimates(self, period):
        """Every finished calculator quote, not people: the same test-quote filter as leads."""
        if not self._leads_ok(period):
            return None
        rows = PricingSubmission.objects.filter(
            is_deleted=False, source_created_at__gte=period.start_dt, source_created_at__lt=period.end_dt
        ).exclude(status="draft").values_list("customer_email", "customer_phone")
        count = 0
        for email, phone in rows:
            email = norm_email(email)
            if (email or norm_phone(phone)) and not is_test_email(email):
                count += 1
        return count

    def _jobber_quotes(self, period):
        from insights.metrics import jobber_sync_note

        if not self._leads_ok(period) or jobber_sync_note("quotes"):
            return None
        sent = Q(sent_at__gte=period.start_dt, sent_at__lt=period.end_dt)
        undated = Q(sent_at__isnull=True, source_created_at__gte=period.start_dt, source_created_at__lt=period.end_dt)
        return (
            Quote.objects.filter(sent | undated, integration=self.integration)
            .exclude(quote_status__iexact="draft")
            .exclude(client__is_internal=True)
            .count()
        )

    def _leads(self, period):
        return len(self.period_leads(period)) if self._leads_ok(period) else None

    def _new_prospects(self, period):
        return len(self.prospects(period)) if self._leads_ok(period) else None

    def _funnel(self, period, attr):
        if not self._leads_ok(period):
            return None
        return sum(1 for lead in self.prospects(period) if getattr(lead, attr))

    def _leads_quoted(self, period):
        return self._funnel(period, "quoted")

    def _leads_accepted(self, period):
        return self._funnel(period, "accepted")

    def _leads_first_clean(self, period):
        return self._funnel(period, "first_clean")

    def _leads_recurring(self, period):
        return self._funnel(period, "recurring")

    def _lead_conversion(self, period):
        prospects, won = self.value("new_prospects", period), self.value("leads_first_clean", period)
        if not prospects:
            return None
        return (Decimal(won) / Decimal(prospects) * 100).quantize(Decimal("0.1"))

    def _days_to_first_clean(self, period):
        if not self._leads_ok(period):
            return None
        days = [(lead.first_clean - lead.date).days for lead in self.prospects(period) if lead.first_clean]
        return (Decimal(sum(days)) / len(days)).quantize(Decimal("0.1")) if days else None

    def lead_sources(self, period) -> list:
        rows = defaultdict(lambda: {"leads": 0, "prospects": 0, "won": 0})
        for lead in self.period_leads(period):
            label = f"{'Calculator' if lead.source == 'calculator' else 'Jobber request'} · {lead.channel}"
            row = rows[label]
            row["leads"] += 1
            if not lead.existing_client:
                row["prospects"] += 1
                row["won"] += 1 if lead.first_clean else 0
        return [
            {"label": label, **row, "conversion": (Decimal(row["won"]) / row["prospects"] * 100).quantize(Decimal("0.1")) if row["prospects"] else None}
            for label, row in sorted(rows.items(), key=lambda kv: -kv[1]["leads"])
        ]

    def leads_start(self):
        firsts = [
            PricingSubmission.objects.filter(is_deleted=False).exclude(status="draft").aggregate(m=Min("source_created_at"))["m"],
            JobberRequest.objects.filter(integration=self.integration).aggregate(m=Min("source_created_at"))["m"],
        ]
        firsts = [_local(f) for f in firsts if f]
        return min(firsts) if firsts else None

    # ——— marketing cost (Financials) ———
    def marketing_spend(self, period):
        from financials import ledger

        cats = ledger.categories()
        return sum((row["amount"] for row in ledger.rows(period.start, period.end) if cats[row["category_id"]].system_key == "marketing"), Decimal(0))

    def _marketing_ok(self, period):
        from financials import ledger

        return self.finance_visible and not self.division and ledger.covers(period.start, period.end, self.data_starts().get("expenses"), "net_profit")

    def _cost_per_lead(self, period):
        leads = self.value("leads", period)
        if not leads or not self._marketing_ok(period):
            return None
        return (self.marketing_spend(period) / leads).quantize(Decimal("0.01"))

    def _cost_per_new_client(self, period):
        new = self.value("new_clients", period)
        if not new or not self._marketing_ok(period):
            return None
        return (self.marketing_spend(period) / new).quantize(Decimal("0.01"))

    def _marketing_spend(self, period):
        return self.marketing_spend(period) if self._marketing_ok(period) else None

    # ——— money received ———
    def payments(self, period):
        qs = PaymentRecord.objects.filter(integration=self.integration, entry_date__gte=period.start_dt, entry_date__lt=period.end_dt).exclude(
            client__is_internal=True
        )
        if self.division:
            qs = qs.filter(Q(invoice__division=self.division) | Q(invoice__isnull=True, client__division=self.division))
        return qs

    def _payments_received(self, period):
        if not self._has("payments", period):
            return None
        return self.payments(period).aggregate(s=Sum("amount"))["s"] or Decimal(0)

    def _days_to_payment(self, period):
        if not self._has("payments", period):
            return None
        days = [
            (timezone.localtime(paid).date() - timezone.localtime(issued).date()).days
            for paid, issued in self.payments(period).filter(amount__gt=0, invoice__issued_date__isnull=False).values_list("entry_date", "invoice__issued_date")
        ]
        days = [d for d in days if d >= 0]
        return (Decimal(sum(days)) / len(days)).quantize(Decimal("0.1")) if days else None

    # ——— cash flow (Financials access) ———
    def _cash_ok(self, period):
        return self.finance_visible and not self.division and self._has("payments", period) and self._has("expenses", period)

    def _cash_in(self, period):
        return self.value("payments_received", period) if self._cash_ok(period) else None

    def _cash_out(self, period):
        if not self._cash_ok(period):
            return None
        from financials.cashflow import money_out

        return money_out(period.start, period.end)

    def _net_cash_flow(self, period):
        cash_in, cash_out = self.value("cash_in", period), self.value("cash_out", period)
        return None if cash_in is None or cash_out is None else cash_in - cash_out
