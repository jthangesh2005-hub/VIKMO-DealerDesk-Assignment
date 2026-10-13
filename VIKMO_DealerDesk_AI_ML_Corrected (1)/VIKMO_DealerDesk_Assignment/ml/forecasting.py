"""Leakage-aware weekly demand forecasting and contract reorder calculations.

Only standard-library modules are used, so the run contract remains lightweight.
Forecast model: choose among last observation, trailing four-week mean, and same-week-
last-year using SKU-level rolling-origin validation on prior observations only. Apply
shrunk promotion and pre-Diwali multipliers learned only from historical rows.
"""
from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ML_DIR = ROOT / "ml"
FORECAST_PATH = ML_DIR / "forecast.csv"
FORECAST_WEEKS = [date.fromisoformat(d) for d in ("2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19")]
FORECAST_COLUMNS = ["sku", "week_start", "forecast_units"]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Required data file not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_inputs():
    lead_rows = _read_csv(DATA_DIR / "lead_times.csv")
    sales_rows = _read_csv(DATA_DIR / "sales_history.csv")
    promo_rows = _read_csv(DATA_DIR / "promo_calendar.csv")
    festival_rows = _read_csv(DATA_DIR / "festival_calendar.csv")
    lead_times = {row["sku"]: int(row["lead_time_weeks"]) for row in lead_rows}
    history: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)
    for row in sales_rows:
        week = date.fromisoformat(row["week_start"])
        # Target-week and later actuals are never used to forecast the specified horizon.
        history[row["sku"]][week] = {"units": max(0, float(row["units_sold"])), "promo": int(row.get("promo_flag", 0) or 0)}
    promos = {(row["sku"], date.fromisoformat(row["week_start"])): int(row.get("promo_flag", 0) or 0) for row in promo_rows}
    festivals = [date.fromisoformat(row["date"]) for row in festival_rows if row.get("festival", "").strip().lower() == "diwali"]
    return lead_times, history, promos, festivals


def _baseline(name: str, previous: dict[date, dict[str, Any]], target_week: date) -> float | None:
    dates = sorted(d for d in previous if d < target_week)
    if not dates:
        return None
    if name == "last_value":
        return float(previous[dates[-1]]["units"])
    if name == "recent_average":
        window = dates[-4:]
        return sum(previous[d]["units"] for d in window) / len(window)
    if name == "same_week_last_year":
        return float(previous[target_week - timedelta(days=364)]["units"]) if target_week - timedelta(days=364) in previous else None
    return None


def _best_baseline(sku_history: dict[date, dict[str, Any]]) -> str:
    dates = sorted(sku_history)
    # Walk-forward validation over up to the last 12 observed weeks. Each prediction sees
    # only observations strictly earlier than its validation week.
    validation_dates = dates[-12:]
    scores = {"last_value": [], "recent_average": [], "same_week_last_year": []}
    for target in validation_dates:
        previous = {d: v for d, v in sku_history.items() if d < target}
        actual = sku_history[target]["units"]
        for model in scores:
            pred = _baseline(model, previous, target)
            if pred is not None:
                scores[model].append(abs(actual - pred))
    valid_scores = {model: sum(errs) / len(errs) for model, errs in scores.items() if len(errs) >= (4 if model == "same_week_last_year" else 1)}
    if valid_scores:
        return min(valid_scores, key=valid_scores.get)
    return "recent_average" if dates else "last_value"


def _promotion_multiplier(history: dict[str, dict[date, dict[str, Any]]], sku: str) -> float:
    all_promo, all_regular = [], []
    sku_promo, sku_regular = [], []
    for candidate, rows in history.items():
        for values in rows.values():
            target = all_promo if values["promo"] else all_regular
            target.append(values["units"])
            if candidate == sku:
                (sku_promo if values["promo"] else sku_regular).append(values["units"])
    global_ratio = (sum(all_promo) / len(all_promo)) / max(0.1, sum(all_regular) / max(1, len(all_regular))) if all_promo and all_regular else 1.15
    if len(sku_promo) >= 2 and sku_regular:
        sku_ratio = (sum(sku_promo) / len(sku_promo)) / max(0.1, sum(sku_regular) / len(sku_regular))
        n = len(sku_promo)
        ratio = (n / (n + 4)) * sku_ratio + (4 / (n + 4)) * global_ratio
    else:
        ratio = global_ratio
    return min(2.0, max(1.0, ratio))


def _festival_multiplier(history: dict[str, dict[date, dict[str, Any]]], sku: str, festivals: list[date]) -> float:
    # Learn a pre-Diwali lift from historical data; a week is in the window if it is in
    # the 42 days immediately preceding Diwali. Only observed rows are considered.
    festive_all, regular_all, festive_sku, regular_sku = [], [], [], []
    for candidate, rows in history.items():
        for week, values in rows.items():
            in_window = any(0 <= (festival - week).days <= 42 for festival in festivals if festival < date(2026, 11, 1))
            if in_window:
                festive_all.append(values["units"])
                if candidate == sku:
                    festive_sku.append(values["units"])
            else:
                regular_all.append(values["units"])
                if candidate == sku:
                    regular_sku.append(values["units"])
    global_ratio = (sum(festive_all) / len(festive_all)) / max(0.1, sum(regular_all) / max(1, len(regular_all))) if festive_all and regular_all else 1.0
    if len(festive_sku) >= 4 and regular_sku:
        sku_ratio = (sum(festive_sku) / len(festive_sku)) / max(0.1, sum(regular_sku) / len(regular_sku))
        n = len(festive_sku)
        ratio = (n / (n + 8)) * sku_ratio + (8 / (n + 8)) * global_ratio
    else:
        ratio = global_ratio
    return min(1.8, max(0.8, ratio))


def _forecast_sku_week(sku: str, week: date, history: dict[str, dict[date, dict[str, Any]]], promos, festivals, chosen_model: str) -> float:
    sku_history = {d: row for d, row in history.get(sku, {}).items() if d < FORECAST_WEEKS[0]}
    prediction = _baseline(chosen_model, sku_history, week)
    if prediction is None:
        prediction = _baseline("recent_average", sku_history, week)
    if prediction is None:
        # Defensive new/rare SKU fallback: median/mean of all latest SKU sales, if available.
        available = [row["units"] for rows in history.values() for d, row in rows.items() if d < FORECAST_WEEKS[0]]
        prediction = statistics.mean(available) if available else 0.0
    if promos.get((sku, week), 0) == 1:
        prediction *= _promotion_multiplier(history, sku)
    if any(0 <= (festival - week).days <= 42 for festival in festivals):
        prediction *= _festival_multiplier(history, sku, festivals)
    return round(max(0.0, prediction), 1)


def generate_forecast_csv() -> Path:
    lead_times, history, promos, festivals = read_inputs()
    # Enforce historical cutoff at the first target week; the data supplied has history
    # through 2026-09-21, but this remains safe if later rows are present in local data.
    history = {sku: {d: row for d, row in rows.items() if d < FORECAST_WEEKS[0]} for sku, rows in history.items()}
    models = {sku: _best_baseline(rows) for sku, rows in history.items()}
    ML_DIR.mkdir(parents=True, exist_ok=True)
    with FORECAST_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FORECAST_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for sku in sorted(lead_times):
            model = models.get(sku, "recent_average")
            for week in FORECAST_WEEKS:
                forecast = _forecast_sku_week(sku, week, history, promos, festivals, model)
                writer.writerow({"sku": sku, "week_start": week.isoformat(), "forecast_units": f"{forecast:.1f}"})
    return FORECAST_PATH


def load_forecast() -> list[dict[str, str]]:
    if not FORECAST_PATH.exists():
        generate_forecast_csv()
    with FORECAST_PATH.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FORECAST_COLUMNS:
            raise ValueError(f"Forecast CSV columns must be exactly {FORECAST_COLUMNS}")
        rows = list(reader)
    lead_times, _, _, _ = read_inputs()
    expected = {(sku, week.isoformat()) for sku in lead_times for week in FORECAST_WEEKS}
    actual = {(row["sku"], row["week_start"]) for row in rows}
    if len(rows) != 160 or actual != expected:
        raise ValueError(f"Forecast must contain exactly 160 SKU/week rows; found {len(rows)}")
    if any(float(row["forecast_units"]) < 0 for row in rows):
        raise ValueError("Forecast values must be non-negative")
    return rows


def build_reorder_suggestions() -> list[dict[str, Any]]:
    from core.models import Product

    forecast_rows = load_forecast()
    lead_times, history, _, _ = read_inputs()
    forecast_by_sku: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in forecast_rows:
        forecast_by_sku[row["sku"]].append(row)
    stock_by_sku = dict(Product.objects.filter(sku__in=lead_times).values_list("sku", "stock"))
    suggestions = []
    for sku in sorted(lead_times):
        if sku not in stock_by_sku:
            # Catalogue omissions should not silently reduce the required 40 result count;
            # report zero live stock for an unknown forecast SKU instead.
            current_stock = 0
        else:
            current_stock = int(stock_by_sku[sku])
        lead_time = max(1, int(lead_times[sku]))
        future = sorted(forecast_by_sku[sku], key=lambda x: x["week_start"])
        lead_time_demand = sum(float(row["forecast_units"]) for row in future[:lead_time])
        observations = [entry["units"] for _, entry in sorted(history.get(sku, {}).items())][-12:]
        sigma = statistics.stdev(observations) if len(observations) >= 2 else 0.0
        safety_stock = math.ceil(1.65 * sigma * math.sqrt(lead_time))
        reorder_point = lead_time_demand + safety_stock
        reorder_qty = max(0, math.ceil(reorder_point - current_stock))
        suggestions.append({
            "sku": sku,
            "current_stock": current_stock,
            "lead_time_weeks": lead_time,
            "lead_time_demand": round(lead_time_demand, 1),
            "safety_stock": safety_stock,
            "reorder_point": round(reorder_point, 1),
            "reorder_qty": reorder_qty,
        })
    return suggestions


def walk_forward_baseline_metrics() -> dict[str, float]:
    """Diagnostic only: compare three baselines over historical one-step forecasts."""
    _, history, _, _ = read_inputs()
    errors = {name: [] for name in ("last_value", "recent_average", "same_week_last_year")}
    for sku, series in history.items():
        dates = sorted(series)
        for target in dates[-12:]:
            previous = {d: row for d, row in series.items() if d < target}
            actual = series[target]["units"]
            for model in errors:
                pred = _baseline(model, previous, target)
                if pred is not None:
                    errors[model].append(abs(actual - pred))
    return {name: round(sum(values) / len(values), 3) if values else float("nan") for name, values in errors.items()}
