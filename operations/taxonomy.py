"""Division + one-off service type classification from titles/keywords."""

from __future__ import annotations

from functools import lru_cache

from operations.models import (
    DIVISION_CHOICES,
    DivisionRule,
    ServiceTypeMapping,
)

DIVISION_LABELS = {key: label for key, label in DIVISION_CHOICES}
UNCATEGORIZED = "Other / Uncategorized"

DEFAULT_SERVICE_MAPPINGS = [
    ("window", "Window Cleaning", 10),
    ("carpet", "Carpet Cleaning", 20),
    ("upholstery", "Upholstery Cleaning", 30),
    ("pressure wash", "Pressure Washing", 40),
    ("pressure washing", "Pressure Washing", 41),
    ("soft wash", "Soft Washing", 50),
    ("soft washing", "Soft Washing", 51),
    ("gutter", "Gutter Cleaning", 60),
    ("floor care", "Floor Care", 70),
    ("floor", "Floor Care", 71),
    ("move-in", "Move-In / Move-Out Cleaning", 80),
    ("move in", "Move-In / Move-Out Cleaning", 81),
    ("move-out", "Move-In / Move-Out Cleaning", 82),
    ("move out", "Move-In / Move-Out Cleaning", 83),
    ("post-renovation", "Post-Renovation Cleaning", 90),
    ("post renovation", "Post-Renovation Cleaning", 91),
    ("commercial clean", "Commercial Cleaning", 100),
    ("office clean", "Commercial Cleaning", 101),
    ("residential clean", "Residential Cleaning", 110),
    ("house clean", "Residential Cleaning", 111),
    ("home clean", "Residential Cleaning", 112),
]

DEFAULT_DIVISION_RULES = [
    ("commercial", "commercial", 10),
    ("office", "commercial", 20),
    ("business", "commercial", 30),
    ("rental", "rentals", 40),
    ("tenant", "rentals", 50),
    ("specialized", "specialized", 60),
    ("window", "specialized", 70),
    ("carpet", "specialized", 71),
    ("pressure", "specialized", 72),
    ("gutter", "specialized", 73),
    ("soft wash", "specialized", 74),
    ("residential", "residential", 100),
    ("residence", "residential", 101),
    ("home", "residential", 102),
    ("house", "residential", 103),
]

CANCELLED_VISIT_TITLES = {"cancelled visit", "canceled visit"}
CANCELLED_JOB_TITLES = {"cancelled job", "canceled job"}


def seed_taxonomy_defaults():
    if not ServiceTypeMapping.objects.exists():
        ServiceTypeMapping.objects.bulk_create(
            [
                ServiceTypeMapping(keyword=k, service_type=s, priority=p, active=True)
                for k, s, p in DEFAULT_SERVICE_MAPPINGS
            ]
        )
    if not DivisionRule.objects.exists():
        DivisionRule.objects.bulk_create(
            [
                DivisionRule(keyword=k, division=d, priority=p, active=True)
                for k, d, p in DEFAULT_DIVISION_RULES
            ]
        )
    clear_taxonomy_cache()


def clear_taxonomy_cache():
    _service_rules.cache_clear()
    _division_rules.cache_clear()


@lru_cache(maxsize=1)
def _service_rules():
    return list(
        ServiceTypeMapping.objects.filter(active=True)
        .order_by("priority", "id")
        .values_list("keyword", "service_type")
    )


@lru_cache(maxsize=1)
def _division_rules():
    return list(
        DivisionRule.objects.filter(active=True)
        .order_by("priority", "id")
        .values_list("keyword", "division")
    )


def classify_service_type(title: str | None) -> str:
    text = (title or "").lower()
    if not text:
        return UNCATEGORIZED
    try:
        rules = _service_rules()
    except Exception:
        rules = [(k, s) for k, s, _ in DEFAULT_SERVICE_MAPPINGS]
    for keyword, service_type in rules:
        if keyword.lower() in text:
            return service_type
    return UNCATEGORIZED


def classify_division(*texts: str | None) -> str:
    blob = " ".join((t or "").lower() for t in texts if t)
    if not blob:
        return ""
    try:
        rules = _division_rules()
    except Exception:
        rules = [(k, d) for k, d, _ in DEFAULT_DIVISION_RULES]
    for keyword, division in rules:
        if keyword.lower() in blob:
            return division
    return "residential"


def cancellation_type_from_title(title: str | None) -> str | None:
    text = (title or "").strip().lower()
    if text in CANCELLED_VISIT_TITLES or "cancelled visit" in text or "canceled visit" in text:
        return "cancelled_visit"
    if text in CANCELLED_JOB_TITLES or "cancelled job" in text or "canceled job" in text:
        return "cancelled_job"
    return None
