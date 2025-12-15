import datetime
import json

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.template.loader import render_to_string

from carts.models import CartItem
from store.models import Product

from .forms import OrderForm
from .models import Order, OrderProduct, Payment



def place_order(request, total=0, quantity=0):
    current_user = request.user

    cart_items = CartItem.objects.filter(user=current_user)
    if cart_items.count() <= 0:
        return redirect("store")

    # Calculate totals
    for item in cart_items:
        total += item.product.price * item.quantity
        quantity += item.quantity
    tax = (2 * total) / 100
    grand_total = total + tax
    amount_paisa = int(round(grand_total * 100))

    if request.method == "POST":
        form = OrderForm(request.POST)
        if form.is_valid():
            data = Order()
            data.user = current_user
            data.first_name = form.cleaned_data["first_name"]
            data.last_name = form.cleaned_data["last_name"]
            data.phone = form.cleaned_data["phone"]
            data.email = form.cleaned_data["email"]
            data.address_line_1 = form.cleaned_data["address_line_1"]
            data.address_line_2 = form.cleaned_data["address_line_2"]
            data.country = form.cleaned_data["country"]
            data.state = form.cleaned_data["state"]
            data.city = form.cleaned_data["city"]
            data.order_note = form.cleaned_data["order_note"]
            data.order_total = grand_total
            data.tax = tax
            data.ip = request.META.get("REMOTE_ADDR")
            data.save()

            # Generate unique order number
            yr = int(datetime.date.today().strftime("%Y"))
            dt = int(datetime.date.today().strftime("%d"))
            mt = int(datetime.date.today().strftime("%m"))
            current_date = datetime.date(yr, mt, dt).strftime("%Y%m%d")
            order_number = current_date + str(data.id)
            data.order_number = order_number
            data.save()

            order = Order.objects.get(
                user=current_user, is_ordered=False, order_number=order_number
            )

            # Persist current order in session for payment flow
            request.session["order_number"] = order.order_number

            context = {
                "order": order,
                "cart_items": cart_items,
                "total": total,
                "tax": tax,
                "grand_total": grand_total,
            }
            return render(request, "orders/payments.html", context)
    return redirect("checkout")



def order_complete(request):
    order_number = request.GET.get("order_number")
    transID = request.GET.get("payment_id")

    try:
        order = Order.objects.get(order_number=order_number, is_ordered=True)
        ordered_products = OrderProduct.objects.filter(order_id=order.id)

        subtotal = sum(i.product_price * i.quantity for i in ordered_products)
        payment = Payment.objects.get(payment_id=transID)

        context = {
            "order": order,
            "ordered_products": ordered_products,
            "order_number": order.order_number,
            "transID": payment.payment_id,
            "payment": payment,
            "subtotal": subtotal,
        }
        return render(request, "orders/order_complete.html", context)
    except (Payment.DoesNotExist, Order.DoesNotExist):
        return redirect("home")



