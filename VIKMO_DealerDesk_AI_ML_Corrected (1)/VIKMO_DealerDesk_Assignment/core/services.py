from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import ValidationError

from .models import Dealer, Product, Order, OrderItem
from .utils import generate_order_number

CENT = Decimal("0.01")
GST = Decimal("0.18")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_line(unit_price, quantity):
    subtotal = money(Decimal(unit_price) * quantity)
    tax = money(subtotal * GST)
    return subtotal, tax, money(subtotal + tax)


def recalculate_order(order, *, current_catalogue_prices=False):
    """Recompute line and order totals without changing timestamps on read-only GETs."""
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    changed_lines = False
    for line in order.items.select_related("product").all():
        updates = []
        if current_catalogue_prices:
            if line.unit_price != line.product.price:
                line.unit_price = line.product.price
                updates.append("unit_price")
            if line.name != line.product.name:
                line.name = line.product.name
                updates.append("name")
        line_subtotal, line_tax, line_total = calculate_line(line.unit_price, line.quantity)
        for field, value in (("line_subtotal", line_subtotal), ("line_tax", line_tax), ("line_total", line_total)):
            if getattr(line, field) != value:
                setattr(line, field, value)
                updates.append(field)
        if updates:
            line.save(update_fields=updates)
            changed_lines = True
        subtotal += line.line_subtotal
        tax_total += line.line_tax

    totals = {
        "subtotal": money(subtotal),
        "tax_total": money(tax_total),
    }
    totals["grand_total"] = money(totals["subtotal"] + totals["tax_total"])
    changed_fields = [field for field, value in totals.items() if getattr(order, field) != value]
    for field, value in totals.items():
        setattr(order, field, value)
    # A changed line should mark the order as updated even if rounding leaves its totals
    # unchanged. Otherwise, a GET that changes nothing must not mutate updated_at.
    if changed_fields or changed_lines:
        order.save(update_fields=[*changed_fields, "updated_at"] if changed_fields else ["updated_at"])
    return order


@transaction.atomic
def create_draft_order(dealer_id, items):
    """Single validated order-creation path shared by REST API and assistant."""
    try:
        dealer = Dealer.objects.get(pk=dealer_id)
    except (Dealer.DoesNotExist, TypeError, ValueError):
        raise ValidationError({"dealer_id": "Dealer does not exist."})
    if not isinstance(items, list) or not items:
        raise ValidationError({"items": "At least one item is required."})

    seen = set()
    normalised = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError({"items": f"Item {index + 1} must be an object."})
        sku = item.get("sku")
        qty = item.get("quantity")
        if not isinstance(sku, str) or not sku.strip():
            raise ValidationError({"items": f"SKU is required for item {index + 1}."})
        if sku in seen:
            raise ValidationError({"items": f"Duplicate SKU {sku} in order."})
        seen.add(sku)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValidationError({"items": f"Quantity for {sku} must be an integer greater than or equal to 1."})
        normalised.append((sku, qty))

    # Lock products in a stable order; this also protects pricing from simultaneous updates.
    products = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=seen).order_by("sku")}
    missing = [sku for sku, _ in normalised if sku not in products]
    if missing:
        raise ValidationError({"items": f"Unknown SKU(s): {', '.join(missing)}."})

    order = Order.objects.create(dealer=dealer, order_number=generate_order_number(), status="DRAFT")
    for sku, qty in normalised:
        product = products[sku]
        line_subtotal, line_tax, line_total = calculate_line(product.price, qty)
        OrderItem.objects.create(
            order=order, product=product, name=product.name, quantity=qty,
            unit_price=product.price, line_subtotal=line_subtotal,
            line_tax=line_tax, line_total=line_total,
        )
    return recalculate_order(order)
