import hashlib
import json
import math
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import NotFound, ValidationError

from .api_errors import error_response
from .models import Product, Dealer, Order, OrderItem, IdempotencyRecord
from .serializers import (
    ProductSerializer, DealerSerializer, OrderReadSerializer,
    OrderCreateSerializer, OrderItemInputSerializer, ReplaceOrderItemsSerializer,
)
from .services import recalculate_order, calculate_line
from .utils import generate_order_number
from ml.forecasting import generate_forecast_csv, load_forecast, build_reorder_suggestions


def health(request):
    return JsonResponse({"status": "ok"})


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all().order_by("sku")
    serializer_class = ProductSerializer
    lookup_field = "sku"
    lookup_url_kwarg = "sku"

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        category = self.request.query_params.get("category")
        vehicle = self.request.query_params.get("vehicle")
        if search:
            qs = qs.filter(Q(sku__icontains=search) | Q(name__icontains=search) | Q(brand__icontains=search))
        if category:
            qs = qs.filter(category=category)
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle)
        return qs

    def create(self, request, *args, **kwargs):
        sku = request.data.get("sku") if isinstance(request.data, dict) else None
        if sku and Product.objects.filter(sku=sku).exists():
            return error_response("DUPLICATE", f"Product SKU {sku} already exists.", 409)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Force stock to zero for products created through the catalogue API.
        product = serializer.save(stock=0)
        return Response(self.get_serializer(product).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        requested_sku = request.data.get("sku") if isinstance(request.data, dict) else None
        if requested_sku and requested_sku != instance.sku:
            return error_response("VALIDATION_ERROR", "SKU cannot be changed.", 400)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self.get_serializer(instance).data)


class DealerViewSet(viewsets.ModelViewSet):
    queryset = Dealer.objects.all().order_by("id")
    serializer_class = DealerSerializer

    def create(self, request, *args, **kwargs):
        if not isinstance(request.data, dict):
            return error_response("VALIDATION_ERROR", "Dealer request body must be a JSON object.", 400)
        code = request.data.get("dealer_code")
        if code and Dealer.objects.filter(dealer_code=code).exists():
            return error_response("DUPLICATE", f"Dealer code {code} already exists.", 409)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dealer = serializer.save()
        return Response(self.get_serializer(dealer).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        code = request.data.get("dealer_code") if isinstance(request.data, dict) else None
        if code and Dealer.objects.filter(dealer_code=code).exclude(pk=instance.pk).exists():
            return error_response("DUPLICATE", f"Dealer code {code} already exists.", 409)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self.get_serializer(instance).data)


