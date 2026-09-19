import secrets
from itertools import groupby
from operator import attrgetter
from urllib.parse import quote

from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST

from .models import Categoria, Atributo, Producto, ConfiguracionSitio, LikeProducto

COOKIE_VISITANTE = "mt_visitante"
DOS_ANIOS_EN_SEGUNDOS = 60 * 60 * 24 * 365 * 2


def _obtener_visitante_id(request):
    """Devuelve el id anónimo del visitante (guardado en cookie) y si es nuevo
    (para setearlo en la respuesta si todavía no existía)."""
    visitante_id = request.COOKIES.get(COOKIE_VISITANTE)
    es_nuevo = not visitante_id
    if es_nuevo:
        visitante_id = secrets.token_hex(16)
    return visitante_id, es_nuevo


def _setear_cookie_visitante_si_falta(response, visitante_id, es_nuevo):
    if es_nuevo:
        response.set_cookie(
            COOKIE_VISITANTE, visitante_id,
            max_age=DOS_ANIOS_EN_SEGUNDOS, httponly=True, samesite="Lax",
        )
    return response


def _productos_filtrados(request):
    productos = Producto.objects.filter(disponible=True).select_related("categoria").prefetch_related(
        "imagenes", "atributos__atributo"
    )

    # Búsqueda por texto
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        productos = productos.filter(Q(nombre__icontains=q) | Q(descripcion__icontains=q))

    # Categoría
    categoria_slug = request.GET.get("categoria", "").strip()
    if categoria_slug:
        productos = productos.filter(categoria__slug=categoria_slug)

    # Rango de precio
    precio_min = request.GET.get("precio_min", "").strip()
    precio_max = request.GET.get("precio_max", "").strip()
    if precio_min:
        try:
            productos = productos.filter(precio__gte=float(precio_min))
        except ValueError:
            pass
    if precio_max:
        try:
            productos = productos.filter(precio__lte=float(precio_max))
        except ValueError:
            pass

    # Valores de atributo seleccionados (checkboxes), ej: ?valor=3&valor=7
    valores_ids = [v for v in request.GET.getlist("valor") if v.isdigit()]
    if valores_ids:
        from .models import ValorAtributo
        seleccionados = ValorAtributo.objects.filter(id__in=valores_ids).select_related("atributo")
        # Agrupar por atributo: dentro del mismo atributo es OR, entre atributos distintos es AND
        seleccionados_ordenados = sorted(seleccionados, key=lambda v: v.atributo_id)
        for atributo_id, grupo in groupby(seleccionados_ordenados, key=attrgetter("atributo_id")):
            ids_del_grupo = [v.id for v in grupo]
            productos = productos.filter(atributos__id__in=ids_del_grupo)

    return productos.distinct()


def _contexto_filtros(request):
    # Solo mostramos categorías y valores de atributo que efectivamente tienen
    # al menos un producto disponible: evita "filtros fantasma" que llevan a
    # una lista vacía.
    categorias = (
        Categoria.objects.filter(activa=True, productos__disponible=True)
        .distinct()
        .order_by("orden", "nombre")
    )
    atributos = (
        Atributo.objects.filter(mostrar_como_filtro=True, valores__productos__disponible=True)
        .distinct()
        .prefetch_related("valores")
        .order_by("orden", "nombre")
    )
    valores_seleccionados = set(request.GET.getlist("valor"))
    return {
        "categorias": categorias,
        "atributos": atributos,
        "valores_seleccionados": valores_seleccionados,
        "categoria_actual": request.GET.get("categoria", ""),
        "q_actual": request.GET.get("q", ""),
        "precio_min_actual": request.GET.get("precio_min", ""),
        "precio_max_actual": request.GET.get("precio_max", ""),
    }


