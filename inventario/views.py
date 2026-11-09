import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Prefetch, Q, Sum
from django.db.models.functions import Lower
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from catalogo.models import Atributo, Categoria, ConfiguracionSitio, Filamento, Producto, ValorAtributo

from . import servicios
from . import catalogo_pdf
from .fotos import foto_panel
from .lineas import leer_costeo, leer_decimal, leer_lineas, leer_piezas_a_medida
from .models import (
    Cotizacion, CotizacionItem, CotizacionItemPersonalizado, FotoItemPersonalizado, MovimientoStock, PerfilVendedor, Stock, Ubicacion, Venta, VentaItem,
    ids_ubicaciones_propias,
)
from .pdf import generar_pdf_cotizacion, generar_pdf_recibo
from .servicios import ErrorNegocio

User = get_user_model()

MAX_FALLOS_LOGIN = 8
BLOQUEO_SEGUNDOS = 10 * 60


# --------------------------------------------------------------------------
# Acceso
# --------------------------------------------------------------------------
def solo_admin(vista):
    """Login obligatorio + ser administrador (is_staff). Los vendedores no lo son."""
    @wraps(vista)
    @login_required
    def envuelta(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envuelta


class LoginPanel(LoginView):
    """Login del panel, con bloqueo temporal tras varios intentos fallidos
    (las contraseñas no tienen restricciones, así que esto es la protección)."""

    template_name = "panel/login.html"
    redirect_authenticated_user = True

    def _clave(self):
        usuario = (self.request.POST.get("username") or "").strip().lower()
        return f"panel-login:{usuario}:{self.request.META.get('REMOTE_ADDR', '')}"

    def post(self, request, *args, **kwargs):
        if cache.get(self._clave(), 0) >= MAX_FALLOS_LOGIN:
            form = self.get_form()
            form.add_error(None, "Demasiados intentos fallidos. Espera unos minutos e inténtalo de nuevo.")
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        clave = self._clave()
        cache.add(clave, 0, BLOQUEO_SEGUNDOS)
        try:
            cache.incr(clave)
        except ValueError:
            cache.set(clave, 1, BLOQUEO_SEGUNDOS)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._clave())
        return super().form_valid(form)


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------
def _filtros_panel():
    """Los mismos filtros que se van generando en el catálogo público (categorías y
    atributos como Material, Edad, Color…), construidos a partir de lo que el admin
    crea. Solo aparecen los que tienen al menos un producto, para no mostrar
    filtros "fantasma". Los usan Stock, Vender y Cotizar."""
    return {
        "f_categorias": list(
            Categoria.objects.filter(productos__isnull=False).distinct().order_by("orden", "nombre")
        ),
        "f_atributos": list(
            Atributo.objects.filter(mostrar_como_filtro=True, valores__productos__isnull=False)
            .distinct().order_by("orden", "nombre")
            .prefetch_related(Prefetch(
                "valores", queryset=ValorAtributo.objects.filter(productos__isnull=False).distinct().order_by("valor")))
        ),
        "f_ubicaciones": list(Ubicacion.objects.filter(activa=True)),
        "f_disenos": catalogo_pdf.opciones(),  # diseños del catálogo imprimible (4 a 18 por hoja)
    }


def _ids_valores(producto):
    """Ids de los valores de atributo del producto, como ' 3 7 9 ' (para filtrar en el navegador)."""
    return " " + " ".join(str(v.pk) for v in producto.atributos.all()) + " "


def _lugares_con_propias(request):
    """(ubicaciones activas, ids propias del usuario). Las propias van primero:
    son donde vende; las demás se muestran solo como referencia."""
    activas = list(Ubicacion.objects.filter(activa=True))
    propias = set(ids_ubicaciones_propias(request.user, activas))
    activas.sort(key=lambda u: u.pk not in propias)  # estable: propias primero, mismo orden entre sí
    return activas, propias


