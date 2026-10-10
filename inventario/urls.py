from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("login/", views.LoginPanel.as_view(), name="login"),
    path("salir/", LogoutView.as_view(), name="logout"),

    path("", views.stock_lista, name="stock"),
    path("producto/<int:pk>/", views.stock_producto, name="stock_producto"),
    path("producto/<int:pk>/ingreso/", views.stock_ingreso, name="stock_ingreso"),
    path("producto/<int:pk>/traslado/", views.stock_traslado, name="stock_traslado"),
    path("producto/<int:pk>/ajuste/", views.stock_ajuste, name="stock_ajuste"),

    path("calcular/", views.calcular, name="calcular"),

    path("vender/", views.venta_nueva, name="venta_nueva"),
    path("ventas/", views.ventas_lista, name="ventas"),
    path("ventas/<int:pk>/", views.venta_detalle, name="venta_detalle"),
    path("ventas/<int:pk>/anular/", views.venta_anular, name="venta_anular"),

    path("cotizar/", views.cotizacion_nueva, name="cotizacion_nueva"),
    path("cotizaciones/", views.cotizaciones_lista, name="cotizaciones"),
    path("cotizaciones/<int:pk>/", views.cotizacion_detalle, name="cotizacion_detalle"),
    path("cotizaciones/<int:pk>/pdf/", views.cotizacion_pdf, name="cotizacion_pdf"),

    path("usuarios/", views.usuarios, name="usuarios"),
    path("usuarios/<int:pk>/clave/", views.usuario_clave, name="usuario_clave"),
    path("usuarios/<int:pk>/activar/", views.usuario_activar, name="usuario_activar"),
]
