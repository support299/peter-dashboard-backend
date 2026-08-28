"""Process Jobber cancellation tasks into CancellationRecord rows."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db.models import Avg
from django.utils import timezone

from operations.models import (
    CANCELLATION_JOB,
    CANCELLATION_VISIT,
    CancellationRecord,
    Client,
    Job,
    JobberTask,
    Visit,
)
from operations.taxonomy import cancellation_type_from_title, classify_division

logger = logging.getLogger(__name__)


def _client_avg_price(client: Client | None, integration) -> Decimal | None:
    if not client:
        return None
    if client.average_price_per_visit is not None:
        return client.average_price_per_visit
    agg = (
        Visit.objects.filter(integration=integration, client=client, price_per_visit__isnull=False)
        .exclude(is_cancelled=True)
        .aggregate(avg=Avg("price_per_visit"))
    )
    return agg["avg"]


def _monthly_value(client: Client | None, job: Job | None) -> Decimal | None:
    if job and job.monthly_recurring_value is not None:
        return job.monthly_recurring_value
    if client and client.monthly_recurring_value is not None:
        return client.monthly_recurring_value
    if job and job.is_recurring and job.total is not None:
        return job.total
    if client:
        recurring = (
            Job.objects.filter(client=client, is_recurring=True, total__isnull=False)
            .order_by("-source_updated_at", "-id")
            .first()
        )
        if recurring and recurring.total is not None:
            return recurring.total
    return None


def process_cancellation_task(task: JobberTask, integration) -> CancellationRecord | None:
    cancel_type = cancellation_type_from_title(task.title)
    if not cancel_type:
        return None

    existing = CancellationRecord.objects.filter(
        integration=integration,
        jobber_task_external_id=task.external_id,
    ).first()
    if existing:
        return existing

    client = task.client
    job = task.job
    if not job and client:
        job = (
            Job.objects.filter(client=client, is_recurring=True)
            .order_by("-source_updated_at", "-id")
            .first()
        )

    if cancel_type == CANCELLATION_VISIT:
        value = _client_avg_price(client, integration)
        is_lost = False
    else:
        value = _monthly_value(client, job)
        is_lost = True

    division = (
        (job.division if job else "")
        or (client.division if client else "")
        or classify_division(task.title, client.name if client else "", job.title if job else "")
    )
    task_date = None
    if task.due_at:
        task_date = timezone.localdate(task.due_at)
    elif task.source_created_at:
        task_date = timezone.localdate(task.source_created_at)
    else:
        task_date = timezone.localdate()

    record = CancellationRecord.objects.create(
        integration=integration,
        jobber_task=task,
        jobber_task_external_id=task.external_id,
        cancellation_type=cancel_type,
        client=client,
        client_name=(client.name if client else "") or "",
        job=job,
        task_date=task_date,
        value=value,
        division=division or "",
        is_lost_client=is_lost,
        source_payload=task.source_payload or {},
    )
    logger.info(
        "Created %s for task %s client=%s value=%s",
        cancel_type,
        task.external_id,
        record.client_name,
        value,
    )
    return record


def process_all_cancellation_tasks(integration) -> dict:
    created = 0
    skipped = 0
    for task in JobberTask.objects.filter(integration=integration).iterator():
        if not cancellation_type_from_title(task.title):
            continue
        before = CancellationRecord.objects.filter(
            integration=integration, jobber_task_external_id=task.external_id
        ).exists()
        process_cancellation_task(task, integration)
        if before:
            skipped += 1
        else:
            created += 1
    return {"created": created, "skipped": skipped}
