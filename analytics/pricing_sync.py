import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from analytics.parsing import as_dict, as_list, parse_d, parse_decimal, parse_dt
from analytics.pricing_models import (
    PricingAddon,
    PricingBundle,
    PricingCoupon,
    PricingLocation,
    PricingPackage,
    PricingService,
    PricingSubmission,
    PricingSyncRun,
)

logger = logging.getLogger(__name__)


class PricingCalculatorAPIError(Exception):
    pass


def _config():
    return settings.PRICING_CALCULATOR_APP


def fetch_pricing_payload():
    cfg = _config()
    base = (cfg.get("BASE_URL") or "").rstrip("/")
    path = cfg.get("ANALYTICS_PATH") or "/api/pricing-calculator/analytics/"
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base}{path}"
    api_key = cfg.get("API_KEY") or ""
    if not base:
        raise PricingCalculatorAPIError("PRICING_CALCULATOR_APP_BASE_URL is not configured.")
    if not api_key:
        raise PricingCalculatorAPIError("PRICING_CALCULATOR_APP_API_KEY is not configured.")

    timeout = int(cfg.get("TIMEOUT") or 90)
    response = requests.get(url, headers={"X-API-Key": api_key}, timeout=timeout)
    if response.status_code >= 400:
        raise PricingCalculatorAPIError(
            f"Upstream returned {response.status_code}: {response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PricingCalculatorAPIError("Upstream response was not valid JSON.") from exc


def _prune(model, keep_ids):
    if not keep_ids:
        deleted, _ = model.objects.all().delete()
        return deleted
    deleted, _ = model.objects.exclude(external_id__in=keep_ids).delete()
    return deleted


def _upsert_submissions(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        customer = as_dict(row.get("customer"))
        prop = as_dict(row.get("property"))
        location = as_dict(row.get("location"))
        pricing = as_dict(row.get("pricing"))
        coupon = as_dict(row.get("coupon"))
        bundle = as_dict(row.get("bundle"))
        flags = as_dict(row.get("flags"))
        services = [
            sel.get("service_name")
            for sel in as_list(row.get("service_selections"))
            if sel.get("service_name")
        ]
        PricingSubmission.objects.update_or_create(
            external_id=external_id,
            defaults={
                "status": row.get("status") or "",
                "is_deleted": bool(row.get("is_deleted")),
                "customer_first_name": customer.get("first_name") or "",
                "customer_last_name": customer.get("last_name") or "",
                "customer_company": customer.get("company_name") or "",
                "customer_email": customer.get("email") or "",
                "customer_phone": customer.get("phone") or "",
                "postal_code": customer.get("postal_code") or "",
                "street_address": customer.get("street_address") or "",
                "heard_about_us": customer.get("heard_about_us") or "",
                "is_previous_customer": bool(customer.get("is_previous_customer")),
                "property_type": prop.get("type") or "",
                "property_name": prop.get("name") or "",
                "num_floors": str(prop.get("num_floors") or ""),
                "actual_sqft": parse_decimal(prop.get("actual_sqft")),
                "size_range": prop.get("size_range") or "",
                "location_external_id": str(location.get("id") or ""),
                "location_name": location.get("name") or "",
                "total_base_price": parse_decimal(pricing.get("total_base_price")),
                "total_adjustments": parse_decimal(pricing.get("total_adjustments")),
                "total_surcharges": parse_decimal(pricing.get("total_surcharges")),
                "total_addons_price": parse_decimal(pricing.get("total_addons_price")),
                "discounted_amount": parse_decimal(pricing.get("discounted_amount")),
                "bundle_discount_amount": parse_decimal(pricing.get("bundle_discount_amount")),
                "final_total": parse_decimal(pricing.get("final_total")),
                "coupon_applied": bool(coupon.get("applied")),
                "coupon_code": coupon.get("code") or "",
                "bundle_applied": bool(bundle.get("applied")),
                "bundle_name": bundle.get("name") or "",
                "is_bid_in_person": bool(flags.get("is_bid_in_person")),
                "is_on_the_go": bool(flags.get("is_on_the_go")),
                "service_names": services,
                "quote_url": row.get("quote_url") or "",
                "expires_at": parse_dt(row.get("expires_at")),
                "source_created_at": parse_dt(row.get("created_at")),
                "source_updated_at": parse_dt(row.get("updated_at")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_services(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingService.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": row.get("name") or "",
                "description": row.get("description") or "",
                "is_active": bool(row.get("is_active", True)),
                "is_commercial": bool(row.get("is_commercial")),
                "is_residential": bool(row.get("is_residential")),
                "sort_order": int(row.get("order") or 0),
                "icon_url": row.get("icon_url") or "",
                "source_payload": row,
            },
        )
    return seen


def _upsert_packages(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingPackage.objects.update_or_create(
            external_id=external_id,
            defaults={
                "service_external_id": str(row.get("service_id") or ""),
                "service_name": row.get("service_name") or "",
                "name": row.get("name") or "",
                "base_price": parse_decimal(row.get("base_price")),
                "sort_order": int(row.get("order") or 0),
                "is_active": bool(row.get("is_active", True)),
                "source_payload": row,
            },
        )
    return seen


def _upsert_locations(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingLocation.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": row.get("name") or "",
                "address": row.get("address") or "",
                "latitude": str(row.get("latitude") or ""),
                "longitude": str(row.get("longitude") or ""),
                "trip_surcharge": parse_decimal(row.get("trip_surcharge")),
                "is_active": bool(row.get("is_active", True)),
                "source_payload": row,
            },
        )
    return seen


def _upsert_addons(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingAddon.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": row.get("name") or "",
                "description": row.get("description") or "",
                "base_price": parse_decimal(row.get("base_price")),
                "is_global": bool(row.get("is_global")),
                "service_ids": as_list(row.get("service_ids")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_coupons(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingCoupon.objects.update_or_create(
            external_id=external_id,
            defaults={
                "code": row.get("code") or "",
                "percentage_discount": parse_decimal(row.get("percentage_discount")),
                "fixed_discount": parse_decimal(row.get("fixed_discount")),
                "expiration_date": parse_dt(row.get("expiration_date")),
                "used_count": int(row.get("used_count") or 0),
                "is_active": bool(row.get("is_active", True)),
                "is_global": bool(row.get("is_global")),
                "source_payload": row,
            },
        )
    return seen


def _upsert_bundles(rows):
    seen = set()
    for row in rows:
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            continue
        seen.add(external_id)
        PricingBundle.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": row.get("name") or "",
                "description": row.get("description") or "",
                "discount_type": row.get("discount_type") or "",
                "discount_percentage": parse_decimal(row.get("discount_percentage")),
                "discount_fixed": parse_decimal(row.get("discount_fixed")),
                "is_active": bool(row.get("is_active", True)),
                "service_ids": as_list(row.get("service_ids")),
                "source_payload": row,
            },
        )
    return seen


def persist_payload(payload, sync_run: PricingSyncRun):
    catalog = as_dict(payload.get("catalog"))
    with transaction.atomic():
        submission_ids = _upsert_submissions(as_list(payload.get("submissions")))
        service_ids = _upsert_services(as_list(catalog.get("services")))
        package_ids = _upsert_packages(as_list(catalog.get("packages")))
        location_ids = _upsert_locations(as_list(catalog.get("locations")))
        addon_ids = _upsert_addons(as_list(catalog.get("addons")))
        coupon_ids = _upsert_coupons(as_list(catalog.get("coupons")))
        bundle_ids = _upsert_bundles(as_list(catalog.get("bundles")))

        _prune(PricingSubmission, submission_ids)
        _prune(PricingService, service_ids)
        _prune(PricingPackage, package_ids)
        _prune(PricingLocation, location_ids)
        _prune(PricingAddon, addon_ids)
        _prune(PricingCoupon, coupon_ids)
        _prune(PricingBundle, bundle_ids)

        sync_run.source_generated_at = parse_dt(payload.get("generated_at"))
        sync_run.summary = as_dict(payload.get("summary"))
        sync_run.counts = {
            "submissions": len(submission_ids),
            "services": len(service_ids),
            "packages": len(package_ids),
            "locations": len(location_ids),
            "addons": len(addon_ids),
            "coupons": len(coupon_ids),
            "bundles": len(bundle_ids),
        }
        sync_run.status = PricingSyncRun.STATUS_SUCCESS
        sync_run.finished_at = timezone.now()
        sync_run.error = ""
        sync_run.save()


def run_pricing_sync(sync_run: PricingSyncRun | None = None) -> PricingSyncRun:
    sync_run = sync_run or PricingSyncRun.objects.create(status=PricingSyncRun.STATUS_QUEUED)
    sync_run.status = PricingSyncRun.STATUS_RUNNING
    sync_run.started_at = timezone.now()
    sync_run.error = ""
    sync_run.save(update_fields=["status", "started_at", "error"])

    try:
        payload = fetch_pricing_payload()
        persist_payload(payload, sync_run)
        logger.info("Pricing sync %s succeeded: %s", sync_run.id, sync_run.counts)
    except Exception as exc:
        logger.exception("Pricing sync %s failed", sync_run.id)
        sync_run.status = PricingSyncRun.STATUS_FAILED
        sync_run.finished_at = timezone.now()
        sync_run.error = str(exc)
        sync_run.save(update_fields=["status", "finished_at", "error"])
    return sync_run


def latest_successful_pricing_sync():
    return PricingSyncRun.objects.filter(status=PricingSyncRun.STATUS_SUCCESS).first()


def latest_pricing_sync():
    return PricingSyncRun.objects.first()
