# Design Decisions – VIKMO DealerDesk

## 1. Backend Architecture

Used Django REST Framework to implement REST APIs for products, dealers, inventory, orders, and forecasting. PostgreSQL is used for persistent storage.

## 2. Database Design

Used separate models for products, dealers, orders, and order items. SKU and dealer code provide unique identifiers. Foreign keys maintain relationships between orders, dealers, and products.

## 3. Order Management

New orders are created as DRAFT orders. Order totals are calculated using Decimal arithmetic to avoid floating-point errors. Input validation prevents invalid quantities, unknown products, duplicate SKUs, and invalid dealers.

## 4. AI Dealer Assistant

Used an LLM with tool calling to search product information, check stock, and create draft orders. Product prices and stock levels are retrieved from the database rather than invented by the model.

## 5. Forecasting Approach

Compared simple forecasting baselines using historical observations. Forecasts are generated for four future weeks. The reorder calculation uses lead-time demand, sample standard deviation, and safety stock.

## 6. Reorder Policy

Safety stock is calculated as ceil(1.65 × standard deviation × sqrt(lead time)). Reorder quantity is the non-negative difference between the reorder point and current stock.

## 7. Data Seeding

Used an idempotent seed command with update-or-create operations so repeated seeding does not duplicate products or dealers.

## 8. Testing

Validated the core APIs, order creation, assistant response structure, forecast CSV format, and reorder endpoint using automated tests and the provided smoke test.

## 9. Limitations

Forecast accuracy depends on the available historical data. LLM responses depend on provider availability and API configuration. Further evaluation with actual future sales and broader edge-case testing would improve reliability.