@login_required
def stock_lista(request):
    """Todos los productos con su stock. El número grande es el de las ubicaciones del
    usuario; el de las otras ubicaciones se ve aparte, como referencia. Los filtros (texto,
    categoría, material, lugar…) se aplican al instante en el navegador (ver panel_vista.js)."""
    ubicaciones, propias = _lugares_con_propias(request)
    productos = (
        Producto.objects.prefetch_related("stocks", "imagenes", "atributos").order_by("nombre")
    )
    ids_ub = [u.pk for u in ubicaciones]
    filas = []
    for p in productos:
        por_ub = {s.ubicacion_id: s.cantidad for s in p.stocks.all() if s.ubicacion_id in ids_ub}
        total_otras = sum(c for pk, c in por_ub.items() if pk not in propias)
        filas.append({
            "producto": p, "total": sum(c for pk, c in por_ub.items() if pk in propias),
            "total_otras": total_otras, "foto": foto_panel(p),
            "neg": sum(1 for pk, c in por_ub.items() if pk in propias and c < 0),
            "valores": _ids_valores(p),
            "precio": p.precio.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "lugares": [{"ubicacion": u, "cantidad": por_ub.get(u.pk, 0), "propia": u.pk in propias}
                        for u in ubicaciones],
        })
    resumen_ub = []
    for u in ubicaciones:
        cants = [f["lugares"][i]["cantidad"] for f in filas for i, x in enumerate(f["lugares"]) if x["ubicacion"].pk == u.pk]
        resumen_ub.append({
            "ubicacion": u, "propia": u.pk in propias,
            "unidades": sum(c for c in cants if c > 0),
            "productos": sum(1 for c in cants if c > 0),
            "agotados": sum(1 for c in cants if c <= 0),
            "por_cubrir": sum(-c for c in cants if c < 0),
        })
    return render(request, "panel/stock_lista.html", {
        "filas": filas, "ubicaciones": ubicaciones, "resumen_ub": resumen_ub, "ids_propias": propias,
        "total_productos": len(filas),
        "hay_otras": any(u.pk not in propias for u in ubicaciones),
        "mis_ubicaciones": [u for u in ubicaciones if u.pk in propias],
        # Unidades vendidas sin stock registrado (stock negativo) en MIS ubicaciones, por cubrir
        "por_cubrir": sum(-l["cantidad"] for f in filas for l in f["lugares"] if l["propia"] and l["cantidad"] < 0),
        **_filtros_panel(),
    })


@login_required
def stock_producto(request, pk):
    producto = get_object_or_404(Producto.objects.select_related("categoria"), pk=pk)
    ubicaciones, propias = _lugares_con_propias(request)
    cantidades = {s.ubicacion_id: s.cantidad for s in Stock.objects.filter(producto=producto)}
    lugares = [{"ubicacion": u, "cantidad": cantidades.get(u.pk, 0), "propia": u.pk in propias} for u in ubicaciones]
    mios = [l for l in lugares if l["propia"]]
    ventas_recientes = (
        VentaItem.objects.filter(producto=producto, venta__anulada=False)
        .select_related("venta", "venta__vendedor", "ubicacion").order_by("-venta__creado")[:10]
    )
    movimientos = MovimientoStock.objects.filter(producto=producto).select_related("ubicacion", "usuario")[:25]
    return render(request, "panel/stock_producto.html", {
        "producto": producto, "lugares": lugares, "ubicaciones": ubicaciones,
        "hay_otras": len(mios) < len(lugares),
        "total": sum(l["cantidad"] for l in mios),
        "total_otras": sum(l["cantidad"] for l in lugares if not l["propia"]),
        "por_cubrir": sum(-l["cantidad"] for l in mios if l["cantidad"] < 0),
        "ventas_recientes": ventas_recientes, "movimientos": movimientos,
    })


