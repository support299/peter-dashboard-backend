"""Nightly Master Dashboard refresh: revenue classification, touch-ups, MRR."""

import logging

from django.db.models import Q

from insights.models import InsightsSetting
from insights.mrr import rebuild_mrr
from operations.models import Client, Visit
from operations.revenue import reclassify_all

logger = logging.getLogger(__name__)

TOUCH_UP_PATTERN = r"touch[- ]?ups?"
INTERNAL_KEY = "internal_client_keywords"
# The company's own Jobber client records (team leader tasks, inventory restock).
DEFAULT_INTERNAL_KEYWORDS = ["clean on the go"]


def flag_internal_clients(integration) -> int:
    """
    Mark the company's own client records as internal so they're left out of the
    Master Dashboard. Keywords live in InsightsSetting "internal_client_keywords";
    clients can also be ticked by hand in Django admin (never auto-unticked).
    """
    keywords = InsightsSetting.get(INTERNAL_KEY) or DEFAULT_INTERNAL_KEYWORDS
    match = Q()
    for keyword in keywords:
        match |= Q(name__icontains=keyword) | Q(company_name__icontains=keyword)
    return Client.objects.filter(integration=integration, is_internal=False).filter(match).update(is_internal=True)


def flag_touch_ups(integration) -> int:
    qs = Visit.objects.filter(integration=integration)
    qs.filter(is_touch_up=True).exclude(title__iregex=TOUCH_UP_PATTERN).update(is_touch_up=False)
    return qs.filter(is_touch_up=False, title__iregex=TOUCH_UP_PATTERN).update(is_touch_up=True)


def rebuild_insights(integration) -> dict:
    stats = {
        "internal_clients_flagged": flag_internal_clients(integration),
        "revenue": reclassify_all(integration),
        "touch_ups_flagged": flag_touch_ups(integration),
    }
    stats["mrr"] = rebuild_mrr(integration)
    logger.info("Insights rebuilt: %s", stats)
    return stats
