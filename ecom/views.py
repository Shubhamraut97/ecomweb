from django.shortcuts import render
from category.models import Category
from store.models import Product


def home(request):
    products = Product.objects.all().filter(is_available=True).order_by('-created_date')[:8]  # Latest 8 products
    categories = Category.objects.all()[:6]  # Show 6 categories
    context = {
        'products': products,
        'categories': categories,
    }
    return render(request, "home.html", context)
# Create your views here.