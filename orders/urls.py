from django.urls import path

from . import views

urlpatterns = [
    path("place_order/", views.place_order, name="place_order"),
    path("order_complete/", views.order_complete, name="order_complete"),
    # ePayment flow
    path("khalti/initiate/", views.khalti_initiate, name="khalti_initiate"),
    path("khalti/lookup/", views.khalti_lookup, name="khalti_lookup"),
]
