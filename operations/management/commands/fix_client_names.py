from django.core.management.base import BaseCommand

from integrations.mapping import person_display_name
from operations.models import Client
from operations.views import _clean_name, _looks_like_phone, client_display_name


class Command(BaseCommand):
    help = "Clear phone numbers stored in Client.name and rebuild display names from Jobber fields."

    def handle(self, *args, **options):
        updated = 0
        cleared = 0
        for client in Client.objects.iterator():
            payload = client.source_payload if isinstance(client.source_payload, dict) else {}
            rebuilt = person_display_name(payload) or client_display_name(client, include_phone_fallback=False)
            if rebuilt == "Unnamed customer":
                rebuilt = ""

            current_name = _clean_name(client.name)
            next_name = rebuilt or (
                ""
                if current_name and _looks_like_phone(current_name)
                else current_name
            )

            fields = {}
            if next_name != client.name:
                fields["name"] = next_name
                if current_name and _looks_like_phone(current_name):
                    cleared += 1

            payload_first = _clean_name(payload.get("firstName"))
            payload_last = _clean_name(payload.get("lastName"))
            if payload_first and payload_first != client.first_name:
                fields["first_name"] = payload_first
            if payload_last and payload_last != client.last_name:
                fields["last_name"] = payload_last

            if fields:
                Client.objects.filter(pk=client.pk).update(**fields)
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"Updated {updated} clients ({cleared} phone-only names cleared)."))
