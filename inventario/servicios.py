"""Lógica de negocio: descuentos, stock, ventas y anulaciones.

Las vistas solo validan lo que llega del formulario y llaman a estas
funciones; así las reglas viven en un único lugar (y se pueden testear).
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import (
    Cotizacion, CotizacionItem, MovimientoStock, ReglaDescuento, Stock, Venta, VentaItem,
)

CENTAVO = Decimal("0.01")


class ErrorNegocio(Exception):
    """Error que se le puede mostrar tal cual al usuario del panel."""


def _q(valor):
    return Decimal(valor).quantize(CENTAVO, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Descuentos
# --------------------------------------------------------------------------
def calcular_totales(lineas, aplicar_reglas=True, descuento_extra=0, hoy=None):
    """Calcula subtotal, reglas aplicables y total.

    `lineas`: iterable de objetos/dicts con producto, cantidad, precio_unitario.
    Devuelve un dict con:
      subtotal, unidades, aplicadas (lista), descuento_reglas, descuento_extra,
      total, sugerencia (texto para animar a llegar a la siguiente regla).
    """
    items = []
    for l in lineas:
        g = (lambda k: l[k]) if isinstance(l, dict) else (lambda k: getattr(l, k))
        items.append((g("producto"), int(g("cantidad")), Decimal(g("precio_unitario"))))

    unidades = sum(c for _, c, _ in items)
    subtotal = _q(sum((c * p for _, c, p in items), Decimal("0")))

    aplicadas = []
    sugerencia = ""
    if aplicar_reglas and items:
        hoy = hoy or timezone.localdate()
        reglas = [
            r for r in ReglaDescuento.objects.select_related("categoria", "producto").filter(activa=True)
            if r.vigente(hoy)
        ]

        def alcance(regla):
            if regla.alcance == ReglaDescuento.CATEGORIA:
                sel = [(c, p) for prod, c, p in items if prod.categoria_id == regla.categoria_id]
            elif regla.alcance == ReglaDescuento.PRODUCTO:
                sel = [(c, p) for prod, c, p in items if prod.pk == regla.producto_id]
            else:
                sel = [(c, p) for _, c, p in items]
            return sum(c for c, _ in sel), sum((c * p for c, p in sel), Decimal("0"))

        candidatas = []
        for r in reglas:
            u, base = alcance(r)
            if u and u >= r.minimo_unidades:
                candidatas.append({
                    "regla": r, "nombre": r.nombre, "porcentaje": r.porcentaje,
                    "unidades": u, "base": _q(base),
                    "monto": _q(base * r.porcentaje / 100), "acumulable": r.acumulable,
                })
        aplicadas = [c for c in candidatas if c["acumulable"]]
        no_acum = [c for c in candidatas if not c["acumulable"]]
        if no_acum:
            aplicadas.append(max(no_acum, key=lambda c: c["monto"]))

        # Sugerencia: la regla "de todo el pedido" más cercana que aún no se alcanza.
        faltan = [
            (r.minimo_unidades - unidades, r) for r in reglas
            if r.alcance == ReglaDescuento.TODO and r.minimo_unidades > unidades
        ]
        mejor_actual = max((a["porcentaje"] for a in aplicadas), default=Decimal("0"))
        faltan = [(f, r) for f, r in faltan if r.porcentaje > mejor_actual]
        if faltan:
            f, r = min(faltan, key=lambda x: x[0])
            sugerencia = f"Con {f} unidad{'es' if f != 1 else ''} más se aplica {r.porcentaje.normalize():f}% de descuento ({r.nombre})."

    descuento_reglas = min(_q(sum((a["monto"] for a in aplicadas), Decimal("0"))), subtotal)
    extra = max(_q(descuento_extra or 0), Decimal("0"))
    extra = min(extra, subtotal - descuento_reglas)
    return {
        "subtotal": subtotal,
        "unidades": unidades,
        "aplicadas": aplicadas,
        "descuento_reglas": descuento_reglas,
        "descuento_extra": extra,
        "total": _q(subtotal - descuento_reglas - extra),
        "sugerencia": sugerencia,
    }


def texto_reglas(aplicadas):
    """Texto plano para guardar qué reglas se aplicaron en el momento."""
    return "\n".join(
        f"{a['nombre']} ({a['porcentaje'].normalize():f}%): -Bs. {a['monto']}" for a in aplicadas
    )


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------
def _stock_bloqueado(producto, ubicacion):
    Stock.objects.get_or_create(producto=producto, ubicacion=ubicacion)
    return Stock.objects.select_for_update().get(producto=producto, ubicacion=ubicacion)


@transaction.atomic
def mover_stock(producto, ubicacion, delta, tipo, usuario=None, nota="", venta=None):
    """Suma/resta stock en una ubicación y deja el rastro en el historial."""
    if delta == 0:
        raise ErrorNegocio("La cantidad no puede ser 0.")
    stock = _stock_bloqueado(producto, ubicacion)
    if stock.cantidad + delta < 0:
        raise ErrorNegocio(
            f"No hay suficiente «{producto.nombre}» en {ubicacion.nombre} "
            f"(hay {stock.cantidad}, se pidió {-delta})."
        )
    stock.cantidad += delta
    stock.save(update_fields=["cantidad"])
    return MovimientoStock.objects.create(
        producto=producto, ubicacion=ubicacion, cantidad=delta, tipo=tipo,
        usuario=usuario, nota=nota, venta=venta,
    )


def registrar_ingreso(producto, ubicacion, cantidad, usuario, nota=""):
    """Primer ingreso en un lugar = 'cantidad inicial'; los siguientes = 'ingreso'."""
    if cantidad <= 0:
        raise ErrorNegocio("La cantidad debe ser mayor que 0.")
    es_inicial = not MovimientoStock.objects.filter(producto=producto, ubicacion=ubicacion).exists()
    tipo = MovimientoStock.INICIAL if es_inicial else MovimientoStock.INGRESO
    return mover_stock(producto, ubicacion, cantidad, tipo, usuario, nota)


@transaction.atomic
def trasladar(producto, origen, destino, cantidad, usuario, nota=""):
    if origen == destino:
        raise ErrorNegocio("El origen y el destino son el mismo lugar.")
    if cantidad <= 0:
        raise ErrorNegocio("La cantidad debe ser mayor que 0.")
    mover_stock(producto, origen, -cantidad, MovimientoStock.TRASLADO, usuario, f"A {destino.nombre}. {nota}".strip())
    mover_stock(producto, destino, cantidad, MovimientoStock.TRASLADO, usuario, f"Desde {origen.nombre}. {nota}".strip())


def ajustar(producto, ubicacion, nueva_cantidad, usuario, nota=""):
    """Corrige el stock a un número exacto (conteo físico, roturas, etc.)."""
    if nueva_cantidad < 0:
        raise ErrorNegocio("La cantidad no puede ser negativa.")
    with transaction.atomic():
        actual = _stock_bloqueado(producto, ubicacion).cantidad
        delta = nueva_cantidad - actual
        if delta == 0:
            raise ErrorNegocio("Esa ya es la cantidad registrada.")
        return mover_stock(producto, ubicacion, delta, MovimientoStock.AJUSTE, usuario,
                           f"Ajuste de {actual} a {nueva_cantidad}. {nota}".strip())


# --------------------------------------------------------------------------
# Ventas
# --------------------------------------------------------------------------
@transaction.atomic
def registrar_venta(vendedor, items, cliente="", observaciones="", aplicar_reglas=True, descuento_extra=0):
    """`items`: lista de dicts producto, ubicacion, cantidad, precio_unitario.
    Descuenta del stock del lugar elegido; si algo no alcanza, no se guarda nada."""
    if not items:
        raise ErrorNegocio("Agrega al menos un producto.")
    totales = calcular_totales(items, aplicar_reglas, descuento_extra)
    venta = Venta.objects.create(
        vendedor=vendedor, cliente=cliente.strip(), observaciones=observaciones.strip(),
        subtotal=totales["subtotal"], descuento_reglas=totales["descuento_reglas"],
        detalle_reglas=texto_reglas(totales["aplicadas"]),
        descuento_extra=totales["descuento_extra"], total=totales["total"],
    )
    # Se agrupa por (producto, lugar) para validar bien si el mismo producto
    # aparece en dos filas del mismo lugar.
    for it in items:
        VentaItem.objects.create(
            venta=venta, producto=it["producto"], ubicacion=it["ubicacion"],
            cantidad=it["cantidad"], precio_lista=it["producto"].precio,
            precio_unitario=_q(it["precio_unitario"]),
        )
        mover_stock(it["producto"], it["ubicacion"], -it["cantidad"], MovimientoStock.VENTA,
                    vendedor, f"Venta #{venta.pk}", venta=venta)
    return venta


@transaction.atomic
def anular_venta(venta, usuario, motivo=""):
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.anulada:
        raise ErrorNegocio("Esta venta ya está anulada.")
    for it in venta.items.select_related("producto", "ubicacion"):
        mover_stock(it.producto, it.ubicacion, it.cantidad, MovimientoStock.ANULACION,
                    usuario, f"Anulación venta #{venta.pk}", venta=venta)
    venta.anulada = True
    venta.anulada_por = usuario
    venta.anulada_en = timezone.now()
    venta.motivo_anulacion = motivo.strip()
    venta.save()
    return venta


# --------------------------------------------------------------------------
# Cotizaciones
# --------------------------------------------------------------------------
@transaction.atomic
def crear_cotizacion(creador, items, cliente="", observaciones="", validez_dias=7,
                     aplicar_reglas=True, descuento_extra=0):
    if not items:
        raise ErrorNegocio("Agrega al menos un producto.")
    totales = calcular_totales(items, aplicar_reglas, descuento_extra)
    cot = Cotizacion.objects.create(
        creador=creador, cliente=cliente.strip(), observaciones=observaciones.strip(),
        validez_dias=validez_dias, subtotal=totales["subtotal"],
        descuento_reglas=totales["descuento_reglas"], detalle_reglas=texto_reglas(totales["aplicadas"]),
        descuento_extra=totales["descuento_extra"], total=totales["total"],
    )
    for it in items:
        CotizacionItem.objects.create(
            cotizacion=cot, producto=it["producto"], cantidad=it["cantidad"],
            precio_unitario=_q(it["precio_unitario"]),
        )
    return cot