# Ya no es un límite de productos a mostrar: es el tamaño de cada "tanda" que
# se trae por scroll infinito. El catálogo completo sigue siendo navegable,
# simplemente se va cargando de a PRODUCTOS_POR_PAGINA en PRODUCTOS_POR_PAGINA
# a medida que el usuario baja en la página (ver hx-trigger="revealed" en
# _grid_productos.html).
PRODUCTOS_POR_PAGINA = 12


def home(request):
    """Página principal: el catálogo completo se ve directamente aquí, con filtros."""
    from django.core.paginator import Paginator

    productos_qs = _productos_filtrados(request)
    paginator = Paginator(productos_qs, PRODUCTOS_POR_PAGINA)
    numero_pagina = request.GET.get("page", 1)
    pagina = paginator.get_page(numero_pagina)

    # Querystring actual sin 'page', para armar los links de paginación
    params = request.GET.copy()
    params.pop("page", None)
    querystring_sin_pagina = params.urlencode()

    contexto = {
        "productos": pagina,
        "total_resultados": paginator.count,
        "querystring_sin_pagina": querystring_sin_pagina,
        "config": ConfiguracionSitio.get(),
        **_contexto_filtros(request),
    }

    # Si la petición viene de HTMX (el usuario tocó un filtro), devolvemos
    # solo el bloque del grid de productos, no la página completa.
    if request.headers.get("HX-Request"):
        return render(request, "catalogo/_grid_productos.html", contexto)

    return render(request, "catalogo/home.html", contexto)


def producto_detalle(request, slug):
    producto = get_object_or_404(
        Producto.objects.select_related("categoria").prefetch_related("imagenes", "atributos__atributo"),
        slug=slug, disponible=True,
    )
    config = ConfiguracionSitio.get()

    whatsapp_url = None
    if config.whatsapp_numero:
        mensaje = config.mensaje_whatsapp_base.format(producto=producto.nombre)
        whatsapp_url = f"https://wa.me/{config.whatsapp_numero}?text={quote(mensaje)}"

    relacionados = []
    if producto.categoria:
        relacionados = (
            Producto.objects.filter(categoria=producto.categoria, disponible=True)
            .exclude(pk=producto.pk)
            .prefetch_related("imagenes")[:4]
        )

    visitante_id, es_nuevo = _obtener_visitante_id(request)
    ya_dio_like = LikeProducto.objects.filter(producto=producto, visitante_id=visitante_id).exists()

    contexto = {
        "producto": producto,
        "config": config,
        "whatsapp_url": whatsapp_url,
        "relacionados": relacionados,
        "ya_dio_like": ya_dio_like,
        "url_absoluta": request.build_absolute_uri(),
        "imagen_absoluta": (
            request.build_absolute_uri(producto.imagen_principal)
            if producto.imagen_principal else None
        ),
    }
    response = render(request, "catalogo/producto_detalle.html", contexto)
    return _setear_cookie_visitante_si_falta(response, visitante_id, es_nuevo)


@require_POST
def toggle_like(request, slug):
    """Suma o quita un 'me gusta' del producto. Identifica al visitante por una
    cookie anónima (sin necesidad de cuentas de cliente) para no contar el
    mismo voto dos veces desde el mismo navegador."""
    producto = get_object_or_404(Producto, slug=slug, disponible=True)
    visitante_id, es_nuevo = _obtener_visitante_id(request)

    like_existente = LikeProducto.objects.filter(producto=producto, visitante_id=visitante_id).first()
    if like_existente:
        like_existente.delete()
        producto.total_likes = max(0, producto.total_likes - 1)
        ya_dio_like = False
    else:
        LikeProducto.objects.create(producto=producto, visitante_id=visitante_id)
        producto.total_likes = producto.total_likes + 1
        ya_dio_like = True
    producto.save(update_fields=["total_likes"])

    contexto = {"producto": producto, "ya_dio_like": ya_dio_like}
    response = render(request, "catalogo/_boton_like.html", contexto)
    return _setear_cookie_visitante_si_falta(response, visitante_id, es_nuevo)
