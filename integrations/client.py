import logging
import time

import requests
from django.conf import settings

from integrations.models import Integration
from integrations.oauth import JobberOAuthError, get_access_token, refresh_access_token
from integrations.versioning import normalize_versioning

logger = logging.getLogger(__name__)


class JobberAPIError(Exception):
    def __init__(self, message, *, status_code=None, errors=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []
        self.body = body


class JobberThrottled(JobberAPIError):
    def __init__(self, message, *, cost=None, **kwargs):
        super().__init__(message, **kwargs)
        self.cost = cost or {}


class JobberQueryTooExpensive(JobberAPIError):
    """requestedQueryCost is above maximumAvailable; this query will never resolve."""


def parse_cost(payload: dict | None) -> dict:
    extensions = (payload or {}).get("extensions") or {}
    cost = extensions.get("cost") or {}
    throttle = cost.get("throttleStatus") or {}
    return {
        "requested": int(cost.get("requestedQueryCost") or 0),
        "actual": int(cost.get("actualQueryCost") or 0),
        "available": throttle.get("currentlyAvailable"),
        "maximum": int(throttle.get("maximumAvailable") or 10000),
        "restore": int(throttle.get("restoreRate") or 500),
    }


class JobberClient:
    def __init__(self, integration: Integration):
        self.integration = integration
        self.api_url = settings.JOBBER["API_URL"]
        self.version = settings.JOBBER["GRAPHQL_VERSION"]
        self.last_versioning: dict = {}
        self.last_cost: dict = {
            "requested": 0,
            "actual": 0,
            "available": None,
            "maximum": 10000,
            "restore": 500,
        }
        self._cost_observed_at = 0.0

    def _persist_versioning(self, extensions: dict | None):
        info = normalize_versioning(extensions, self.version)
        self.last_versioning = info
        if info.get("warning"):
            logger.warning("Jobber API version warning: %s", info["warning"])

        integration = self.integration
        updates = []
        if integration.requested_api_version != info["requested"]:
            integration.requested_api_version = info["requested"]
            updates.append("requested_api_version")
        if info["served"] and integration.served_api_version != info["served"]:
            integration.served_api_version = info["served"]
            updates.append("served_api_version")
        if info["warning"] and integration.version_warning != info["warning"]:
            integration.version_warning = info["warning"]
            updates.append("version_warning")
        elif not info["warning"] and info["served"] and not info["mismatch"] and integration.version_warning:
            integration.version_warning = ""
            updates.append("version_warning")
        if updates:
            updates.append("updated_at")
            integration.save(update_fields=updates)

    def _estimate_available(self) -> float | None:
        available = self.last_cost.get("available")
        if available is None:
            return None
        restore = self.last_cost.get("restore") or 500
        maximum = self.last_cost.get("maximum") or 10000
        elapsed = max(time.monotonic() - self._cost_observed_at, 0)
        return min(maximum, float(available) + elapsed * restore)

    def _wait_for_points(self, needed: int):
        if needed <= 0:
            return
        maximum = self.last_cost.get("maximum") or 10000
        if needed > maximum:
            raise JobberQueryTooExpensive(
                f"Query costs {needed} points, above maximumAvailable {maximum}. "
                "Reduce page size or nested connections."
            )
        available = self._estimate_available()
        if available is None or available >= needed:
            return
        restore = self.last_cost.get("restore") or 500
        wait_for = ((needed - available) / restore) + 0.2
        wait_for = min(max(wait_for, 0.2), 30)
        logger.info(
            "Jobber leaky bucket: need %s, have ~%.0f, restore %s/s; sleeping %.2fs",
            needed,
            available,
            restore,
            wait_for,
        )
        time.sleep(wait_for)

    def _remember_cost(self, payload: dict | None):
        cost = parse_cost(payload)
        if cost.get("available") is not None or cost.get("requested"):
            self.last_cost = cost
            self._cost_observed_at = time.monotonic()

    def execute(self, query: str, variables: dict | None = None, retries: int = 8) -> dict:
        last_error = None
        refreshed = False
        predicted_cost = int(self.last_cost.get("requested") or 0)

        for attempt in range(retries):
            if predicted_cost:
                self._wait_for_points(predicted_cost)

            token = get_access_token(self.integration)
            response = requests.post(
                self.api_url,
                json={"query": query, "variables": variables or {}},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-JOBBER-GRAPHQL-VERSION": self.version,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=60,
            )

            if response.status_code == 401 and not refreshed:
                logger.info("Jobber returned 401; refreshing access token.")
                try:
                    refresh_access_token(self.integration)
                    self.integration.refresh_from_db()
                    refreshed = True
                    continue
                except JobberOAuthError as exc:
                    raise JobberAPIError(str(exc), status_code=401) from exc

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After") or "12"
                try:
                    wait_for = min(max(float(retry_after), 1), 60)
                except ValueError:
                    wait_for = 12
                logger.warning("Jobber DDoS limiter (429); sleeping %.1fs", wait_for)
                time.sleep(wait_for)
                last_error = JobberAPIError("429 Too Many Requests", status_code=429)
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise JobberAPIError(
                    f"Jobber returned non-JSON ({response.status_code})",
                    status_code=response.status_code,
                    body=response.text[:1000],
                ) from exc

            self._persist_versioning(payload.get("extensions") or {})
            self._remember_cost(payload)
            cost = self.last_cost
            predicted_cost = cost.get("requested") or predicted_cost

            errors = payload.get("errors") or []
            if errors:
                codes = [((err.get("extensions") or {}).get("code") or "").upper() for err in errors]
                messages = "; ".join(err.get("message", "unknown") for err in errors)
                throttled = "THROTTLED" in codes or "throttl" in messages.lower()
                if throttled:
                    requested = cost.get("requested") or 0
                    maximum = cost.get("maximum") or 10000
                    if requested > maximum:
                        raise JobberQueryTooExpensive(
                            f"Throttled: requestedQueryCost {requested} exceeds maximumAvailable {maximum}",
                            status_code=response.status_code,
                            errors=errors,
                            body=payload,
                        )
                    self._wait_for_points(max(requested, 1))
                    last_error = JobberThrottled(
                        messages,
                        status_code=response.status_code,
                        errors=errors,
                        body=payload,
                        cost=cost,
                    )
                    continue
                raise JobberAPIError(messages, status_code=response.status_code, errors=errors, body=payload)

            if response.status_code >= 400:
                last_error = JobberAPIError(
                    f"Jobber HTTP {response.status_code}",
                    status_code=response.status_code,
                    body=payload,
                )
                time.sleep(min(2 ** attempt, 8))
                continue

            return payload.get("data") or {}

        raise last_error or JobberAPIError("Jobber request failed after retries")

    def paginate(self, connection_name: str, node_selection: str, page_size: int | None = None, extra_args: str = ""):
        """Yield nodes from a root Relay connection. Always sends `first` so Jobber does not assume 100 nodes."""
        page_size = page_size or settings.JOBBER["PAGE_SIZE"]
        cursor = None
        extra = f", {extra_args}" if extra_args else ""
        query = f"""
        query SyncPage($first: Int!, $after: String) {{
          {connection_name}(first: $first, after: $after{extra}) {{
            nodes {{
              {node_selection}
            }}
            pageInfo {{
              hasNextPage
              endCursor
            }}
            totalCount
          }}
        }}
        """
        while True:
            try:
                data = self.execute(query, {"first": page_size, "after": cursor})
            except JobberQueryTooExpensive:
                if page_size <= 1:
                    raise
                page_size = max(1, page_size // 2)
                logger.warning(
                    "Jobber query for `%s` too expensive; retrying with first=%s",
                    connection_name,
                    page_size,
                )
                continue
            connection = data.get(connection_name) or {}
            nodes = connection.get("nodes") or []
            for node in nodes:
                if node:
                    yield node
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