@solo_admin
def stock_carga(request):
    """Cargar stock de muchos productos a la vez (tabla: productos × ubicaciones).
    Modo «sumar»: lo escrito se suma al stock. Modo «fijar»: conteo, queda esa cantidad."""
    ubicaciones = list(Ubicacion.objects.filter(activa=True))
    productos = list(Producto.objects.select_related("categoria").prefetch_related("stocks", "imagenes").order_by("nombre"))
    por_id = {p.pk: p for p in productos}
    por_ubic = {u.pk: u for u in ubicaciones}
    modo = request.POST.get("modo") if request.POST.get("modo") in ("sumar", "fijar") else "sumar"
    nota = request.POST.get("nota", "").strip()[:200]
    escritos, errores, cambios = {}, [], []

    if request.method == "POST":
        for clave, valor in request.POST.items():
            if not clave.startswith("c_") or not valor.strip():
                continue
            try:
                _, pid, uid = clave.split("_")
                producto, ubic = por_id[int(pid)], por_ubic[int(uid)]
            except (ValueError, KeyError):
                continue
            escritos[clave] = valor.strip()
            n = _entero(valor)
            if n is None or (modo == "sumar" and n <= 0) or (modo == "fijar" and n < 0):
                errores.append(f"{producto.codigo or producto.nombre} en {ubic.nombre}: "
                               f"«{valor.strip()}» no es una cantidad válida"
                               f"{' (debe ser mayor que 0)' if modo == 'sumar' else ' (0 o más)'}.")
                continue
            cambios.append((producto, ubic, n))
        if not escritos and not errores:
            errores.append("No escribiste ninguna cantidad.")
        if not errores:
            try:
                cambios.sort(key=lambda c: (c[0].nombre.lower(), c[1].orden))
                aplicados, sin_cambio = servicios.carga_masiva(cambios, modo, request.user, nota)
                n_prod = len({c[0].pk for c in cambios})
                texto = (f"Se actualizaron {aplicados} cantidad{'es' if aplicados != 1 else ''} "
                         f"de {n_prod} producto{'s' if n_prod != 1 else ''}.")
                if sin_cambio:
                    texto += f" ({sin_cambio} ya tenía{'n' if sin_cambio != 1 else ''} esa cantidad.)"
                messages.success(request, texto)
                return redirect("panel:stock")
            except ErrorNegocio as e:
                errores.append(str(e))

    filas = []
    for p in productos:
        hay = {s.ubicacion_id: s.cantidad for s in p.stocks.all()}
        filas.append({
            "producto": p, "foto": foto_panel(p),
            "total": sum(hay.get(u.pk, 0) for u in ubicaciones),
            "celdas": [{"ubicacion": u, "hay": hay.get(u.pk, 0), "campo": f"c_{p.pk}_{u.pk}",
                        "valor": escritos.get(f"c_{p.pk}_{u.pk}", "")} for u in ubicaciones],
        })
    return render(request, "panel/stock_carga.html", {
        "filas": filas, "ubicaciones": ubicaciones, "modo": modo, "nota": nota, "errores": errores,
        "categorias": Categoria.objects.filter(productos__isnull=False).distinct().order_by("orden", "nombre"),
    })


def _entero(texto):
    t = (texto or "").strip()
    return int(t) if t.lstrip("-").isdigit() else None


def _ubicacion(pk):
    return Ubicacion.objects.filter(pk=pk, activa=True).first() if str(pk).isdigit() else None


