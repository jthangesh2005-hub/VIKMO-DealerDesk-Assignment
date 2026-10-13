from django.urls import include, path
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")
router.register("dealers", views.DealerViewSet, basename="dealer")
router.register("orders", views.OrderViewSet, basename="order")

urlpatterns = [
    path("health/", views.health, name="health"),
    path("inventory/<str:sku>/", views.InventoryUpdateView.as_view(), name="inventory-update"),
    path("forecast/<str:sku>/", views.ForecastView.as_view(), name="forecast-detail"),
    path("reorder-suggestions/", views.ReorderSuggestionView.as_view(), name="reorder-suggestions"),
    path("", include(router.urls)),
]
