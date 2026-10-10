"""Lectura de las filas de producto que llegan del formulario de venta/cotización."""
from decimal import Decimal, InvalidOperation

from catalogo.models import Producto

from .models import Ubicacion


def _decimal(texto):
    try:
        d = Decimal(str(texto).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def leer_decimal(texto, defecto=Decimal("0")):
    """Para campos opcionales (ej. descuento extra): vacío o inválido = defecto."""
    if not str(texto or "").strip():
        return defecto
    d = _decimal(texto)
    return defecto if d is None else d


def leer_lineas(post, con_ubicacion, estricto=True):
    """Devuelve (lineas, errores, filas_previas).

    - lineas: dicts listos para los servicios (producto, ubicacion, cantidad, precio_unitario).
    - errores: textos para mostrar al usuario (solo si `estricto`).
    - filas_previas: lo que se escribió, para volver a pintar el formulario si hay error.
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
            precio = producto.precio
        ubicacion = None
        if con_ubicacion:
            ubicacion = ubs.get(int(ub_txt)) if ub_txt.isdigit() else None
            if ubicacion is None:
                errores.append(f"Fila {n} ({producto.nombre}): elige de qué lugar sale.")
                continue
        lineas.append({"producto": producto, "ubicacion": ubicacion, "cantidad": cantidad, "precio_unitario": precio})
    return lineas, (errores if estricto else []), previas