@solo_admin
@require_POST
def stock_ingreso(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    ub, cant = _ubicacion(request.POST.get("ubicacion")), _entero(request.POST.get("cantidad"))
    try:
        if ub is None or cant is None:
            raise ErrorNegocio("Elige el lugar y escribe una cantidad válida.")
        servicios.registrar_ingreso(producto, ub, cant, request.user, request.POST.get("nota", "").strip())
        messages.success(request, f"Se agregaron {cant} unidades en {ub.nombre}.")
    except ErrorNegocio as e:
        messages.error(request, str(e))
    return redirect("panel:stock_producto", pk=pk)


@solo_admin
@require_POST
def stock_traslado(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    o, d = _ubicacion(request.POST.get("origen")), _ubicacion(request.POST.get("destino"))
    cant = _entero(request.POST.get("cantidad"))
    try:
        if o is None or d is None or cant is None:
            raise ErrorNegocio("Elige origen, destino y una cantidad válida.")
        servicios.trasladar(producto, o, d, cant, request.user, request.POST.get("nota", "").strip())
        messages.success(request, f"Se trasladaron {cant} unidades de {o.nombre} a {d.nombre}.")
    except ErrorNegocio as e:
        messages.error(request, str(e))
    return redirect("panel:stock_producto", pk=pk)


@solo_admin
@require_POST
def stock_ajuste(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    ub, cant = _ubicacion(request.POST.get("ubicacion")), _entero(request.POST.get("cantidad"))
    try:
        if ub is None or cant is None:
            raise ErrorNegocio("Elige el lugar y escribe la cantidad real que hay.")
        servicios.ajustar(producto, ub, cant, request.user, request.POST.get("nota", "").strip())
        messages.success(request, f"Stock de {ub.nombre} corregido a {cant}.")
    except ErrorNegocio as e:
        messages.error(request, str(e))
    return redirect("panel:stock_producto", pk=pk)


# --------------------------------------------------------------------------
# Formulario compartido de venta / cotización
# --------------------------------------------------------------------------
def _datos_productos(request):
    """Productos con su stock por lugar (todos los lugares activos, incluso con 0 o
    negativo), para las tarjetas y el selector del formulario. `total` es el de las
    ubicaciones del usuario; `otras` el de las demás (solo referencia)."""
    ubicaciones, propias = _lugares_con_propias(request)
    ids = {u.pk for u in ubicaciones}
    productos = Producto.objects.prefetch_related("stocks", "imagenes", "atributos").order_by("nombre")
    datos = []
    for p in productos:
        stock = {str(u.pk): 0 for u in ubicaciones}
        for s in p.stocks.all():
            if s.ubicacion_id in ids:
                stock[str(s.ubicacion_id)] = s.cantidad
        datos.append({"id": p.pk, "nombre": p.nombre,
                      "precio": str(p.precio.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                      "stock": stock,
                      "total": sum(c for k, c in stock.items() if int(k) in propias),
                      "otras": sum(c for k, c in stock.items() if int(k) not in propias),
                      "foto": foto_panel(p),
                      "categoria": str(p.categoria_id or ""), "valores": [v.pk for v in p.atributos.all()],
                      "codigo": p.codigo or ""})
    return datos, ubicaciones, propias


def _contexto_formulario(request, tipo, previas=None, errores=None):
    datos, ubicaciones, propias = _datos_productos(request)
    return {
        "tipo": tipo,
        "productos_json": datos,
        "ubicaciones_json": [{"id": u.pk, "nombre": u.nombre, "propia": u.pk in propias} for u in ubicaciones],
        "hay_otras": any(u.pk not in propias for u in ubicaciones),
        "filas_json": previas or [],
        "errores": errores or [],
        "post": request.POST if request.method == "POST" else {},
        **_filtros_panel(),
        # Al abrir el formulario las promociones vienen marcadas.
        "aplicar_reglas": request.method != "POST" or request.POST.get("aplicar_reglas") == "on",
    }


@login_required
def calcular(request):
    """Vista previa (HTMX) del total mientras se arma la venta/cotización."""
    if request.method != "POST":
        return HttpResponse(status=405)
    lineas, _, _ = leer_lineas(request.POST, con_ubicacion=False, estricto=False)
    t = servicios.calcular_totales(
        lineas, aplicar_reglas=request.POST.get("aplicar_reglas") == "on",
        descuento_extra=leer_decimal(request.POST.get("descuento_extra")),
    )
    return render(request, "panel/_resumen.html", {"t": t})


# --------------------------------------------------------------------------
# Ventas
# --------------------------------------------------------------------------
@login_required
def venta_nueva(request):
    if request.method == "POST":
        _, propias = _lugares_con_propias(request)
        lineas, errores, previas = leer_lineas(request.POST, con_ubicacion=True, ubicaciones_permitidas=propias)
        if not errores:
            try:
                venta = servicios.registrar_venta(
                    request.user, lineas,
                    cliente=request.POST.get("cliente", ""), observaciones=request.POST.get("observaciones", ""),
                    aplicar_reglas=request.POST.get("aplicar_reglas") == "on",
                    descuento_extra=leer_decimal(request.POST.get("descuento_extra")),
                )
                messages.success(request, f"Venta #{venta.pk} registrada: Bs. {venta.total}.")
                return redirect("panel:venta_detalle", pk=venta.pk)
            except ErrorNegocio as e:
                errores = [str(e)]
        return render(request, "panel/venta_nueva.html", _contexto_formulario(request, "venta", previas, errores))
    return render(request, "panel/venta_nueva.html", _contexto_formulario(request, "venta"))


def _ventas_visibles(request):
    qs = Venta.objects.select_related("vendedor").prefetch_related("items__producto")
    if not request.user.is_staff:
        qs = qs.filter(vendedor=request.user)
    return qs


def _buscar_en_documento(texto, modelo_item, campo_documento, campo_usuario, con_piezas_a_medida=False):
    """Condición para buscar ventas/cotizaciones por el CÓDIGO o el nombre de alguno de sus productos,
    por cliente, por usuario (vendedor/creador), por observaciones o por número. Usa una subconsulta
    (no un JOIN) para que los totales no se dupliquen. En cotizaciones también busca en las piezas
    a medida (nombre y descripción)."""
    con_producto = modelo_item.objects.filter(
        Q(producto__codigo__icontains=texto) | Q(producto__nombre__icontains=texto)
    ).values(campo_documento)
    condicion = (Q(pk__in=con_producto) | Q(cliente__icontains=texto)
                 | Q(observaciones__icontains=texto) | Q(**{f"{campo_usuario}__username__icontains": texto}))
    if con_piezas_a_medida:
        a_medida = CotizacionItemPersonalizado.objects.filter(
            Q(nombre__icontains=texto) | Q(descripcion__icontains=texto)).values("cotizacion_id")
        condicion |= Q(pk__in=a_medida)
    numero = texto.lstrip("#").strip()
    if numero.isdigit():
        condicion |= Q(pk=int(numero))
    return condicion


def _fecha(texto):
    """'2026-10-13' → date; si viene vacío o mal escrito, None (así nunca da error 500)."""
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return None


def _orden_documentos(campo_usuario):
    """Opciones de orden para las listas de Ventas y Cotizaciones: {clave: (etiqueta, campos)}."""
    return {
        "recientes": ("Más recientes primero", ("-creado", "-id")),
        "antiguos": ("Más antiguos primero", ("creado", "id")),
        "numero": ("N° (menor a mayor)", ("id",)),
        "total_desc": ("Mayor monto primero", ("-total", "-id")),
        "total_asc": ("Menor monto primero", ("total", "id")),
        "cliente": ("Cliente (A-Z)", (Lower("cliente"), "-id")),
        "usuario": ("Vendedor (A-Z)" if campo_usuario == "vendedor" else "Creador (A-Z)",
                    (Lower(f"{campo_usuario}__username"), "-id")),
    }


def _filtrar_y_ordenar(request, qs, modelo_item, campo_documento, campo_usuario, con_piezas_a_medida=False):
    """Búsqueda, rango de fechas, usuario y orden, compartidos por Ventas y Cotizaciones."""
    texto = request.GET.get("q", "").strip()
    usuario = request.GET.get("vendedor", "")
    desde, hasta = _fecha(request.GET.get("desde")), _fecha(request.GET.get("hasta"))
    ordenes = _orden_documentos(campo_usuario)
    orden = request.GET.get("orden", "")
    if orden not in ordenes:
        orden = "recientes"
    if texto:
        qs = qs.filter(_buscar_en_documento(texto, modelo_item, campo_documento, campo_usuario, con_piezas_a_medida))
    if request.user.is_staff and usuario.isdigit():
        qs = qs.filter(**{f"{campo_usuario}_id": int(usuario)})
    if desde:
        qs = qs.filter(creado__date__gte=desde)
    if hasta:
        qs = qs.filter(creado__date__lte=hasta)
    qs = qs.order_by(*ordenes[orden][1])
    filtros = {
        "q": texto, "vendedor": usuario, "orden": orden,
        "desde": desde.isoformat() if desde else "", "hasta": hasta.isoformat() if hasta else "",
    }
    return qs, filtros, [(k, v[0]) for k, v in ordenes.items()]


@login_required
def ventas_lista(request):
    qs, filtros, ordenes = _filtrar_y_ordenar(request, _ventas_visibles(request), VentaItem, "venta_id", "vendedor")

    ubicacion = request.GET.get("ubicacion", "")
    if ubicacion == "ninguna":
        qs = qs.filter(pk__in=VentaItem.objects.filter(ubicacion__isnull=True).values("venta_id"))
    elif ubicacion.isdigit():
        qs = qs.filter(pk__in=VentaItem.objects.filter(ubicacion_id=int(ubicacion)).values("venta_id"))
    else:
        ubicacion = ""
    filtros["ubicacion"] = ubicacion

    validas = qs.filter(anulada=False)
    resumen = validas.aggregate(total=Sum("total"), n=Count("id"))
    items_validos = VentaItem.objects.filter(venta__in=validas)
    if ubicacion == "ninguna":
        items_validos = items_validos.filter(ubicacion__isnull=True)
    elif ubicacion:
        items_validos = items_validos.filter(ubicacion_id=int(ubicacion))
    # Por ubicación: de dónde salió lo vendido. El monto es NETO: cada producto ya trae guardado su parte
    # del descuento (se calcula producto por producto al vender), así que la suma de todas las
    # ubicaciones da exactamente el «Total vendido». (Con filtro de ubicación se muestra solo esa fila.)
    neto = ExpressionWrapper(F("cantidad") * F("precio_unitario") - F("descuento"),
                             output_field=DecimalField(max_digits=14, decimal_places=2))
    por_ubicacion = [
        {"nombre": r["ubicacion__nombre"] or "Sin descontar de ningún lugar", "unidades": r["u"], "monto": r["m"]}
        for r in items_validos.values("ubicacion__nombre")
        .annotate(u=Sum("cantidad"), m=Sum(neto)).order_by("-u", "ubicacion__nombre")
    ]
    mas_vendidos = list(
        items_validos.values("producto__codigo", "producto__nombre")
        .annotate(u=Sum("cantidad")).order_by("-u", "producto__nombre")[:5]
    )
    unidades = items_validos.aggregate(u=Sum("cantidad"))["u"] or 0
    por_vendedor = []
    if request.user.is_staff:
        por_vendedor = list(
            validas.values("vendedor__username").annotate(total=Sum("total"), n=Count("id")).order_by("-total")
        )
    return render(request, "panel/ventas_lista.html", {
        "ventas": qs[:200], "resumen": resumen, "unidades": unidades, "por_vendedor": por_vendedor,
        "por_ubicacion": por_ubicacion, "mas_vendidos": mas_vendidos,
        "ubicaciones": Ubicacion.objects.filter(activa=True),
        "vendedores": User.objects.filter(ventas__isnull=False).distinct() if request.user.is_staff else [],
        "f": filtros, "ordenes": ordenes,
    })


@login_required
def venta_detalle(request, pk):
    venta = get_object_or_404(_ventas_visibles(request), pk=pk)
    items = list(venta.items.select_related("producto", "ubicacion"))
    if request.user.is_staff:
        for it in items:  # alerta de "vendido bajo el costo" (solo la ve el admin)
            costo, _ = it.producto.calcular_costo_y_precio_sugerido()
            it.bajo_costo = costo is not None and it.precio_unitario < costo
    return render(request, "panel/venta_detalle.html", {"venta": venta, "items": items})


@login_required
def venta_recibo(request, pk):
    """Recibo de la venta en PDF (una sola hoja), solo como respaldo."""
    venta = get_object_or_404(_ventas_visibles(request), pk=pk)
    respuesta = HttpResponse(generar_pdf_recibo(venta), content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="recibo-{venta.pk:05d}.pdf"'
    return respuesta


@solo_admin
@require_POST
def venta_anular(request, pk):
    venta = get_object_or_404(Venta, pk=pk)
    try:
        servicios.anular_venta(venta, request.user, request.POST.get("motivo", ""))
        messages.success(request, f"Venta #{venta.pk} anulada y el stock fue devuelto.")
    except ErrorNegocio as e:
        messages.error(request, str(e))
    return redirect("panel:venta_detalle", pk=pk)


# --------------------------------------------------------------------------
# Cotizaciones
# --------------------------------------------------------------------------
@login_required
def cotizacion_nueva(request):
    if request.method == "POST":
        lineas, errores, previas = leer_lineas(request.POST, con_ubicacion=False)
        validez = _entero(request.POST.get("validez_dias")) or 7
        if not errores:
            try:
                cot = servicios.crear_cotizacion(
                    request.user, lineas, cliente=request.POST.get("cliente", ""),
                    observaciones=request.POST.get("observaciones", ""), validez_dias=max(1, validez),
                    aplicar_reglas=request.POST.get("aplicar_reglas") == "on",
                    descuento_extra=leer_decimal(request.POST.get("descuento_extra")),
                )
                messages.success(request, f"Cotización {cot.numero} lista. Descárgala en PDF para enviarla.")
                return redirect("panel:cotizacion_detalle", pk=cot.pk)
            except ErrorNegocio as e:
                errores = [str(e)]
        return render(request, "panel/cotizacion_nueva.html", _contexto_formulario(request, "cotizacion", previas, errores))
    return render(request, "panel/cotizacion_nueva.html", _contexto_formulario(request, "cotizacion"))


def _cotizaciones_visibles(request):
    qs = Cotizacion.objects.select_related("creador").prefetch_related("items__producto", "items_personalizados")
    return qs if request.user.is_staff else qs.filter(creador=request.user)


@login_required
def cotizaciones_lista(request):
    cotizaciones, filtros, ordenes = _filtrar_y_ordenar(
        request, _cotizaciones_visibles(request), CotizacionItem, "cotizacion_id", "creador", con_piezas_a_medida=True)
    return render(request, "panel/cotizaciones_lista.html", {
        "cotizaciones": cotizaciones[:200], "f": filtros, "ordenes": ordenes,
        "creadores": User.objects.filter(cotizaciones__isnull=False).distinct() if request.user.is_staff else [],
    })


@login_required
def cotizacion_detalle(request, pk):
    cot = get_object_or_404(_cotizaciones_visibles(request), pk=pk)
    return render(request, "panel/cotizacion_detalle.html", {
        "cot": cot, "items": cot.items.select_related("producto"),
        # Las piezas a medida siguen la numeración de los productos del catálogo (como en el PDF).
        "piezas": cot.items_personalizados.prefetch_related("fotos"), "primer_n_pieza": cot.items.count() + 1,
    })


@login_required
def cotizacion_pdf(request, pk):
    cot = get_object_or_404(_cotizaciones_visibles(request), pk=pk)
    respuesta = HttpResponse(generar_pdf_cotizacion(cot), content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="cotizacion-{cot.numero}.pdf"'
    return respuesta


# --------------------------------------------------------------------------
# Cotización a medida: piezas que NO están en el catálogo (solo admin: maneja costos)
# --------------------------------------------------------------------------
def _pct(valor):
    """Porcentaje legible: Decimal('10.00') -> '10', Decimal('12.50') -> '12.5' (normalize() solo daría '1E+1')."""
    return format(Decimal(valor).normalize(), "f")


def _contexto_a_medida(request, previas=None, errores=None):
    config = ConfiguracionSitio.get()
    return {
        "filamentos_json": [{"id": f.pk, "nombre": f.nombre, "costo_kg": str(f.costo_por_kg)}
                            for f in Filamento.objects.filter(activo=True)],
        "config_json": {"costo_hora": str(config.costo_hora_impresora), "fallas": _pct(config.tasa_fallas_pct),
                        "margen": _pct(config.margen_ganancia_pct),
                        "incompleta": config.costo_hora_impresora <= 0},
        "piezas_previas_json": previas or [],
        "errores": errores or [],
        "max_fotos": FotoItemPersonalizado.MAX_FOTOS,
        "max_piezas": servicios.MAX_PIEZAS_A_MEDIDA,
        "post": request.POST if request.method == "POST" else {},
        "hay_fotos_perdidas": bool(errores) and any(request.FILES.getlist(k) for k in request.FILES),
    }


@solo_admin
def cotizacion_medida(request):
    """Cotizar algo que no está en el catálogo: se escribe qué es (nombre, descripción, medidas, fotos),
    se calcula el costo con filamento + peso + tiempo y se decide el precio final."""
    if request.method == "POST":
        piezas, errores, previas = leer_piezas_a_medida(request.POST, request.FILES)
        validez = _entero(request.POST.get("validez_dias")) or 7
        if not piezas and not errores:
            errores = ["Agrega al menos una pieza."]
        if not errores:
            try:
                cot = servicios.crear_cotizacion_a_medida(
                    request.user, piezas, cliente=request.POST.get("cliente", ""),
                    observaciones=request.POST.get("observaciones", ""), validez_dias=max(1, validez),
                    descuento_extra=leer_decimal(request.POST.get("descuento_extra")),
                )
                messages.success(request, f"Cotización {cot.numero} lista. Ábrela en PDF para enviarla.")
                return redirect("panel:cotizacion_detalle", pk=cot.pk)
            except ErrorNegocio as e:
                errores = [str(e)]
        return render(request, "panel/cotizacion_medida.html", _contexto_a_medida(request, previas, errores))
    return render(request, "panel/cotizacion_medida.html", _contexto_a_medida(request))


@solo_admin
@require_POST
def cotizacion_medida_calcular(request):
    """Costo y precio sugerido de UNA pieza mientras se escribe (lo llama el formulario con fetch).
    Usa la misma fórmula que los productos del catálogo."""
    g = request.POST.get
    costeo, error = leer_costeo(g("peso"), g("horas"), g("minutos"), g("filamento"), g("otro_nombre"), g("otro_costo"))
    if costeo is None:
        # Sin nada escrito es normal (no hay que quejarse); con datos a medias se explica qué falta.
        return JsonResponse({"ok": False, "vacio": error is None, "mensaje": error or ""})
    d = servicios.costear_pieza(costeo["peso_gramos"], costeo["horas"], costeo["costo_por_kg"])
    return JsonResponse({
        "ok": True, "material": int(d["material"]), "maquina": int(d["maquina"]), "fallas": int(d["fallas"]),
        "costo": int(d["costo"]), "sugerido": int(d["precio_sugerido"]),
        "costo_hora": f"{d['costo_hora']:.2f}", "fallas_pct": _pct(d["tasa_fallas_pct"]),
        "margen_pct": _pct(d["margen_pct"]), "config_incompleta": d["config_incompleta"],
    })


# --------------------------------------------------------------------------
# Catálogo imprimible (PDF carta) con los productos y el orden que se están viendo
# --------------------------------------------------------------------------
@login_required
@require_POST
def catalogo_pdf_vista(request):
    """Recibe los ids de los productos visibles (ya filtrados y ordenados en el navegador) y el diseño
    elegido, y abre el PDF en otra pestaña. No incluye stock. Los productos ocultos del sitio público
    (disponible = No) no se imprimen."""
    ids = []
    for trozo in request.POST.get("ids", "").split(","):
        trozo = trozo.strip()
        if trozo.isdigit() and int(trozo) not in ids:
            ids.append(int(trozo))
    ids = ids[:catalogo_pdf.MAX_PRODUCTOS]
    diseno = request.POST.get("diseno", "")
    if diseno not in catalogo_pdf.DISENOS:
        diseno = catalogo_pdf.DISENO_POR_DEFECTO
    productos = {
        p.pk: p for p in Producto.objects.filter(pk__in=ids, disponible=True)
        .select_related("categoria").prefetch_related("imagenes")
    }
    lista = [productos[i] for i in ids if i in productos]
    if not lista:
        return HttpResponse(
            "<!doctype html><meta charset='utf-8'><title>Catálogo</title>"
            "<p style='font-family:sans-serif;padding:2rem'>No hay productos para imprimir con esos filtros. "
            "Cierra esta pestaña y cambia los filtros.</p>", status=400)
    pdf = catalogo_pdf.generar_pdf_catalogo(lista, diseno, request.POST.get("filtros", "").strip()[:300])
    respuesta = HttpResponse(pdf, content_type="application/pdf")
    respuesta["Content-Disposition"] = 'inline; filename="catalogo-misael-toys.pdf"'
    return respuesta


# --------------------------------------------------------------------------
# Usuarios (solo admin): usuario + contraseña, sin más
# --------------------------------------------------------------------------
def _leer_ubicaciones_post(request):
    """Ubicaciones activas marcadas en el formulario (ids inválidos se ignoran)."""
    ids = [int(i) for i in request.POST.getlist("ubicaciones") if i.isdigit()]
    return list(Ubicacion.objects.filter(activa=True, pk__in=ids))


@solo_admin
def usuarios(request):
    if request.method == "POST":
        nombre = request.POST.get("username", "").strip()
        clave = request.POST.get("password", "")
        elegidas = _leer_ubicaciones_post(request)
        if not nombre or not clave:
            messages.error(request, "Escribe un usuario y una contraseña.")
        elif len(nombre) > 150:
            messages.error(request, "El usuario puede tener hasta 150 caracteres.")
        elif User.objects.filter(username__iexact=nombre).exists():
            messages.error(request, f"Ya existe un usuario llamado «{nombre}».")
        elif not elegidas and Ubicacion.objects.filter(activa=True).exists():
            messages.error(request, "Elige al menos una ubicación donde venderá este usuario.")
        else:
            # create_user no valida el formato: se aceptan espacios, tildes, símbolos, etc.
            u = User.objects.create_user(username=nombre, password=clave)
            PerfilVendedor.objects.create(usuario=u).ubicaciones.set(elegidas)
            messages.success(request, f"Usuario «{nombre}» creado.")
        return redirect("panel:usuarios")
    lista = list(
        User.objects.annotate(n_ventas=Count("ventas", filter=Q(ventas__anulada=False)))
        .select_related("perfil_vendedor").prefetch_related("perfil_vendedor__ubicaciones")
        .order_by("-is_staff", "username")
    )
    for u in lista:
        perfil = getattr(u, "perfil_vendedor", None)
        u.ubicaciones_ids = {x.pk for x in perfil.ubicaciones.all()} if perfil else set()
    return render(request, "panel/usuarios.html", {
        "usuarios": lista, "ubicaciones": list(Ubicacion.objects.filter(activa=True)),
    })


@solo_admin
@require_POST
def usuario_ubicaciones(request, pk):
    u = get_object_or_404(User, pk=pk)
    elegidas = _leer_ubicaciones_post(request)
    if not elegidas and not u.is_staff and Ubicacion.objects.filter(activa=True).exists():
        messages.error(request, "Elige al menos una ubicación.")
    else:
        perfil, _ = PerfilVendedor.objects.get_or_create(usuario=u)
        perfil.ubicaciones.set(elegidas)
        messages.success(request, f"Ubicaciones de «{u.username}» actualizadas.")
    return redirect("panel:usuarios")


@solo_admin
@require_POST
def usuario_clave(request, pk):
    u = get_object_or_404(User, pk=pk)
    clave = request.POST.get("password", "")
    if not clave:
        messages.error(request, "Escribe la nueva contraseña.")
    else:
        u.set_password(clave)
        u.save(update_fields=["password"])
        messages.success(request, f"Contraseña de «{u.username}» cambiada.")
    return redirect("panel:usuarios")


@solo_admin
@require_POST
def usuario_activar(request, pk):
    u = get_object_or_404(User, pk=pk)
    if u == request.user:
        messages.error(request, "No puedes desactivar tu propio usuario.")
    else:
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        messages.success(request, f"«{u.username}» ahora está {'activo' if u.is_active else 'desactivado'}.")
    return redirect("panel:usuarios")
