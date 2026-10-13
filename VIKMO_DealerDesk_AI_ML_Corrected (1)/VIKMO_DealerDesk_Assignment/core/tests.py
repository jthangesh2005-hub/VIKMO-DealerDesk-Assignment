from decimal import Decimal
from pathlib import Path
import csv

from django.test import TestCase
from rest_framework.test import APITestCase

from .models import Product, Dealer, Order
from ml.forecasting import load_forecast, FORECAST_WEEKS, FORECAST_PATH


class CoreApiTests(APITestCase):
    def setUp(self):
        self.dealer = Dealer.objects.create(dealer_code="TEST-001", name="Test Dealer", credit_limit=Decimal("10000.00"))
        self.product = Product.objects.create(
            sku="TST-1001", name="Test Brake Pad", category="Brakes", brand="TestBrand",
            vehicle_fitment="Test Bike 150", price=Decimal("100.05"), stock=20, description="Test fixture",
        )

    def test_product_pagination_shape(self):
        response = self.client.get("/api/products/?page_size=5")
        self.assertEqual(response.status_code, 200)
        self.assertTrue({"count", "next", "previous", "results"}.issubset(response.data))

    def test_order_creates_correct_decimal_totals(self):
        response = self.client.post("/api/orders/", {
            "dealer_id": self.dealer.pk, "items": [{"sku": self.product.sku, "quantity": 3}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        body = response.data
        self.assertEqual(body["status"], "DRAFT")
        self.assertEqual(body["dealer_id"], self.dealer.pk)
        self.assertEqual(body["subtotal"], "300.15")
        self.assertEqual(body["tax_total"], "54.03")
        self.assertEqual(body["grand_total"], "354.18")
        self.assertEqual(len(body["items"]), 1)

    def test_empty_order_items_returns_contract_error(self):
        response = self.client.post("/api/orders/", {"dealer_id": self.dealer.pk, "items": []}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "VALIDATION_ERROR")
        self.assertIsInstance(response.data["error"]["details"], list)

    def test_order_quantity_rejects_boolean_and_float(self):
        for quantity in (True, 1.0, 1.5):
            with self.subTest(quantity=quantity):
                response = self.client.post("/api/orders/", {
                    "dealer_id": self.dealer.pk,
                    "items": [{"sku": self.product.sku, "quantity": quantity}],
                }, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["error"]["code"], "VALIDATION_ERROR")

    def test_reading_draft_order_does_not_change_updated_at(self):
        created = self.client.post("/api/orders/", {
            "dealer_id": self.dealer.pk, "items": [{"sku": self.product.sku, "quantity": 1}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        order = Order.objects.get(order_number=created.data["order_number"])
        original_updated_at = order.updated_at
        detail = self.client.get(f"/api/orders/{order.order_number}/")
        self.assertEqual(detail.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.updated_at, original_updated_at)

    def test_inventory_update_reflects_in_product(self):
        response = self.client.put(f"/api/inventory/{self.product.sku}/", {"quantity": 50, "reason": "test"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/products/{self.product.sku}/").data["stock"], 50)

    def test_order_idempotency(self):
        body = {"dealer_id": self.dealer.pk, "items": [{"sku": self.product.sku, "quantity": 1}]}
        one = self.client.post("/api/orders/", body, format="json", HTTP_IDEMPOTENCY_KEY="test-idem-1")
        two = self.client.post("/api/orders/", body, format="json", HTTP_IDEMPOTENCY_KEY="test-idem-1")
        self.assertEqual(one.status_code, 201)
        self.assertEqual(two.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    def test_confirm_cancel_stock_is_restored(self):
        order = self.client.post("/api/orders/", {"dealer_id": self.dealer.pk, "items": [{"sku": self.product.sku, "quantity": 3}]}, format="json").data
        confirm = self.client.post(f"/api/orders/{order['order_number']}/confirm/")
        self.assertEqual(confirm.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 17)
        cancel = self.client.post(f"/api/orders/{order['order_number']}/cancel/")
        self.assertEqual(cancel.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 20)


class ForecastArtifactTests(TestCase):
    def test_forecast_file_has_required_shape_and_horizon(self):
        rows = load_forecast()
        self.assertEqual(len(rows), 160)
        self.assertEqual(set(r["week_start"] for r in rows), {d.isoformat() for d in FORECAST_WEEKS})
        self.assertEqual(len({r["sku"] for r in rows}), 40)
        self.assertTrue(all(float(r["forecast_units"]) >= 0 for r in rows))
