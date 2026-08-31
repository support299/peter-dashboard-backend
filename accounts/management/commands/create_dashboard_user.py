from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update a dashboard login user (email + password)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--username", default="")
        parser.add_argument("--staff", action="store_true", help="Also grant Django admin access")

    def handle(self, *args, **options):
        User = get_user_model()
        email = options["email"].strip().lower()
        password = options["password"]
        username = (options["username"] or email).strip()
        if not email or not password:
            raise CommandError("email and password are required")

        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=username).first()
        created = False
        if user is None:
            user = User(username=username, email=email)
            created = True
        else:
            user.email = email
            user.username = username or user.username

        user.set_password(password)
        user.is_active = True
        # Dashboard users need staff for privileged ops (celery, disconnect, cancel process).
        # --staff also grants Django admin + superuser.
        if options["staff"]:
            user.is_staff = True
            user.is_superuser = True
        else:
            # Still mark staff so privileged ops work for normal dashboard operators.
            user.is_staff = True
        user.save()
        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} user {user.username} <{user.email}>"))
