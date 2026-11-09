"""Lectura de las filas de producto que llegan del formulario de venta/cotización."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from catalogo.models import Filamento, Producto

from .models import FotoItemPersonalizado, Ubicacion


def _decimal(texto):
    try:
        d = Decimal(str(texto).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal("1"), rounding=ROUND_HALF_UP) if d.is_finite() and d >= 0 else None


def leer_decimal(texto, defecto=Decimal("0")):
    """Para campos opcionales (ej. descuento extra): vacío o inválido = defecto.
    El resultado siempre es un entero (sin decimales)."""
    if not str(texto or "").strip():
        return defecto
    d = _decimal(texto)
    return defecto if d is None else d


def leer_lineas(post, con_ubicacion, estricto=True, ubicaciones_permitidas=None):
    """Devuelve (lineas, errores, filas_previas).

    - lineas: dicts listos para los servicios (producto, ubicacion, cantidad, precio_unitario).
    - errores: textos para mostrar al usuario (solo si `estricto`).
    - filas_previas: lo que se escribió, para volver a pintar el formulario si hay error.
    - ubicaciones_permitidas: ids desde los que este usuario puede vender (None = cualquiera).
    """
    productos = post.getlist("producto")
    ubicaciones = post.getlist("ubicacion")
    cantidades = post.getlist("cantidad")
    precios = post.getlist("precio")

    prods = Producto.objects.select_related("categoria").in_bulk([int(p) for p in productos if p.isdigit()])
    ubs = Ubicacion.objects.filter(activa=True).in_bulk([int(u) for u in ubicaciones if u.isdigit()])

    lineas, errores, previas = [], [], []
    for i, pid in enumerate(productos):
        if not pid:
            continue
        cant_txt = cantidades[i] if i < len(cantidades) else ""
        precio_txt = precios[i] if i < len(precios) else ""
        ub_txt = ubicaciones[i] if i < len(ubicaciones) else ""
        previas.append({"producto": pid, "ubicacion": ub_txt, "cantidad": cant_txt, "precio": precio_txt})
        n = len(previas)

        producto = prods.get(int(pid)) if pid.isdigit() else None
        if producto is None:
            errores.append(f"Fila {n}: producto no válido.")
            continue
        cantidad = int(cant_txt) if cant_txt.strip().isdigit() else 0
        if cantidad <= 0:
            errores.append(f"Fila {n} ({producto.nombre}): la cantidad debe ser un número mayor que 0.")
            continue
        if precio_txt.strip():
            precio = _decimal(precio_txt)
            if precio is None:
                errores.append(f"Fila {n} ({producto.nombre}): precio no válido.")
                continue
        else:
            precio = producto.precio.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        ubicacion = None
        if con_ubicacion and ub_txt.strip():
            # Vacío = "no descontar de ningún lugar" (válido). Si viene un lugar, debe existir.
            ubicacion = ubs.get(int(ub_txt)) if ub_txt.isdigit() else None
            if ubicacion is None:
                errores.append(f"Fila {n} ({producto.nombre}): el lugar elegido no es válido.")
                continue
            if ubicaciones_permitidas is not None and ubicacion.pk not in ubicaciones_permitidas:
                errores.append(f"Fila {n} ({producto.nombre}): no puedes vender desde {ubicacion.nombre}; "
                               "esa ubicación no está asignada a tu usuario (solo la ves como referencia).")
                continue
        lineas.append({"producto": producto, "ubicacion": ubicacion, "cantidad": cantidad, "precio_unitario": precio})
    return lineas, (errores if estricto else []), previas


# --------------------------------------------------------------------------
# Piezas a medida (cotización de algo que no está en el catálogo)
# --------------------------------------------------------------------------
def _decimal_exacto(texto):
    """Número >= 0 con decimales (acepta coma o punto). Vacío o inválido = None."""
    try:
        d = Decimal(str(texto or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def horas_desde(horas_txt, minutos_txt):
    """'5' horas + '30' minutos -> Decimal('5.50'). Cualquiera de los dos puede ir vacío.
    Devuelve None si alguno viene mal escrito o si el total es 0."""
    h = _decimal_exacto(horas_txt) if str(horas_txt or "").strip() else Decimal("0")
    m = _decimal_exacto(minutos_txt) if str(minutos_txt or "").strip() else Decimal("0")
    if h is None or m is None:
        return None
    total = (h + m / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return total if total > 0 else None


def leer_costeo(peso_txt, horas_txt, minutos_txt, filamento_txt, otro_nombre="", otro_costo_txt=""):
    """Lee los datos de costeo de UNA pieza. Devuelve (costeo, error):
    - los datos de costeo son todo o nada: si no se escribió nada, (None, None);
    - si se escribió algo pero falta o está mal una parte, (None, "mensaje").
    El filamento sale de la lista de Filamentos o es «otro» (nombre y costo por kg escritos a mano)."""
    peso_txt, filamento_txt = str(peso_txt or "").strip(), str(filamento_txt or "").strip()
    hay_tiempo = bool(str(horas_txt or "").strip() or str(minutos_txt or "").strip())
    if not (peso_txt or hay_tiempo or filamento_txt):
        return None, None

    faltan = [n for n, ok in (("el filamento", filamento_txt), ("el peso", peso_txt), ("el tiempo de impresión", hay_tiempo)) if not ok]
    if faltan:
        return None, ("para calcular el costo falta " + " y ".join(faltan)
                      + " (o deja los tres vacíos si no quieres calcularlo)")

    peso = _decimal_exacto(peso_txt)
    if peso is None or peso <= 0 or peso > 50000:
        return None, "el peso debe ser un número mayor que 0 (en gramos)"
    horas = horas_desde(horas_txt, minutos_txt)
    if horas is None or horas > 9999:
        return None, "el tiempo de impresión no es válido (escribe horas y/o minutos)"

    filamento = None
    if filamento_txt == "otro":
        costo = _decimal_exacto(otro_costo_txt)
        if costo is None or costo <= 0 or costo > 99999:
            return None, "escribe el costo por kg del filamento «otro» (mayor que 0)"
        nombre = str(otro_nombre or "").strip()[:80] or "Otro"
    else:
        filamento = Filamento.objects.filter(pk=int(filamento_txt), activo=True).first() if filamento_txt.isdigit() else None
        if filamento is None:
            return None, "el filamento elegido no es válido"
        costo, nombre = filamento.costo_por_kg, filamento.nombre
    return {"peso_gramos": peso.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "horas": horas,
            "costo_por_kg": costo, "filamento": filamento, "filamento_nombre": nombre}, None


CAMPOS_PIEZA = ("nombre", "descripcion", "medidas", "cantidad", "precio", "filamento", "otro_nombre",
                "otro_costo", "peso", "horas", "minutos")


def leer_piezas_a_medida(post, files):
    """Devuelve (piezas, errores, previas) para el formulario «A medida».

    - piezas: dicts listos para servicios.crear_cotizacion_a_medida.
    - errores: textos para mostrar (ya con el número de pieza).
    - previas: lo escrito en cada pieza, para volver a pintar el formulario si hubo error.

    Cada pieza llega con campos con prefijo `p<índice>_` (ej. p3_nombre) y sus fotos en `p<índice>_fotos`.
    """
    indices = [i for i in post.getlist("pieza") if i.isdigit()]
    piezas, errores, previas = [], [], []
    for n, i in enumerate(indices, start=1):
        g = lambda campo: (post.get(f"p{i}_{campo}") or "").strip()
        previas.append({campo: g(campo) for campo in CAMPOS_PIEZA})
        nombre = g("nombre")
        etiqueta = f"Pieza {n}" + (f" ({nombre})" if nombre else "")
        fallos = len(errores)

        if not nombre:
            errores.append(f"{etiqueta}: escribe el nombre.")
        if not g("descripcion"):
            errores.append(f"{etiqueta}: escribe una descripción.")
        cantidad = int(g("cantidad")) if g("cantidad").isdigit() else 0
        if cantidad <= 0:
            errores.append(f"{etiqueta}: la cantidad debe ser un número mayor que 0.")
        precio = _decimal(g("precio")) if g("precio") else None
        if precio is None or precio <= 0:
            errores.append(f"{etiqueta}: escribe el precio final (un número mayor que 0).")
        costeo, error_costeo = leer_costeo(g("peso"), g("horas"), g("minutos"), g("filamento"), g("otro_nombre"), g("otro_costo"))
        if error_costeo:
            errores.append(f"{etiqueta}: {error_costeo}.")
        fotos = [f for f in files.getlist(f"p{i}_fotos") if getattr(f, "size", 0)]
        if len(fotos) > FotoItemPersonalizado.MAX_FOTOS:
            errores.append(f"{etiqueta}: máximo {FotoItemPersonalizado.MAX_FOTOS} fotos (elegiste {len(fotos)}).")
        if len(errores) == fallos:
            piezas.append({"nombre": nombre, "descripcion": g("descripcion"), "medidas": g("medidas"),
                           "cantidad": cantidad, "precio_unitario": precio, "fotos": fotos, "costeo": costeo})
    return piezas, errores, previas
