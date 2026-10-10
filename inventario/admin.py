from django.contrib import admin

from .models import (
    Cotizacion, CotizacionItem, MovimientoStock, ReglaDescuento, Stock, Ubicacion, Venta, VentaItem,
)


@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    list_display = ("nombre", "orden", "activa")
    list_editable = ("orden", "activa")


@admin.register(ReglaDescuento)
class ReglaDescuentoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "minimo_unidades", "porcentaje", "alcance", "acumulable", "vigente_desde", "vigente_hasta", "activa")
    list_editable = ("activa",)
    list_filter = ("activa", "alcance")
    fieldsets = (
        (None, {"fields": ("nombre", "minimo_unidades", "porcentaje", "activa")}),
        ("¿A qué se aplica?", {
            "fields": ("alcance", "categoria", "producto", "acumulable"),
            "description": "Por defecto aplica a todo el pedido. Si eliges 'Solo una categoría' o 'Solo un producto', "
                            "completa el campo correspondiente. Las reglas NO acumulables no se suman entre sí: "
                            "se aplica solo la que más descuenta (ej. 5+ → 5% y 10+ → 10%).",
        }),
        ("Promoción por fechas (opcional)", {"fields": ("vigente_desde", "vigente_hasta")}),
    )


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    # Solo lectura: el stock se mueve desde el panel para que quede historial.
    list_display = ("producto", "ubicacion", "cantidad")
    list_filter = ("ubicacion",)
    search_fields = ("producto__nombre",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MovimientoStock)
class MovimientoStockAdmin(admin.ModelAdmin):
    list_display = ("creado", "producto", "ubicacion", "cantidad", "tipo", "usuario", "nota")
    list_filter = ("tipo", "ubicacion")
    search_fields = ("producto__nombre", "nota")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class VentaItemInline(admin.TabularInline):
    model = VentaItem
    extra = 0
    can_delete = False
    readonly_fields = ("producto", "ubicacion", "cantidad", "precio_lista", "precio_unitario")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    list_display = ("id", "creado", "vendedor", "cliente", "total", "anulada")
    list_filter = ("anulada", "vendedor")
    inlines = [VentaItemInline]
    readonly_fields = [f.name for f in Venta._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CotizacionItemInline(admin.TabularInline):
    model = CotizacionItem
    extra = 0
    can_delete = False
    readonly_fields = ("producto", "cantidad", "precio_unitario")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Cotizacion)
class CotizacionAdmin(admin.ModelAdmin):
    list_display = ("id", "creado", "creador", "cliente", "total")
    inlines = [CotizacionItemInline]
    readonly_fields = [f.name for f in Cotizacion._meta.fields]

    def has_add_permission(self, request):
        return False
