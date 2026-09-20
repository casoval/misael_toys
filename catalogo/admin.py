from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .models import Categoria, Atributo, ValorAtributo, Producto, ImagenProducto, ConfiguracionSitio, Filamento


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("color_preview", "nombre", "orden", "activa")
    list_editable = ("orden", "activa")
    prepopulated_fields = {"slug": ("nombre",)}
    search_fields = ("nombre",)

    @admin.display(description="Color")
    def color_preview(self, obj):
        return format_html(
            '<div style="width:22px;height:22px;border-radius:50%;background:{};"></div>',
            obj.color_fondo,
        )


class ValorAtributoInline(admin.TabularInline):
    model = ValorAtributo
    extra = 1
    fields = ("valor", "color_hex")


@admin.register(Atributo)
class AtributoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "orden", "mostrar_como_filtro")
    list_editable = ("orden", "mostrar_como_filtro")
    inlines = [ValorAtributoInline]


@admin.register(Filamento)
class FilamentoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "costo_por_kg", "activo")
    list_editable = ("costo_por_kg", "activo")


class ImagenProductoInline(admin.TabularInline):
    model = ImagenProducto
    extra = 1


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        "miniatura", "nombre", "categoria", "precio", "precio_sugerido_col",
        "peso_col", "horas_col", "total_likes", "disponible", "destacado",
        "recomendado_por_terapeutas", "orden",
    )
    ordering = ("-total_likes",)
    list_editable = ("precio", "disponible", "destacado", "recomendado_por_terapeutas", "orden")
    list_filter = ("categoria", "disponible", "destacado", "recomendado_por_terapeutas")
    search_fields = ("nombre", "descripcion")
    prepopulated_fields = {"slug": ("nombre",)}
    filter_horizontal = ("atributos",)
    inlines = [ImagenProductoInline]
    readonly_fields = ("precio_sugerido_detalle",)
    fieldsets = (
        (None, {
            "fields": ("nombre", "slug", "categoria", "precio")
        }),
        ("Descripción", {
            "fields": ("descripcion_corta", "descripcion")
        }),
        ("Filtros / atributos", {
            "fields": ("atributos",),
            "description": "Selecciona los valores que aplican a este producto (ej. Material: PLA, Color: Azul)."
        }),
        ("Cálculo de costos (uso interno, no se muestra al público)", {
            "fields": ("filamento", "peso_gramos", "horas_impresion", "precio_sugerido_detalle"),
            "description": "Completa estos datos y abajo verás un precio sugerido de referencia. "
                            "El precio real que ve el cliente sigue siendo el campo 'precio' de arriba.",
        }),
        ("Visibilidad", {
            "fields": ("disponible", "destacado", "recomendado_por_terapeutas", "orden")
        }),
    )

    def get_queryset(self, request):
        # Precarga las imágenes para que la miniatura no dispare una consulta
        # extra por cada producto en la lista.
        return super().get_queryset(request).prefetch_related("imagenes")

    @admin.display(description="Foto")
    def miniatura(self, obj):
        primera = obj.imagenes.first()
        if primera and primera.imagen:
            return format_html(
                '<img src="{}" style="width:40px;height:40px;border-radius:50%;'
                'object-fit:cover;" />',
                primera.imagen.url,
            )
        return mark_safe(
            '<div style="width:40px;height:40px;border-radius:50%;background:#eee;'
            'display:flex;align-items:center;justify-content:center;'
            'color:#999;font-size:9px;text-align:center;">Sin foto</div>'
        )

    @admin.display(description="Precio sugerido")
    def precio_sugerido_col(self, obj):
        _, sugerido = obj.calcular_costo_y_precio_sugerido()
        return f"Bs. {sugerido}" if sugerido is not None else "—"

    @admin.display(description="Peso")
    def peso_col(self, obj):
        return f"{obj.peso_gramos} g" if obj.peso_gramos is not None else "—"

    @admin.display(description="Horas impr.")
    def horas_col(self, obj):
        return f"{obj.horas_impresion} h" if obj.horas_impresion is not None else "—"

    @admin.display(description="Costo y precio sugerido (calculado)")
    def precio_sugerido_detalle(self, obj):
        costo, sugerido = obj.calcular_costo_y_precio_sugerido()
        if costo is None:
            return mark_safe(
                "<span style='color:#999'>Completa filamento, peso y horas de impresión "
                "para ver el cálculo.</span>"
            )
        return format_html(
            "Costo estimado: <b>Bs. {}</b> &nbsp;|&nbsp; Precio sugerido (con margen configurado): "
            "<b>Bs. {}</b>",
            costo, sugerido,
        )


@admin.register(ConfiguracionSitio)
class ConfiguracionSitioAdmin(admin.ModelAdmin):
    readonly_fields = ("costo_hora_impresora_calculado",)
    fieldsets = (
        ("Datos de la tienda", {
            "fields": ("nombre_tienda", "texto_bienvenida", "horario_atencion")
        }),
        ("Contacto", {
            "fields": ("whatsapp_numero", "mensaje_whatsapp_base", "telefono", "email_contacto")
        }),
        ("Costo de la impresora (para calcular precios sugeridos)", {
            "fields": (
                "precio_impresora", "vida_util_horas",
                "potencia_impresora_kw", "precio_kwh",
                "mantenimiento_anual", "horas_uso_anual",
                "costo_hora_impresora_calculado",
            ),
            "description": "Carga estos datos una sola vez. El sistema calcula solo el costo por hora "
                            "de impresora abajo, y lo usa para sugerir precios en cada producto.",
        }),
        ("Fallas y margen", {
            "fields": ("tasa_fallas_pct", "margen_ganancia_pct"),
        }),
    )

    @admin.display(description="Costo por hora de impresora (calculado)")
    def costo_hora_impresora_calculado(self, obj):
        return format_html("<b>Bs. {}</b> por hora", obj.costo_hora_impresora)

    def has_add_permission(self, request):
        # Solo debe existir un registro de configuración (singleton)
        return not ConfiguracionSitio.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
