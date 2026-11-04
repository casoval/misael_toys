"""Inventario, ventas y cotizaciones (uso interno, NUNCA se muestra al cliente).

Todo vive bajo /panel/ y requiere login. El catálogo público (app `catalogo`)
no importa nada de aquí, así que el stock jamás se filtra al sitio público.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from catalogo.models import Categoria, Producto

DINERO = dict(max_digits=10, decimal_places=2)


class Ubicacion(models.Model):
    """Lugar físico donde se guardan productos (hasta 3 en la práctica,
    pero no hay límite en el código)."""

    nombre = models.CharField(max_length=80, unique=True)
    orden = models.PositiveIntegerField(default=0)
    activa = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Ubicación"
        verbose_name_plural = "Ubicaciones"
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre


class PerfilVendedor(models.Model):
    """Dónde vende cada usuario. El usuario ve y vende desde estas ubicaciones y,
    además, puede CONSULTAR (solo como referencia) el stock de las demás.

    Un usuario sin perfil o sin ubicaciones asignadas se trata como «todas son mías»
    (así los usuarios creados antes de este cambio siguen funcionando igual)."""

    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil_vendedor")
    ubicaciones = models.ManyToManyField(Ubicacion, blank=True, related_name="vendedores")

    class Meta:
        verbose_name = "Ubicaciones del vendedor"
        verbose_name_plural = "Ubicaciones de los vendedores"

    def __str__(self):
        return f"{self.usuario} → {', '.join(u.nombre for u in self.ubicaciones.all()) or 'todas'}"


def ids_ubicaciones_propias(usuario, activas):
    """Ids (de la lista `activas`) que son «propias» del usuario.
    Admin, o usuario sin ubicaciones asignadas → todas."""
    todas = [u.pk for u in activas]
    if usuario is None or usuario.is_staff:
        return todas
    perfil = getattr(usuario, "perfil_vendedor", None)
    if perfil is None:
        return todas
    asignadas = {u.pk for u in perfil.ubicaciones.all()}
    propias = [pk for pk in todas if pk in asignadas]
    return propias or todas


class Stock(models.Model):
    """Cuántas unidades de un producto hay en una ubicación."""

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="stocks")
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="stocks")
    # Puede ser negativo: significa "se vendió sin stock registrado" (producto que
    # existía pero no estaba contado, o que se fabrica para entregar después).
    # Al agregar stock nuevo, se compensa solo.
    cantidad = models.IntegerField(default=0)

    class Meta:
        verbose_name = "Stock"
        verbose_name_plural = "Stock"
        unique_together = ("producto", "ubicacion")

    def __str__(self):
        return f"{self.producto} @ {self.ubicacion}: {self.cantidad}"


class MovimientoStock(models.Model):
    """Historial de cada cambio de stock (quién, cuándo, por qué).
    `cantidad` es con signo: positivo entra, negativo sale."""

    INICIAL = "inicial"
    INGRESO = "ingreso"
    VENTA = "venta"
    ANULACION = "anulacion"
    TRASLADO = "traslado"
    AJUSTE = "ajuste"
    TIPOS = [
        (INICIAL, "Cantidad inicial"),
        (INGRESO, "Ingreso"),
        (VENTA, "Venta"),
        (ANULACION, "Venta anulada"),
        (TRASLADO, "Traslado"),
        (AJUSTE, "Ajuste"),
    ]

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="movimientos")
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="movimientos")
    cantidad = models.IntegerField()
    tipo = models.CharField(max_length=12, choices=TIPOS)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    nota = models.CharField(max_length=250, blank=True)
    venta = models.ForeignKey("Venta", null=True, blank=True, on_delete=models.SET_NULL, related_name="movimientos")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Movimiento de stock"
        verbose_name_plural = "Movimientos de stock"
        ordering = ["-creado", "-id"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.cantidad:+d} {self.producto} @ {self.ubicacion}"


class ReglaDescuento(models.Model):
    """Regla de descuento por cantidad / promoción, editable desde el admin.

    Ejemplos que ya existen: '5 o más productos = 5% del total' y
    '10 o más productos = 10% del total'.

    - alcance TODO: cuenta todas las unidades del pedido; el % se aplica al total.
    - alcance CATEGORIA / PRODUCTO: solo cuenta (y descuenta) esas unidades.
    - No acumulable (por defecto): entre las reglas no acumulables que
      correspondan se aplica SOLO la que más descuenta (así 5+ y 10+ no se suman).
    - Acumulable: se suma siempre que se cumpla (útil para promos puntuales).
    """

    TODO = "todo"
    CATEGORIA = "categoria"
    PRODUCTO = "producto"
    ALCANCES = [
        (TODO, "Todo el pedido"),
        (CATEGORIA, "Solo una categoría"),
        (PRODUCTO, "Solo un producto"),
    ]

    nombre = models.CharField(max_length=100, help_text="Ej: Descuento por 5 o más productos")
    minimo_unidades = models.PositiveIntegerField(help_text="Cantidad mínima de unidades para que aplique")
    porcentaje = models.DecimalField(max_digits=5, decimal_places=2, help_text="Ej: 5 = 5%")
    alcance = models.CharField(max_length=10, choices=ALCANCES, default=TODO)
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, null=True, blank=True, on_delete=models.CASCADE)
    acumulable = models.BooleanField(
        default=False,
        help_text="Si NO está marcado, solo se aplica la mejor de las reglas no acumulables.",
    )
    vigente_desde = models.DateField(null=True, blank=True)
    vigente_hasta = models.DateField(null=True, blank=True)
    activa = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Regla de descuento / promoción"
        verbose_name_plural = "Reglas de descuento / promociones"
        ordering = ["minimo_unidades", "nombre"]

    def __str__(self):
        return f"{self.nombre} ({self.porcentaje}%)"

    def vigente(self, hoy=None):
        hoy = hoy or timezone.localdate()
        if not self.activa:
            return False
        if self.vigente_desde and hoy < self.vigente_desde:
            return False
        if self.vigente_hasta and hoy > self.vigente_hasta:
            return False
        return True


class Venta(models.Model):
    vendedor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="ventas")
    cliente = models.CharField(max_length=120, blank=True)
    observaciones = models.TextField(blank=True)

    subtotal = models.DecimalField(**DINERO, default=Decimal("0"))
    descuento_reglas = models.DecimalField(**DINERO, default=Decimal("0"))
    detalle_reglas = models.TextField(blank=True, help_text="Reglas aplicadas en el momento de la venta")
    descuento_extra = models.DecimalField(**DINERO, default=Decimal("0"), help_text="Descuento manual, en Bs.")
    total = models.DecimalField(**DINERO, default=Decimal("0"))

    anulada = models.BooleanField(default=False)
    anulada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    anulada_en = models.DateTimeField(null=True, blank=True)
    motivo_anulacion = models.CharField(max_length=250, blank=True)

    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Venta"
        verbose_name_plural = "Ventas"
        ordering = ["-creado", "-id"]

    def __str__(self):
        return f"Venta #{self.pk} — Bs. {self.total}"

    @property
    def unidades(self):
        return sum(i.cantidad for i in self.items.all())

    @property
    def codigos_resumen(self):
        """Códigos de los productos, para verlos de un vistazo en las listas: 'J001 · J004 +2'.
        (usa los ítems ya precargados: sin consultas extra)"""
        codigos = [i.producto.codigo or i.producto.nombre for i in self.items.all()]
        texto = " · ".join(codigos[:4])
        return texto + (f" +{len(codigos) - 4}" if len(codigos) > 4 else "")

    @property
    def tiene_faltante(self):
        return any(i.faltante for i in self.items.all())

    @property
    def descuento_total(self):
        return self.descuento_reglas + self.descuento_extra


class VentaItem(models.Model):
    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name="items")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="ventas_items")
    # Nulo = no se descontó de ningún lugar (venta sin control de stock).
    ubicacion = models.ForeignKey(Ubicacion, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cantidad = models.PositiveIntegerField()
    faltante = models.PositiveIntegerField(
        default=0, help_text="Unidades que se vendieron aunque no había stock registrado suficiente")
    precio_lista = models.DecimalField(**DINERO, help_text="Precio del catálogo al momento de vender")
    precio_unitario = models.DecimalField(**DINERO, help_text="Precio realmente cobrado")
    descuento = models.DecimalField(
        **DINERO, default=Decimal("0"),
        help_text="Descuento (promociones + adicional) que le tocó a esta línea, en Bs. enteros. "
                  "La suma de todas las líneas es el descuento de la venta.")

    class Meta:
        verbose_name = "Ítem de venta"
        verbose_name_plural = "Ítems de venta"

    def __str__(self):
        return f"{self.cantidad} × {self.producto}"

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario

    @property
    def neto(self):
        """Lo que realmente se cobró por esta línea (ya con su parte de los descuentos)."""
        return self.subtotal - self.descuento


class Cotizacion(models.Model):
    """Cotización guardada (no toca el stock). Se descarga en PDF para el cliente."""

    creador = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="cotizaciones")
    cliente = models.CharField(max_length=120, blank=True)
    observaciones = models.TextField(blank=True)
    validez_dias = models.PositiveIntegerField(default=7)

    subtotal = models.DecimalField(**DINERO, default=Decimal("0"))
    descuento_reglas = models.DecimalField(**DINERO, default=Decimal("0"))
    detalle_reglas = models.TextField(blank=True)
    descuento_extra = models.DecimalField(**DINERO, default=Decimal("0"))
    total = models.DecimalField(**DINERO, default=Decimal("0"))

    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cotización"
        verbose_name_plural = "Cotizaciones"
        ordering = ["-creado", "-id"]

    def __str__(self):
        return f"Cotización {self.numero}"

    @property
    def numero(self):
        return f"{self.pk:05d}" if self.pk else "—"

    @property
    def unidades(self):
        return sum(i.cantidad for i in self.items.all())

    @property
    def codigos_resumen(self):
        """Códigos de los productos, para verlos de un vistazo en las listas: 'J001 · J004 +2'.
        (usa los ítems ya precargados: sin consultas extra)"""
        codigos = [i.producto.codigo or i.producto.nombre for i in self.items.all()]
        texto = " · ".join(codigos[:4])
        return texto + (f" +{len(codigos) - 4}" if len(codigos) > 4 else "")

    @property
    def descuento_total(self):
        return self.descuento_reglas + self.descuento_extra


class CotizacionItem(models.Model):
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="items")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="+")
    cantidad = models.PositiveIntegerField()
    precio_unitario = models.DecimalField(**DINERO)

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario
