"""Jobber requests, quotes and payment records (sales funnel and payments received).

These need Jobber permissions the app may not have yet, and their exact field
names weren't confirmed against the live API, so each selection is built from
what Jobber's schema reports (introspection), and each entity syncs on its own:
a failure is recorded (and shown on the dashboard) without stopping the main sync.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from integrations.client import JobberAPIError, JobberQueryTooExpensive
from integrations.mapping import clip, node_id, parse_decimal, parse_dt
from operations.models import Client, Invoice, JobberRequest, PaymentRecord, Quote

logger = logging.getLogger(__name__)

STATUS_KEY = "jobber_sales_sync"
PAGE_SIZE = 50
# Records deleted in Jobber are removed after a complete pass, unless the pass saw far fewer
# records than we hold (then something is off with the listing, so nothing is removed).
PRUNE_MIN_SHARE = 0.9
# Adjustment types that aren't money from a client (write-offs and the like): kept but not counted.
NOT_MONEY = ("bad_debt", "bad debt", "baddebt", "write_off", "write off", "writeoff")

# Wanted fields per root: plain names are scalars/enums, tuples are objects with a sub-selection.
WISHLIST = {
    "requests": ["id", "title", "requestStatus", "source", "createdAt", "updatedAt", "jobberWebUri", ("client", "{ id }")],
    "quotes": [
        "id", "quoteNumber", "quoteStatus", "title", "createdAt", "updatedAt", "sentAt", "transitionedAt", "jobberWebUri",
        ("client", "{ id }"), ("request", "{ id }"), ("amounts", "{ subtotal total }"),
    ],
    "paymentRecords": [
        "id", "amount", "entryDate", "adjustmentType", "paymentType", "paymentOrigin", "createdAt", "updatedAt",
        ("client", "{ id }"), ("invoice", "{ id }"),
    ],
}
REQUIRED = {"requests": {"id", "createdAt"}, "quotes": {"id", "createdAt"}, "paymentRecords": {"id", "amount"}}

ROOT_TYPE_QUERY = """
query RootType($name: String!) {
  __type(name: $name) {
    name
    kind
    possibleTypes { name }
    fields { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } }
  }
}
"""

QUERY_ROOT_TYPES = """
query RootFields {
  __type(name: "Query") {
    fields { name type { kind name ofType { kind name ofType { kind name } } } }
  }
}
"""


def _named(type_ref: dict | None) -> tuple[str, str]:
    """Unwrap NON_NULL / LIST wrappers to (kind, name)."""
    while type_ref and not type_ref.get("name"):
        type_ref = type_ref.get("ofType")
    return (type_ref or {}).get("kind", ""), (type_ref or {}).get("name", "")


class Schema:
    def __init__(self, client):
        self.client = client
        self._types = {}
        data = client.execute(QUERY_ROOT_TYPES)
        self.roots = {f["name"]: _named(f["type"])[1] for f in ((data.get("__type") or {}).get("fields") or [])}

    def type(self, name: str) -> dict:
        if name not in self._types:
            self._types[name] = self.client.execute(ROOT_TYPE_QUERY, {"name": name}).get("__type") or {}
        return self._types[name]

    def node_type(self, root: str) -> dict:
        connection = self.type(self.roots.get(root) or "")
        nodes = next((f for f in connection.get("fields") or [] if f["name"] == "nodes"), None)
        return self.type(_named(nodes["type"])[1]) if nodes else {}

    def fields_of(self, type_info: dict) -> dict:
        return {f["name"]: _named(f["type"])[0] for f in type_info.get("fields") or []}

    def selection(self, root: str) -> tuple[str, set]:
        """GraphQL node selection for the wanted fields this schema has, and the names it includes."""
        node = self.node_type(root)
        if not node:
            raise JobberAPIError(f"Jobber has no `{root}` list.")

        def pick(fields: dict) -> tuple[list[str], set]:
            parts, names = [], set()
            for item in WISHLIST[root]:
                name, sub = (item, "") if isinstance(item, str) else item
                kind = fields.get(name)
                if not kind:
                    continue
                if sub and kind in ("OBJECT", "INTERFACE"):
                    parts.append(f"{name} {sub}")
                elif not sub and kind in ("SCALAR", "ENUM"):
                    parts.append(name)
                else:
                    continue
                names.add(name)
            return parts, names

        if node.get("kind") == "UNION":
            fragments, names = [], set()
            for possible in node.get("possibleTypes") or []:
                parts, got = pick(self.fields_of(self.type(possible["name"])))
                if parts:
                    fragments.append(f"... on {possible['name']} {{ {' '.join(parts)} }}")
                    names |= got
            return "__typename " + " ".join(fragments), names
        parts, names = pick(self.fields_of(node))
        return " ".join(parts), names


def _paginate(client, root: str, selection: str):
    cursor, page_size = None, PAGE_SIZE
    while True:
        query = f"""
        query SalesPage($first: Int!, $after: String) {{
          {root}(first: $first, after: $after) {{
            nodes {{ {selection} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        try:
            data = client.execute(query, {"first": page_size, "after": cursor})
        except JobberQueryTooExpensive:
            if page_size <= 1:
                raise
            page_size = max(1, page_size // 2)
            continue
        connection = data.get(root) or {}
        for node in connection.get("nodes") or []:
            if node:
                yield node
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if any(word in text.lower() for word in ("access denied", "unauthorized", "forbidden", "scope", "permission", "not authorized")):
        return "Jobber didn't allow access. The app needs this permission in the Jobber Developer Center, then reconnect Jobber."
    return text[:300]


def _lower(value) -> str:
    return str(value or "").strip().lower()


class SalesSync:
    def __init__(self, service):
        self.service = service
        self.integration = service.integration

    def client_for(self, node):
        return self.service._lookup(self.service._client_cache, Client, node_id(node.get("client")))

    def upsert_request(self, node):
        JobberRequest.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": self.client_for(node),
                "title": clip(node.get("title"), 500),
                "request_status": clip(_lower(node.get("requestStatus")), 64),
                "source": clip(node.get("source"), 120),
                "jobber_web_uri": clip(node.get("jobberWebUri"), 1000),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )

    def upsert_quote(self, node):
        amounts = node.get("amounts") or {}
        request_id = node_id(node.get("request"))
        Quote.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": self.client_for(node),
                "request": JobberRequest.objects.filter(integration=self.integration, external_id=request_id).first() if request_id else None,
                "quote_number": clip(node.get("quoteNumber"), 64),
                "quote_status": clip(_lower(node.get("quoteStatus")), 64),
                "title": clip(node.get("title"), 500),
                "subtotal": parse_decimal(amounts.get("subtotal")),
                "total": parse_decimal(amounts.get("total")),
                "sent_at": parse_dt(node.get("sentAt")),
                "transitioned_at": parse_dt(node.get("transitionedAt")),
                "jobber_web_uri": clip(node.get("jobberWebUri"), 1000),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )

    def upsert_payment(self, node):
        amount = parse_decimal(node.get("amount")) or 0
        adjustment = _lower(node.get("adjustmentType"))
        kind = f"{adjustment} {_lower(node.get('paymentType'))}"
        if "refund" in kind and amount > 0:
            amount = -amount
        elif any(word in adjustment for word in NOT_MONEY):
            amount = 0
        invoice = self.service._lookup(self.service._invoice_cache, Invoice, node_id(node.get("invoice")))
        client = self.client_for(node) or (invoice.client if invoice else None)
        PaymentRecord.objects.update_or_create(
            integration=self.integration,
            external_id=node["id"],
            defaults={
                "client": client,
                "invoice": invoice,
                "entry_date": parse_dt(node.get("entryDate") or node.get("createdAt")),
                "amount": amount,
                "payment_type": clip(_lower(node.get("paymentType") or node.get("paymentOrigin")), 64),
                "adjustment_type": clip(_lower(node.get("adjustmentType")), 64),
                "source_created_at": parse_dt(node.get("createdAt")),
                "source_updated_at": parse_dt(node.get("updatedAt")),
                "source_payload": node,
            },
        )

    def prune(self, model, seen: set, held: int):
        """Remove records Jobber no longer lists. `held` = how many we had before this pass."""
        existing = model.objects.filter(integration=self.integration)
        if not held or len(seen) < held * PRUNE_MIN_SHARE:
            return 0
        removed, _ = existing.exclude(external_id__in=seen).delete()
        return removed

    def run(self) -> dict:
        from insights.models import InsightsSetting

        status = InsightsSetting.get(STATUS_KEY, {}) or {}
        try:
            schema = Schema(self.service.client)
        except JobberAPIError as exc:
            logger.warning("Sales sync: schema lookup failed: %s", exc)
            return status
        for root, upsert in (("requests", self.upsert_request), ("quotes", self.upsert_quote), ("paymentRecords", self.upsert_payment)):
            now = timezone.now().isoformat()
            try:
                selection, names = schema.selection(root)
                missing = REQUIRED[root] - names
                if missing:
                    raise JobberAPIError(f"Jobber's {root} have no {', '.join(sorted(missing))} field.")
                count, seen = 0, set()
                held = MODELS[root].objects.filter(integration=self.integration).count()
                for node in _paginate(self.service.client, root, selection):
                    if node.get("id"):
                        upsert(node)
                        seen.add(node["id"])
                        count += 1
                self.service._record(root, count)
                removed = self.prune(MODELS[root], seen, held)
                status[root] = {"ok": True, "count": count, "removed": removed, "at": now, "fields": sorted(names)}
                if root == "paymentRecords":
                    # Shown in the sync status so the payment types can be checked after the first sync.
                    status[root]["types"] = sorted(
                        {f"{a or '-'} / {t or '-'}" for a, t in PaymentRecord.objects.filter(integration=self.integration).values_list("adjustment_type", "payment_type").distinct()}
                    )[:30]
            except JobberAPIError as exc:
                logger.warning("Sales sync: %s failed: %s", root, exc)
                status[root] = {"ok": False, "error": _friendly(exc), "at": now}
        InsightsSetting.put(STATUS_KEY, status)
        return status


MODELS = {"requests": JobberRequest, "quotes": Quote, "paymentRecords": PaymentRecord}
