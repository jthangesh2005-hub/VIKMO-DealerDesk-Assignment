from decimal import Decimal
from rest_framework import serializers
from .models import Product, Dealer, Order, OrderItem
from .services import create_draft_order


class ProductSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = Product
        fields = ["sku", "name", "category", "brand", "vehicle_fitment", "price", "stock", "description"]
        read_only_fields = ["stock"]

    def validate_sku(self, value):
        if self.instance and value != self.instance.sku:
            raise serializers.ValidationError("SKU cannot be changed.")
        if self.instance is None and Product.objects.filter(sku=value).exists():
            # The view translates this known conflict into HTTP 409 DUPLICATE.
            self.context["duplicate_sku"] = value
        return value


class DealerSerializer(serializers.ModelSerializer):
    credit_limit = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False, default=Decimal("0.00"))

    class Meta:
        model = Dealer
        fields = ["id", "dealer_code", "name", "city", "phone", "email", "gstin", "credit_limit"]
        read_only_fields = ["id"]

    def validate_dealer_code(self, value):
        qs = Dealer.objects.filter(dealer_code=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            self.context["duplicate_dealer_code"] = value
        return value


class StrictPositiveIntegerField(serializers.IntegerField):
    """Accept integer JSON values (and digit strings), but not booleans or floats."""
    default_error_messages = {
        **serializers.IntegerField.default_error_messages,
        "invalid": "A whole-number integer is required.",
    }

    def to_internal_value(self, data):
        if isinstance(data, bool) or isinstance(data, float):
            self.fail("invalid")
        return super().to_internal_value(data)


class OrderItemInputSerializer(serializers.Serializer):
    sku = serializers.CharField(max_length=50, allow_blank=False, trim_whitespace=True)
    quantity = StrictPositiveIntegerField(min_value=1)


class OrderItemReadSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="product.sku", read_only=True)
    name = serializers.CharField(read_only=True)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    line_subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    line_tax = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ["sku", "name", "quantity", "unit_price", "line_subtotal", "line_tax", "line_total"]


class OrderReadSerializer(serializers.ModelSerializer):
    dealer_id = serializers.IntegerField(read_only=True)
    items = OrderItemReadSerializer(many=True, read_only=True)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    tax_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    grand_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Order
        fields = ["order_number", "dealer_id", "status", "items", "subtotal", "tax_total", "grand_total", "created_at", "updated_at"]


class OrderCreateSerializer(serializers.Serializer):
    dealer_id = StrictPositiveIntegerField(min_value=1)
    items = OrderItemInputSerializer(many=True, allow_empty=False)

    def validate_dealer_id(self, value):
        if not Dealer.objects.filter(pk=value).exists():
            raise serializers.ValidationError("Dealer does not exist.")
        return value

    def validate_items(self, items):
        seen = set()
        for item in items:
            sku = item["sku"]
            if sku in seen:
                raise serializers.ValidationError(f"Duplicate SKU {sku} in order.")
            seen.add(sku)
            if not Product.objects.filter(sku=sku).exists():
                raise serializers.ValidationError(f"SKU {sku} does not exist.")
        return items

    def create(self, validated_data):
        return create_draft_order(validated_data["dealer_id"], validated_data["items"])


class ReplaceOrderItemsSerializer(serializers.Serializer):
    items = OrderItemInputSerializer(many=True, allow_empty=False)

    def validate_items(self, items):
        seen = set()
        for item in items:
            sku = item["sku"]
            if sku in seen:
                raise serializers.ValidationError(f"Duplicate SKU {sku} in order.")
            seen.add(sku)
            if not Product.objects.filter(sku=sku).exists():
                raise serializers.ValidationError(f"SKU {sku} does not exist.")
        return items
