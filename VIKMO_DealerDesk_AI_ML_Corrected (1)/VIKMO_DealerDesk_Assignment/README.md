# VIKMO DealerDesk — AI / ML track

This repository implements the required **A1 backend core**, **C dealer assistant**, and **D forecasting & reorder** parts. Optional supplier-sync and frontend work are not claimed as completed.

## Stack

- Python 3.11+
- Django 5.2 and Django REST Framework
- PostgreSQL (SQLite is intentionally rejected, per assignment)
- Groq tool calling, default model `openai/gpt-oss-20b`
- Standard-library, walk-forward-selected demand baselines plus learned promotion and pre-Diwali adjustments

The selected Groq model supports tool use according to the current official model/tool-use documentation: [Groq models](https://console.groq.com/docs/models) and [tool use](https://console.groq.com/docs/tool-use/overview). The grader supplies its own provider key.

## Setup

1. Create a PostgreSQL database named `dealerdesk`.
2. Copy `.env.example` to `.env` and set `DATABASE_URL` to your PostgreSQL connection string. Set `GROQ_API_KEY` to enable live assistant responses. Never commit `.env`.
3. Install and seed:

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py seed_data
python manage.py seed_data
python manage.py check
python manage.py runserver 0.0.0.0:8000 --noreload
```

In another terminal run the required AI/ML smoke test:

```bash
python -m pip install requests
python smoke_test.py --base-url http://localhost:8000 --track aiml
```

The app requires PostgreSQL even for management commands. Original files in `data/` are included unchanged; `seed_data` upserts products and dealers and is safe to run twice.

## API overview

- `GET /api/health/`
- `GET/POST /api/products/`, `GET/PATCH /api/products/{sku}/`
- `PUT /api/inventory/{sku}/`
- `GET/POST /api/dealers/`, `GET /api/dealers/{id}/`
- `GET/POST /api/orders/`, `GET/PUT /api/orders/{order_number}/`, and status actions
- `POST /api/assistant/chat/`
- `GET /api/forecast/{sku}/`
- `GET /api/reorder-suggestions/`

List endpoints use contract pagination. Error responses use the contract's `error.code`, `error.message`, and `error.details` structure.

## Forecasting approach

`ml/forecasting.py` uses historical sales strictly earlier than the first forecast week. It compares one-step, walk-forward predictions for last observation, trailing four-week average, and same week last year. The lowest per-SKU rolling validation MAE selects the baseline. Known future promotions are adjusted using shrunk historical promotional lift. Forecasts within six weeks of Diwali use a bounded, shrunk pre-Diwali multiplier learned from historical Diwali windows. Sparse SKUs fall back to the trailing average. The committed `ml/forecast.csv` has 40 SKUs × 4 weeks and is consumed by both forecast endpoints.

Reorder quantities use the exact contract formula: lead-time demand is the sum of forecast units over lead time; safety stock is `ceil(1.65 × sample_std(last 12 weeks) × sqrt(lead_time_weeks))`; reorder point is demand plus safety stock; reorder quantity is the positive ceiling above live stock.

## Tests

```bash
python manage.py test core
python smoke_test.py --base-url http://localhost:8000 --track aiml
```

The smoke test exercises the contract basics. It is not a substitute for testing concurrency, provider behaviour, or forecast quality on held-out future sales.

## Video and repository

- Track: AI / ML intern
- Optional parts attempted: order lifecycle/idempotency are also implemented; supplier sync and frontend are not claimed.
- Five-minute unlisted walkthrough: (https://drive.google.com/file/d/1wzvVMoEf4UwLgidVjJLKa40Jz44iu6rj/view?usp=drive_link)

