from datetime import date
from django.db import connection, transaction
from django.utils import timezone
from .models import Order


def generate_order_number() -> str:
    """Generate ORD-YYYYMMDD-NNNN using a PostgreSQL transaction advisory lock.

    Call inside the order-creation transaction. One lock per IST day serialises the
    sequence allocation and prevents two concurrent requests selecting the same number.
    """
    today: date = timezone.localdate()
    date_key = int(today.strftime("%Y%m%d"))
    prefix = f"ORD-{today:%Y%m%d}-"
    # This app deliberately requires PostgreSQL as specified by the assignment.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [date_key])
    previous = Order.objects.filter(order_number__startswith=prefix).order_by("-order_number").values_list("order_number", flat=True).first()
    sequence = int(previous.rsplit("-", 1)[1]) + 1 if previous else 1
    return f"{prefix}{sequence:04d}"
