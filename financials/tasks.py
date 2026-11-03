from celery import shared_task

from financials.recurring import generate_all


@shared_task(name="financials.tasks.generate_recurring_expenses")
def generate_recurring_expenses():
    """Create this week's recurring expenses (waiting to be verified)."""
    return generate_all()
