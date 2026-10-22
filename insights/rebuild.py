"""Nightly Master Dashboard refresh: revenue classification, touch-ups, MRR."""

import logging

from insights.mrr import rebuild_mrr
from operations.models import Visit
from operations.revenue import reclassify_all

logger = logging.getLogger(__name__)

TOUCH_UP_PATTERN = r"touch[- ]?ups?"


def flag_touch_ups(integration) -> int:
    qs = Visit.objects.filter(integration=integration)
    qs.filter(is_touch_up=True).exclude(title__iregex=TOUCH_UP_PATTERN).update(is_touch_up=False)
    return qs.filter(is_touch_up=False, title__iregex=TOUCH_UP_PATTERN).update(is_touch_up=True)


def rebuild_insights(integration) -> dict:
    stats = {"revenue": reclassify_all(integration), "touch_ups_flagged": flag_touch_ups(integration)}
    stats["mrr"] = rebuild_mrr(integration)
    logger.info("Insights rebuilt: %s", stats)
    return stats
