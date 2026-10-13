import csv
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Product, Dealer


class Command(BaseCommand):
    help = "Load the CSV fixtures from the data/ folder (idempotent)."

    def handle(self, *args, **options):
        data_dir = Path(settings.DATA_DIR)
        self.stdout.write(self.style.NOTICE(f"Seeding from {data_dir}"))

        with transaction.atomic():
            self._load_products(data_dir / "catalogue.csv")
            self._load_dealers(data_dir / "dealers.csv")
        self.stdout.write(self.style.SUCCESS("Seeding completed."))

    def _load_products(self, csv_path: Path):
        with open(csv_path, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                Product.objects.update_or_create(
                    sku=row["sku"],
                    defaults={
                        "name": row["name"],
                        "category": row["category"],
                        "brand": row["brand"],
                        "vehicle_fitment": row["vehicle_fitment"],
                        "price": row["price"],
                        "stock": int(row["stock"]),
                        "description": row.get("description", ""),
                    },
                )

    def _load_dealers(self, csv_path: Path):
        with open(csv_path, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                Dealer.objects.update_or_create(
                    dealer_code=row["dealer_code"],
                    defaults={
                        "name": row["name"],
                        "city": row.get("city", ""),
                        "phone": row.get("phone", ""),
                        "email": row.get("email", ""),
                        "gstin": row.get("gstin", ""),
                        "credit_limit": row.get("credit_limit", "0"),
                    },
                )
