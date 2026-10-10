import json
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from catalogo.models import Producto

from . import servicios
from .fotos import foto_panel
from .lineas import leer_decimal, leer_lineas
from .models import Cotizacion, MovimientoStock, Stock, Ubicacion, Venta, VentaItem
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
@login_required
def stock_lista(request):
    ubicaciones = list(Ubicacion.objects.filter(activa=True))
    q = request.GET.get("q", "").strip()
    donde = request.GET.get("donde", "")
    solo_con_stock = request.GET.get("con_stock") == "1"

    productos = Producto.objects.select_related("categoria").prefetch_related("stocks", "imagenes").order_by("nombre")
    if q:
        productos = productos.filter(nombre__icontains=q)

    filas = []
    ids_ub = [u.pk for u in ubicaciones]
    for p in productos:
        por_ub = {s.ubicacion_id: s.cantidad for s in p.stocks.all() if s.ubicacion_id in ids_ub}
        total = sum(por_ub.values())
        if solo_con_stock and total <= 0:
            continue
        if donde.isdigit() and por_ub.get(int(donde), 0) == 0:
            continue
        filas.append({
            "producto": p, "total": total, "foto": foto_panel(p),
            "lugares": [{"ubicacion": u, "cantidad": por_ub.get(u.pk, 0)} for u in ubicaciones],
        })
    return render(request, "panel/stock_lista.html", {
        "filas": filas, "ubicaciones": ubicaciones, "q": q, "donde": donde, "con_stock": solo_con_stock,
        "total_unidades": sum(max(f["total"], 0) for f in filas),
        # Unidades vendidas sin stock registrado (stock negativo), por cubrir
        "por_cubrir": sum(-l["cantidad"] for f in filas for l in f["lugares"] if l["cantidad"] < 0),
    })


@login_required
def stock_producto(request, pk):
    producto = get_object_or_404(Producto.objects.select_related("categoria"), pk=pk)
    ubicaciones = list(Ubicacion.objects.filter(activa=True))
    cantidades = {s.ubicacion_id: s.cantidad for s in Stock.objects.filter(producto=producto)}
    lugares = [{"ubicacion": u, "cantidad": cantidades.get(u.pk, 0)} for u in ubicaciones]
    ventas_recientes = (
        VentaItem.objects.filter(producto=producto, venta__anulada=False)
        .select_related("venta", "venta__vendedor", "ubicacion").order_by("-venta__creado")[:10]
    )
    movimientos = MovimientoStock.objects.filter(producto=producto).select_related("ubicacion", "usuario")[:25]
    return render(request, "panel/stock_producto.html", {
        "producto": producto, "lugares": lugares, "ubicaciones": ubicaciones,
        "total": sum(l["cantidad"] for l in lugares),
        "por_cubrir": sum(-l["cantidad"] for l in lugares if l["cantidad"] < 0),
        "ventas_recientes": ventas_recientes, "movimientos": movimientos,
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
def _datos_productos():
    """Productos con su stock por lugar (todos los lugares activos, incluso con
    0 o negativo), para las tarjetas y el selector del formulario."""
    ubicaciones = list(Ubicacion.objects.filter(activa=True))
    ids = {u.pk for u in ubicaciones}
    productos = Producto.objects.prefetch_related("stocks", "imagenes").order_by("nombre")
    datos = []
    for p in productos:
        stock = {str(u.pk): 0 for u in ubicaciones}
        for s in p.stocks.all():
            if s.ubicacion_id in ids:
                stock[str(s.ubicacion_id)] = s.cantidad
        datos.append({"id": p.pk, "nombre": p.nombre,
                      "precio": str(p.precio.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                      "stock": stock, "total": sum(stock.values()), "foto": foto_panel(p)})
    return datos, ubicaciones


def _contexto_formulario(request, tipo, previas=None, errores=None):
    datos, ubicaciones = _datos_productos()
    return {
        "tipo": tipo,
        "productos_json": datos,
        "ubicaciones_json": [{"id": u.pk, "nombre": u.nombre} for u in ubicaciones],
        "filas_json": previas or [],
        "errores": errores or [],
        "post": request.POST if request.method == "POST" else {},
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
        lineas, errores, previas = leer_lineas(request.POST, con_ubicacion=True)
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
    qs = Venta.objects.select_related("vendedor").prefetch_related("items")
    if not request.user.is_staff:
        qs = qs.filter(vendedor=request.user)
    return qs


@login_required
def ventas_lista(request):
    qs = _ventas_visibles(request)
    vendedor = request.GET.get("vendedor", "")
    desde, hasta = request.GET.get("desde", ""), request.GET.get("hasta", "")
    if request.user.is_staff and vendedor.isdigit():
        qs = qs.filter(vendedor_id=int(vendedor))
    if desde:
        qs = qs.filter(creado__date__gte=desde)
    if hasta:
        qs = qs.filter(creado__date__lte=hasta)

    validas = qs.filter(anulada=False)
    resumen = validas.aggregate(total=Sum("total"), n=Count("id"))
    unidades = VentaItem.objects.filter(venta__in=validas).aggregate(u=Sum("cantidad"))["u"] or 0
    por_vendedor = []
    if request.user.is_staff:
        por_vendedor = list(
            validas.values("vendedor__username").annotate(total=Sum("total"), n=Count("id")).order_by("-total")
        )
    return render(request, "panel/ventas_lista.html", {
        "ventas": qs[:200], "resumen": resumen, "unidades": unidades, "por_vendedor": por_vendedor,
        "vendedores": User.objects.filter(ventas__isnull=False).distinct() if request.user.is_staff else [],
        "f": {"vendedor": vendedor, "desde": desde, "hasta": hasta},
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
    qs = Cotizacion.objects.select_related("creador").prefetch_related("items")
    return qs if request.user.is_staff else qs.filter(creador=request.user)


@login_required
def cotizaciones_lista(request):
    return render(request, "panel/cotizaciones_lista.html", {"cotizaciones": _cotizaciones_visibles(request)[:200]})


@login_required
def cotizacion_detalle(request, pk):
    cot = get_object_or_404(_cotizaciones_visibles(request), pk=pk)
    return render(request, "panel/cotizacion_detalle.html", {"cot": cot, "items": cot.items.select_related("producto")})


@login_required
def cotizacion_pdf(request, pk):
    cot = get_object_or_404(_cotizaciones_visibles(request), pk=pk)
    respuesta = HttpResponse(generar_pdf_cotizacion(cot), content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="cotizacion-{cot.numero}.pdf"'
    return respuesta


# --------------------------------------------------------------------------
# Usuarios (solo admin): usuario + contraseña, sin más
# --------------------------------------------------------------------------
@solo_admin
def usuarios(request):
    if request.method == "POST":
        nombre = request.POST.get("username", "").strip()
        clave = request.POST.get("password", "")
        if not nombre or not clave:
            messages.error(request, "Escribe un usuario y una contraseña.")
        elif len(nombre) > 150:
            messages.error(request, "El usuario puede tener hasta 150 caracteres.")
        elif User.objects.filter(username__iexact=nombre).exists():
            messages.error(request, f"Ya existe un usuario llamado «{nombre}».")
        else:
            # create_user no valida el formato: se aceptan espacios, tildes, símbolos, etc.
            User.objects.create_user(username=nombre, password=clave)
            messages.success(request, f"Usuario «{nombre}» creado.")
        return redirect("panel:usuarios")
    lista = User.objects.annotate(n_ventas=Count("ventas", filter=Q(ventas__anulada=False))).order_by("-is_staff", "username")
    return render(request, "panel/usuarios.html", {"usuarios": lista})


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