class OrderViewSet(viewsets.GenericViewSet):
    queryset = Order.objects.all().select_related("dealer").prefetch_related("items__product").order_by("-created_at")
    serializer_class = OrderReadSerializer

    def get_order(self, order_number, *, for_update=False):
        qs = Order.objects.select_related("dealer").prefetch_related("items__product")
        if for_update:
            qs = qs.select_for_update()
        try:
            order = qs.get(order_number=order_number)
        except Order.DoesNotExist:
            raise NotFound("Order not found.")
        return order

    def list(self, request):
        qs = self.get_queryset()
        dealer_id = request.query_params.get("dealer_id")
        status_filter = request.query_params.get("status")
        if dealer_id:
            qs = qs.filter(dealer_id=dealer_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        page = self.paginate_queryset(qs)
        orders = list(page if page is not None else qs)
        # A draft tracks today's catalogue prices; confirmed orders are price-locked.
        for order in orders:
            if order.status == "DRAFT":
                recalculate_order(order, current_catalogue_prices=True)
        serializer = self.get_serializer(orders, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    def retrieve(self, request, pk=None):
        order = self.get_order(pk)
        if order.status == "DRAFT":
            recalculate_order(order, current_catalogue_prices=True)
        return Response(self.get_serializer(order).data)

    def create(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()
        request_hash = hashlib.sha256(json.dumps(request.data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        try:
            with transaction.atomic():
                if key:
                    previous = IdempotencyRecord.objects.select_for_update().filter(key=key).select_related("order").first()
                    if previous:
                        if previous.request_hash != request_hash:
                            return error_response("IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with a different request body.", 409)
                        if previous.order.status == "DRAFT":
                            recalculate_order(previous.order, current_catalogue_prices=True)
                        return Response(self.get_serializer(previous.order).data, status=200)
                serializer = OrderCreateSerializer(data=request.data, context=self.get_serializer_context())
                serializer.is_valid(raise_exception=True)
                order = serializer.save()
                if key:
                    IdempotencyRecord.objects.create(key=key, request_hash=request_hash, order=order)
                return Response(self.get_serializer(order).data, status=status.HTTP_201_CREATED)
        except IntegrityError:
            # A simultaneous retry may win the unique Idempotency-Key insert.
            if key:
                previous = IdempotencyRecord.objects.filter(key=key).select_related("order").first()
                if previous:
                    if previous.request_hash != request_hash:
                        return error_response("IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with a different request body.", 409)
                    return Response(self.get_serializer(previous.order).data, status=200)
            raise

    def update(self, request, pk=None):
        with transaction.atomic():
            order = self.get_order(pk, for_update=True)
            if order.status != "DRAFT":
                return error_response("ORDER_LOCKED", "Only DRAFT orders can be edited.", 409)
            serializer = ReplaceOrderItemsSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            validated_items = serializer.validated_data["items"]
            seen = {item["sku"] for item in validated_items}
            products = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=seen).order_by("sku")}
            if len(products) != len(seen):
                missing = sorted(seen - set(products))
                raise ValidationError({"items": f"Unknown SKU(s): {', '.join(missing)}."})
            order.items.all().delete()
            for item in validated_items:
                product = products[item["sku"]]
                subtotal, tax, total = calculate_line(product.price, item["quantity"])
                OrderItem.objects.create(order=order, product=product, name=product.name, quantity=item["quantity"],
                    unit_price=product.price, line_subtotal=subtotal, line_tax=tax, line_total=total)
            recalculate_order(order)
            return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        with transaction.atomic():
            order = self.get_order(pk, for_update=True)
            if order.status != "DRAFT":
                return error_response("INVALID_TRANSITION", "Can only confirm DRAFT orders.", 409)
            items = list(order.items.select_related("product").all())
            skus = sorted({line.product_id for line in items})
            products = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=skus).order_by("sku")}
            short = []
            for line in items:
                product = products[line.product_id]
                if line.quantity > product.stock:
                    short.append({"sku": product.sku, "requested": line.quantity, "available": product.stock})
            if short:
                sentence_by_sku = {s["sku"]: f"Insufficient stock for {products[s['sku']].name}. Available: {s['available']}, Requested: {s['requested']}" for s in short}
                return error_response("INSUFFICIENT_STOCK", "; ".join(sentence_by_sku[s["sku"]] for s in short), 409, short)
            # Prices follow current catalogue values through draft and lock on confirmation.
            for line in items:
                product = products[line.product_id]
                line.unit_price = product.price
                line.name = product.name
                line.line_subtotal, line.line_tax, line.line_total = calculate_line(product.price, line.quantity)
                line.save(update_fields=["unit_price", "name", "line_subtotal", "line_tax", "line_total"])
            recalculate_order(order)
            for sku, product in products.items():
                qty = sum(line.quantity for line in items if line.product_id == sku)
                Product.objects.filter(sku=sku).update(stock=product.stock - qty)
            order.status = "CONFIRMED"
            order.save(update_fields=["status", "updated_at"])
            return Response(self.get_serializer(order).data)


    @action(detail=True, methods=["post"], url_path="dispatch")
    def dispatch_order(self, request, pk=None):
        with transaction.atomic():
            order = self.get_order(pk, for_update=True)

            if order.status != "CONFIRMED":
                return error_response(
                    "INVALID_TRANSITION",
                    "Can only dispatch CONFIRMED orders.",
                    409,
                )

            order.status = "DISPATCHED"
            order.save(update_fields=["status", "updated_at"])

            return Response(self.get_serializer(order).data)
    @action(detail=True, methods=["post"], url_path="deliver")
    def deliver(self, request, pk=None):
        with transaction.atomic():
            order = self.get_order(pk, for_update=True)
            if order.status != "DISPATCHED":
                return error_response("INVALID_TRANSITION", "Can only deliver DISPATCHED orders.", 409)
            order.status = "DELIVERED"
            order.save(update_fields=["status", "updated_at"])
            return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        with transaction.atomic():
            order = self.get_order(pk, for_update=True)
            if order.status not in ("DRAFT", "CONFIRMED"):
                return error_response("INVALID_TRANSITION", "Only DRAFT or CONFIRMED orders can be cancelled.", 409)
            if order.status == "CONFIRMED":
                lines = list(order.items.all())
                skus = sorted({line.product_id for line in lines})
                products = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=skus).order_by("sku")}
                for sku, product in products.items():
                    restore = sum(line.quantity for line in lines if line.product_id == sku)
                    Product.objects.filter(sku=sku).update(stock=product.stock + restore)
            order.status = "CANCELLED"
            order.save(update_fields=["status", "updated_at"])
            return Response(self.get_serializer(order).data)


class InventoryUpdateView(APIView):
    def put(self, request, sku):
        if not isinstance(request.data, dict):
            return error_response("VALIDATION_ERROR", "Request body must be a JSON object.", 400)
        qty = request.data.get("quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            return error_response("VALIDATION_ERROR", "Quantity must be a non-negative integer.", 400)
        try:
            product = Product.objects.get(sku=sku)
        except Product.DoesNotExist:
            return error_response("NOT_FOUND", "Product not found.", 404)
        Product.objects.filter(pk=product.pk).update(stock=qty)
        return Response({"sku": product.sku, "quantity": qty})


class ForecastView(APIView):
    def get(self, request, sku):
        try:
            rows = load_forecast()
        except (FileNotFoundError, ValueError) as exc:
            return error_response("SERVER_ERROR", "Forecast data is unavailable. Ensure ml/forecast.csv is generated.", 500)
        result = [row for row in rows if row["sku"] == sku]
        if not result:
            return error_response("NOT_FOUND", f"SKU {sku} is not a forecast SKU.", 404)
        return Response({"sku": sku, "forecast": [{"week_start": row["week_start"], "forecast_units": float(row["forecast_units"])} for row in result]})


class ReorderSuggestionView(APIView):
    def get(self, request):
        try:
            return Response({"results": build_reorder_suggestions()})
        except (FileNotFoundError, ValueError) as exc:
            return error_response("SERVER_ERROR", "Forecast or lead-time data is unavailable.", 500)
