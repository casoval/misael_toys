from django.urls import path
from . import views

app_name = "catalogo"

urlpatterns = [
    path("", views.home, name="home"),
    path("categoria/<slug:slug>/", views.categoria_detalle, name="categoria_detalle"),
    path("producto/<slug:slug>/", views.producto_detalle, name="producto_detalle"),
    path("producto/<slug:slug>/like/", views.toggle_like, name="toggle_like"),
]
