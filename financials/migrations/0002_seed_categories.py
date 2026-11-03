"""Starting categories from the client's list (they can be renamed or extended in Financials → Categories)."""

from django.db import migrations

# (group, name, system_key, payroll, labour, in profit & loss)
CATEGORIES = [
    ("cogs", "Cleaner wages and employer payroll costs", "payroll_cleaning", True, True, True),
    ("cogs", "Subcontractors", "subcontractors", False, True, True),
    ("cogs", "Cleaning supplies", "", False, False, True),
    ("cogs", "Other direct cleaning expenses", "", False, False, True),
    ("opex", "Office and management payroll", "payroll_office", True, False, True),
    ("opex", "Rent and utilities", "", False, False, True),
    ("opex", "Software and subscriptions", "", False, False, True),
    ("opex", "Marketing and advertising", "marketing", False, False, True),
    ("opex", "Insurance", "", False, False, True),
    ("opex", "Office supplies", "", False, False, True),
    ("opex", "Professional services", "", False, False, True),
    ("opex", "Recruitment and training", "", False, False, True),
    ("opex", "Bank and processing fees", "", False, False, True),
    ("vehicles", "Fuel", "", False, False, True),
    ("vehicles", "Maintenance and repairs", "", False, False, True),
    ("vehicles", "Vehicle insurance", "", False, False, True),
    ("vehicles", "Registration", "", False, False, True),
    ("vehicles", "Other vehicle expenses", "", False, False, True),
    ("other", "Loan interest", "loan_interest", False, False, True),
    ("other", "Loan principal payments", "loan_principal", False, False, False),
    ("other", "Credit card payments", "credit_card_payments", False, False, False),
    ("other", "Equipment purchases", "equipment_purchases", False, False, False),
    ("other", "Bank transfers", "bank_transfers", False, False, False),
    ("other", "Tax remittances", "tax_remittances", False, False, False),
]


def seed(apps, schema_editor):
    Category = apps.get_model("financials", "Category")
    order = {}
    for group, name, key, payroll, labour, in_pl in CATEGORIES:
        order[group] = order.get(group, 0) + 10
        Category.objects.get_or_create(
            group=group,
            name=name,
            parent=None,
            defaults={
                "system_key": key,
                "counts_as_payroll": payroll,
                "counts_as_labour": labour,
                "in_profit_and_loss": in_pl,
                "sort_order": order[group],
            },
        )


class Migration(migrations.Migration):
    dependencies = [("financials", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