def khalti_initiate(request):
    if not request.user.is_authenticated:
        return redirect("login")

    # Prefer the order set in session by place_order
    session_order_number = request.session.get("order_number")
    try:
        if session_order_number:
            order = Order.objects.get(user=request.user, is_ordered=False, order_number=session_order_number)
        else:
            order = Order.objects.filter(user=request.user, is_ordered=False).latest("id")
    except Order.DoesNotExist:
        messages.error(request, "No pending order found. Please create the order again.")
        return redirect("checkout")

    amount_paisa = int(round(order.order_total * 100))
    if amount_paisa < 100:
        return redirect("checkout")

    return_url = request.build_absolute_uri(reverse("khalti_lookup"))
    website_url = request.build_absolute_uri("/")

    customer_name = (f"{order.first_name} {order.last_name}").strip()
    payload = {
        "return_url": return_url,
        "website_url": website_url,
        "amount": amount_paisa,
        "purchase_order_id": order.order_number,
        "purchase_order_name": f"Order {order.order_number}",
        "customer_info": {
            "name": customer_name,
            "email": order.email,
            "phone": order.phone,
        },
    }

    headers = {"Authorization": f"Key {settings.KHALTI_SECRET_KEY}"}
    try:
        resp = requests.post(
            settings.KHALTI_PAYMENT_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
        # Try to parse JSON; if fails, keep raw text
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
    except Exception as e:
        messages.error(request, f"Could not contact payment gateway: {e}")
        return _render_payments_page(request, order)

    if resp.status_code == 200 and isinstance(data, dict) and data.get("payment_url") and data.get("pidx"):
        # Redirect user to Khalti hosted payment page
        return redirect(data["payment_url"])

    # If initiate fails, show detailed error
    if isinstance(data, dict):
        err_msg = data.get("detail") or data.get("message") or data.get("error") or data.get("raw") or "Payment initiation failed."
    else:
        err_msg = f"Payment initiation failed (HTTP {resp.status_code})."
    messages.error(request, f"Khalti initiate error (HTTP {resp.status_code}): {err_msg}")
    return _render_payments_page(request, order)


def khalti_lookup(request):
    pidx = request.GET.get("pidx")
    if not pidx:
        return redirect("checkout")

    headers = {"Authorization": f"Key {settings.KHALTI_SECRET_KEY}"}
    payload = {"pidx": pidx}
    try:
        resp = requests.post(settings.KHALTI_VERIFICATION_URL, json=payload, headers=headers, timeout=20)
        data = resp.json()
    except Exception:
        return redirect("checkout")

    # Expecting fields: status, purchase_order_id, total_amount, etc.
    if resp.status_code == 200 and data.get("status") in ("Completed", "completed", "Success", "success"):
        order_id = data.get("purchase_order_id")
        total_amount = data.get("total_amount", 0)
        try:
            order = Order.objects.get(order_number=order_id, is_ordered=False)
        except Order.DoesNotExist:
            return redirect("home")

        # Create payment and complete order
        payment = Payment.objects.create(
            user=request.user,
            payment_id=pidx,
            payment_method="Khalti",
            amount_paid=(total_amount or 0) / 100.0,
            status="Completed",
        )
        payment.save()

        order.payment = payment
        order.is_ordered = True
        order.save()

        # Move cart items to OrderProduct and reduce stock
        cart_items = CartItem.objects.filter(user=request.user)
        for item in cart_items:
            orderproduct = OrderProduct.objects.create(
                order_id=order.id,
                payment=payment,
                user_id=request.user.id,
                product_id=item.product_id,
                quantity=item.quantity,
                product_price=item.product.price,
                ordered=True,
            )
            orderproduct.variations.set(item.variations.all())
            orderproduct.save()

            product = Product.objects.get(id=item.product_id)
            product.stock -= item.quantity
            product.save()

        CartItem.objects.filter(user=request.user).delete()

        # Send order email
        mail_subject = "Thank you for your order!"
        message = render_to_string(
            "orders/order_recieved_email.html",
            {"user": request.user, "order": order},
        )
        EmailMessage(mail_subject, message, to=[request.user.email]).send()

        # Show success message and redirect to landing page
        messages.success(
            request,
            "Payment successful! You will receive your order in 2–3 days. "
            "Thank you for shopping with us."
        )
        return redirect("home")

    # Failed lookup or cancelled
    messages.error(request, "Payment not completed. You can try again.")
    # Re-render payments so user can retry
    try:
        order_id = data.get("purchase_order_id")
        order = Order.objects.get(order_number=order_id, is_ordered=False)
    except Exception:
        return redirect("checkout")
    return _render_payments_page(request, order)


# Helper to render the payments page with current cart totals
def _render_payments_page(request, order):
    cart_items = CartItem.objects.filter(user=request.user)
    total = 0
    quantity = 0
    for item in cart_items:
        total += item.product.price * item.quantity
        quantity += item.quantity
    tax = (2 * total) / 100
    grand_total = total + tax
    context = {
        "order": order,
        "cart_items": cart_items,
        "total": total,
        "tax": tax,
        "grand_total": grand_total,
    }
    return render(request, "orders/payments.html", context)
