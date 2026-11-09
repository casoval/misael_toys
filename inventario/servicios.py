"""Lógica de negocio: descuentos, stock, ventas y anulaciones.

Las vistas solo validan lo que llega del formulario y llaman a estas
funciones; así las reglas viven en un único lugar (y se pueden testear).
"""
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .models import (
    Cotizacion, CotizacionItem, CotizacionItemPersonalizado, FotoItemPersonalizado, MovimientoStock,
    ReglaDescuento, Stock, Venta, VentaItem,
)

ENTERO = Decimal("1")


class ErrorNegocio(Exception):
    """Error que se le puede mostrar tal cual al usuario del panel."""


def _q(valor):
    """Redondea al entero más cercano (0.5 sube). Todo el dinero del panel va sin decimales."""
    return Decimal(valor).quantize(ENTERO, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Descuentos
# --------------------------------------------------------------------------
def calcular_totales(lineas, aplicar_reglas=True, descuento_extra=0, hoy=None):
    """Calcula subtotal, reglas aplicables y total, aplicando los descuentos PRODUCTO POR PRODUCTO.

    `lineas`: iterable de objetos/dicts con producto, cantidad, precio_unitario.

    Cada línea (cantidad × precio) recibe su propio descuento ya redondeado a entero; el total es
    la suma de las líneas. Así no hay decimales sueltos y el neto de cada producto (y por lo tanto
    el de cada ubicación) suma exactamente el total de la venta.
      · Reglas por %: se aplican a cada línea que entra en su alcance (todo el pedido, una categoría
        o un producto). El % se calcula sobre el valor de ESA línea y se redondea (0,5 sube).
      · Descuento adicional (Bs.): se reparte entre las líneas en proporción a lo que valen ya con
        sus promociones (método del mayor resto: suma exactamente lo pedido).
    Devuelve un dict con: subtotal, unidades, aplicadas (lista), descuento_reglas, descuento_extra,
    total, sugerencia (texto para animar a llegar a la siguiente regla) y `lineas`: una entrada por
    cada línea recibida, en el mismo orden, con bruto, descuento_reglas, descuento_extra, descuento y neto.
    """
    items = []
    for l in lineas:
        g = (lambda k: l[k]) if isinstance(l, dict) else (lambda k: getattr(l, k))
        items.append((g("producto"), int(g("cantidad")), _q(g("precio_unitario"))))

    unidades = sum(c for _, c, _ in items)
    brutos = [c * p for _, c, p in items]
    subtotal = _q(sum(brutos, Decimal("0")))
    desc_reglas_linea = [Decimal("0")] * len(items)

    aplicadas = []
    sugerencia = ""
    if aplicar_reglas and items:
        hoy = hoy or timezone.localdate()
        reglas = [
            r for r in ReglaDescuento.objects.select_related("categoria", "producto").filter(activa=True)
            if r.vigente(hoy)
        ]

        def lineas_en_alcance(regla):
            if regla.alcance == ReglaDescuento.CATEGORIA:
                return [i for i, (prod, _, _) in enumerate(items) if prod.categoria_id == regla.categoria_id]
            if regla.alcance == ReglaDescuento.PRODUCTO:
                return [i for i, (prod, _, _) in enumerate(items) if prod.pk == regla.producto_id]
            return list(range(len(items)))

        candidatas = []
        for r in reglas:
            idx = lineas_en_alcance(r)
            u = sum(items[i][1] for i in idx)
            if u and u >= r.minimo_unidades:
                por_linea = {i: _q(brutos[i] * r.porcentaje / 100) for i in idx}  # cada producto, redondeado
                candidatas.append({
                    "regla": r, "nombre": r.nombre, "porcentaje": r.porcentaje,
                    "unidades": u, "base": _q(sum((brutos[i] for i in idx), Decimal("0"))),
                    "monto": sum(por_linea.values(), Decimal("0")), "por_linea": por_linea,
                    "acumulable": r.acumulable,
                })
        aplicadas = [c for c in candidatas if c["acumulable"]]
        no_acum = [c for c in candidatas if not c["acumulable"]]
        if no_acum:
            aplicadas.append(max(no_acum, key=lambda c: c["monto"]))

        # Se aplican línea por línea; nunca se descuenta más de lo que vale el producto.
        for a in aplicadas:
            real = Decimal("0")
            for i, monto in a["por_linea"].items():
                monto = min(monto, brutos[i] - desc_reglas_linea[i])
                desc_reglas_linea[i] += monto
                real += monto
            a["monto"] = real

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

    descuento_reglas = sum(desc_reglas_linea, Decimal("0"))
    extra = max(_q(descuento_extra or 0), Decimal("0"))
    extra = min(extra, subtotal - descuento_reglas)
    extra_linea = repartir_total([b - d for b, d in zip(brutos, desc_reglas_linea)], extra)
    detalle = [
        {"bruto": b, "descuento_reglas": dr, "descuento_extra": de, "descuento": dr + de, "neto": b - dr - de}
        for b, dr, de in zip(brutos, desc_reglas_linea, extra_linea)
    ]
    return {
        "subtotal": subtotal,
        "unidades": unidades,
        "aplicadas": aplicadas,
        "descuento_reglas": descuento_reglas,
        "descuento_extra": extra,
        "total": subtotal - descuento_reglas - extra,
        "sugerencia": sugerencia,
        "lineas": detalle,
    }


def texto_reglas(aplicadas):
    """Texto plano para guardar qué reglas se aplicaron en el momento."""
    return "\n".join(
        f"{a['nombre']} ({a['porcentaje'].normalize():f}%): -Bs. {a['monto']:.0f}" for a in aplicadas
    )


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------
def _stock_bloqueado(producto, ubicacion):
    Stock.objects.get_or_create(producto=producto, ubicacion=ubicacion)
    return Stock.objects.select_for_update().get(producto=producto, ubicacion=ubicacion)


@transaction.atomic
def mover_stock(producto, ubicacion, delta, tipo, usuario=None, nota="", venta=None, permitir_negativo=False):
    """Suma/resta stock en una ubicación y deja el rastro en el historial.
    Solo las ventas pueden dejar el stock en negativo (`permitir_negativo`)."""
    if delta == 0:
        raise ErrorNegocio("La cantidad no puede ser 0.")
    stock = _stock_bloqueado(producto, ubicacion)
    # Solo una SALIDA puede bloquearse por falta de stock. Las entradas (ingresos,
    # anulaciones, traslados que llegan) siempre se permiten, aunque el stock
    # siga negativo después de sumarlas.
    if delta < 0 and stock.cantidad + delta < 0 and not permitir_negativo:
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


def repartir_total(pesos, total):
    """Reparte `total` (entero, lo realmente cobrado) entre ítems en proporción a `pesos`
    (cantidad × precio de cada ítem). Devuelve enteros que SUMAN EXACTAMENTE `total`
    (método del mayor resto), para que el monto por ubicación cuadre con el total vendido."""
    pesos = [Decimal(p) for p in pesos]
    total = Decimal(total)
    suma = sum(pesos)
    if not pesos or suma <= 0 or total <= 0:
        return [Decimal(0)] * len(pesos)
    exactos = [p * total / suma for p in pesos]
    pisos = [e.to_integral_value(rounding=ROUND_FLOOR) for e in exactos]
    faltan = int(total - sum(pisos))
    orden = sorted(range(len(pesos)), key=lambda i: (pisos[i] - exactos[i], i))  # mayor parte decimal primero
    for i in orden[:faltan]:
        pisos[i] += 1
    return pisos


@transaction.atomic
def carga_masiva(cambios, modo, usuario, nota=""):
    """Carga de stock de muchos productos a la vez.

    `cambios`: lista de (producto, ubicacion, cantidad).
    `modo`: "sumar" (ingreso: la cantidad se suma) o "fijar" (conteo: queda esa cantidad exacta).
    Todo o nada: si algo falla, no se guarda nada. Devuelve (aplicados, sin_cambio).
    Cada movimiento queda en el historial igual que si se hiciera uno por uno."""
    aplicados = sin_cambio = 0
    for producto, ubicacion, cantidad in cambios:
        if modo == "fijar":
            try:
                ajustar(producto, ubicacion, cantidad, usuario, f"Conteo en lote. {nota}".strip())
                aplicados += 1
            except ErrorNegocio as e:
                if "ya es la cantidad" in str(e):
                    sin_cambio += 1
                else:
                    raise
        else:
            registrar_ingreso(producto, ubicacion, cantidad, usuario, f"Carga en lote. {nota}".strip())
            aplicados += 1
    return aplicados, sin_cambio


# --------------------------------------------------------------------------
# Ventas
# --------------------------------------------------------------------------
@transaction.atomic
def registrar_venta(vendedor, items, cliente="", observaciones="", aplicar_reglas=True, descuento_extra=0):
    """`items`: lista de dicts producto, ubicacion (o None), cantidad, precio_unitario.

    NUNCA se bloquea por falta de stock: el producto puede existir físicamente
    sin estar registrado, o venderse para entregar después. Si el lugar no tiene
    suficiente, el stock queda en negativo (= unidades vendidas por cubrir) y
    en el ítem queda anotado cuántas unidades fueron "sin stock". Si no se
    elige lugar (ubicacion None), no se descuenta de ninguno."""
    if not items:
        raise ErrorNegocio("Agrega al menos un producto.")
    totales = calcular_totales(items, aplicar_reglas, descuento_extra)
    venta = Venta.objects.create(
        vendedor=vendedor, cliente=cliente.strip(), observaciones=observaciones.strip(),
        subtotal=totales["subtotal"], descuento_reglas=totales["descuento_reglas"],
        detalle_reglas=texto_reglas(totales["aplicadas"]),
        descuento_extra=totales["descuento_extra"], total=totales["total"],
    )
    for it, calc in zip(items, totales["lineas"]):
        ub = it.get("ubicacion")
        faltante = 0
        if ub is not None:
            # Se lee fila por fila: si el mismo producto aparece dos veces en el
            # mismo lugar, la segunda fila ya ve lo que descontó la primera.
            antes = _stock_bloqueado(it["producto"], ub).cantidad
            faltante = max(0, it["cantidad"] - max(antes, 0))
        VentaItem.objects.create(
            venta=venta, producto=it["producto"], ubicacion=ub, cantidad=it["cantidad"],
            faltante=faltante, precio_lista=_q(it["producto"].precio),
            precio_unitario=_q(it["precio_unitario"]), descuento=calc["descuento"],
        )
        if ub is not None:
            nota = f"Venta #{venta.pk}" + (f" (sin stock suficiente: faltaban {faltante})" if faltante else "")
            mover_stock(it["producto"], ub, -it["cantidad"], MovimientoStock.VENTA,
                        vendedor, nota, venta=venta, permitir_negativo=True)
    return venta


@transaction.atomic
def anular_venta(venta, usuario, motivo=""):
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.anulada:
        raise ErrorNegocio("Esta venta ya está anulada.")
    for it in venta.items.select_related("producto", "ubicacion"):
        if it.ubicacion is None:
            continue
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


# --------------------------------------------------------------------------
# Cotizaciones a medida (piezas que NO están en el catálogo)
# --------------------------------------------------------------------------
MAX_PIEZAS_A_MEDIDA = 20


def costear_pieza(peso_gramos, horas, costo_por_kg):
    """Costo estimado y precio sugerido de una pieza, con los parámetros del sitio (costo por hora de
    impresora, % de fallas, % de margen). Es la MISMA fórmula que usan los productos del catálogo.
    Devuelve el desglose en enteros (como todo el dinero del panel) más `costo_hora`."""
    from catalogo.models import ConfiguracionSitio

    config = ConfiguracionSitio.get()
    d = config.desglose_costo(peso_gramos, horas, costo_por_kg)
    return {
        "material": _q(d["material"]), "maquina": _q(d["maquina"]), "fallas": _q(d["fallas"]),
        "costo": _q(d["costo"]), "precio_sugerido": _q(d["precio_sugerido"]),
        "costo_hora": config.costo_hora_impresora,
        "tasa_fallas_pct": config.tasa_fallas_pct, "margen_pct": config.margen_ganancia_pct,
        # Sin datos de la impresora el costo de máquina sale 0 y el sugerido solo cubre el material.
        "config_incompleta": config.costo_hora_impresora <= 0,
    }


@transaction.atomic
def crear_cotizacion_a_medida(creador, piezas, cliente="", observaciones="", validez_dias=7, descuento_extra=0):
    """Crea una cotización cuyas piezas no están en el catálogo. No toca el stock ni los productos.

    `piezas`: lista de dicts con: nombre, descripcion, medidas (opcional), cantidad, precio_unitario,
    fotos (lista de archivos subidos, opcional) y costeo (None o dict con peso_gramos, horas,
    costo_por_kg, filamento (objeto o None) y filamento_nombre). Las promociones por cantidad
    del catálogo NO se aplican (el precio de cada pieza ya es el final); sí el descuento adicional."""
    from .imagenes import preparar_foto  # import diferido: imagenes usa ErrorNegocio de este módulo

    if not piezas:
        raise ErrorNegocio("Agrega al menos una pieza.")
    if len(piezas) > MAX_PIEZAS_A_MEDIDA:
        raise ErrorNegocio(f"Máximo {MAX_PIEZAS_A_MEDIDA} piezas por cotización.")

    # 1) Todo se valida y se prepara ANTES de escribir nada (si algo falla, no queda nada a medias).
    listas = []
    for n, pz in enumerate(piezas, start=1):
        nombre = (pz.get("nombre") or "").strip()
        if not nombre:
            raise ErrorNegocio(f"Pieza {n}: escribe el nombre.")
        descripcion = (pz.get("descripcion") or "").strip()
        if not descripcion:
            raise ErrorNegocio(f"Pieza {n} ({nombre}): escribe una descripción.")
        if int(pz.get("cantidad") or 0) <= 0:
            raise ErrorNegocio(f"Pieza {n} ({nombre}): la cantidad debe ser mayor que 0.")
        precio = _q(pz.get("precio_unitario") or 0)
        if precio <= 0:
            raise ErrorNegocio(f"Pieza {n} ({nombre}): escribe el precio final (mayor que 0).")
        archivos = list(pz.get("fotos") or [])
        if len(archivos) > FotoItemPersonalizado.MAX_FOTOS:
            raise ErrorNegocio(f"Pieza {n} ({nombre}): máximo {FotoItemPersonalizado.MAX_FOTOS} fotos.")
        fotos = [preparar_foto(a) for a in archivos]

        costeo = pz.get("costeo")
        datos_costeo = {}
        if costeo:
            calc = costear_pieza(costeo["peso_gramos"], costeo["horas"], costeo["costo_por_kg"])
            datos_costeo = {
                "filamento": costeo.get("filamento"), "filamento_nombre": (costeo.get("filamento_nombre") or "")[:80],
                "filamento_costo_kg": costeo["costo_por_kg"], "peso_gramos": costeo["peso_gramos"],
                "horas_impresion": costeo["horas"], "costo_estimado": calc["costo"],
                "precio_sugerido": calc["precio_sugerido"],
            }
        listas.append({
            "nombre": nombre[:150], "descripcion": descripcion, "medidas": (pz.get("medidas") or "").strip()[:300],
            "cantidad": int(pz["cantidad"]), "precio_unitario": precio, "fotos": fotos, **datos_costeo,
        })

    totales = calcular_totales(
        [{"producto": None, "cantidad": l["cantidad"], "precio_unitario": l["precio_unitario"]} for l in listas],
        aplicar_reglas=False, descuento_extra=descuento_extra,
    )
    cot = Cotizacion.objects.create(
        creador=creador, cliente=cliente.strip(), observaciones=observaciones.strip(),
        validez_dias=validez_dias, subtotal=totales["subtotal"], descuento_reglas=Decimal("0"),
        detalle_reglas="", descuento_extra=totales["descuento_extra"], total=totales["total"],
    )

    # 2) Se guarda cada pieza con sus fotos.
    for orden, datos in enumerate(listas):
        fotos = datos.pop("fotos")
        item = CotizacionItemPersonalizado.objects.create(cotizacion=cot, orden=orden, **datos)
        for k, contenido in enumerate(fotos):
            contenido.name = f"cot{cot.pk:05d}-p{orden + 1}-f{k + 1}.jpg"
            FotoItemPersonalizado.objects.create(item=item, imagen=contenido, orden=k)
    return cot
