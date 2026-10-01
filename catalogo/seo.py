"""Todo lo relacionado con SEO: URLs absolutas, títulos, descripciones,
canonical, robots meta y datos estructurados (JSON-LD)."""
import json
from decimal import Decimal

from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils.text import Truncator


def abs_url(path):
    """Convierte una ruta en URL absoluta usando SITE_URL (siempre https)."""
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return settings.SITE_URL.rstrip("/") + (path if path.startswith("/") else "/" + path)


def _json(data):
    # "<" escapado para que nada dentro de un texto pueda cerrar el <script>.
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _limpio(texto, largo):
    return Truncator(" ".join((texto or "").split())).chars(largo)


def _precio(valor):
    return format(Decimal(valor).quantize(Decimal("0.01")).normalize(), "f")


def url_categoria(categoria):
    return abs_url(reverse("catalogo:categoria_detalle", kwargs={"slug": categoria.slug}))


def _tienda(config):
    return {
        "@type": "OnlineStore",
        "@id": abs_url("/") + "#tienda",
        "name": config.nombre_tienda or "Misael Toys",
        "url": abs_url("/"),
        "logo": abs_url(static("img/logo.png")),
        "image": abs_url(static("img/logo.png")),
        "description": "Juguetes sensoriales y antiestrés impresos en 3D: frutas, verduras, "
                       "animales articulados y más.",
        "areaServed": {"@type": "Country", "name": "Bolivia"},
        "parentOrganization": {
            "@type": "Organization",
            "name": "Centro de Neurodesarrollo Infantil MISAEL",
            "url": "https://neuromisael.com/",
        },
        **({"telephone": config.telefono} if config.telefono else {}),
    }


def _breadcrumb(items):
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": n, "item": u}
            for i, (n, u) in enumerate(items, 1)
        ],
    }


def seo_catalogo(params, config, productos, pagina_num, categoria=None):
    """SEO del home y de las páginas de categoría.
    `params` = querystring ORIGINAL (antes de fijar la categoría de la página)."""
    tienda = config.nombre_tienda or "Misael Toys"
    home = abs_url(reverse("catalogo:home"))

    otros_filtros = bool(
        params.get("q") or params.get("precio_min") or params.get("precio_max")
        or params.getlist("valor")
    )
    cat_param = params.get("categoria", "")
    base = url_categoria(categoria) if categoria else home

    robots = None
    if otros_filtros:
        # Combinaciones de filtros / búsquedas: no se indexan (contenido duplicado).
        robots, canonical = "noindex, follow", base
    elif cat_param and not categoria:
        from .models import Categoria
        c = Categoria.objects.filter(slug=cat_param, activa=True).first()
        canonical = url_categoria(c) if c else home
    else:
        canonical = base
        if pagina_num > 1:
            canonical = f"{base}?page={pagina_num}"

    if categoria:
        titulo = f"{categoria.nombre}: juguetes impresos en 3D | {tienda}"
        h1 = f"{categoria.nombre}: juguetes impresos en 3D"
        descripcion = _limpio(categoria.descripcion, 158) or (
            f"Juguetes de {categoria.nombre.lower()} impresos en 3D en Bolivia, "
            f"pensados para el desarrollo infantil. Consulta por WhatsApp."
        )
    else:
        titulo = f"Juguetes Sensoriales Antiestrés Impresos en 3D | {tienda}"
        h1 = "Juguetes sensoriales antiestrés impresos en 3D"
        descripcion = (
            "Juguetes sensoriales y antiestrés impresos en 3D: frutas, animales articulados y "
            "más. Creados con el apoyo de terapeutas en Potosí, Bolivia."
        )
    if pagina_num > 1:
        titulo = f"{titulo} (página {pagina_num})"

    nodos = [_tienda(config)]
    if categoria:
        nodos.append({
            "@type": "CollectionPage",
            "name": h1,
            "url": base,
            "isPartOf": {"@id": abs_url("/") + "#tienda"},
            "mainEntity": {
                "@type": "ItemList",
                "itemListElement": [
                    {"@type": "ListItem", "position": i, "url": abs_url(p.get_absolute_url()), "name": p.nombre}
                    for i, p in enumerate(list(productos)[:12], 1)
                ],
            },
        })
        nodos.append(_breadcrumb([("Inicio", home), (categoria.nombre, base)]))

    return {
        "title": titulo,
        "description": descripcion,
        "h1": h1,
        "robots": robots,
        "canonical": canonical,
        "og_type": "website",
        "image": abs_url(static("img/logo.png")),
        "schema_json": _json({"@context": "https://schema.org", "@graph": nodos}),
    }


def seo_producto(producto, config):
    tienda = config.nombre_tienda or "Misael Toys"
    url = abs_url(producto.get_absolute_url())
    cat = producto.categoria
    imagenes = [abs_url(i.imagen.url) for i in producto.imagenes.all()]

    descripcion = _limpio(producto.descripcion_corta or producto.descripcion, 158) or (
        f"{producto.nombre}: juguete sensorial impreso en 3D. "
        f"Consulta disponibilidad por WhatsApp."
    )
    titulo = f"{producto.nombre}{' · ' + cat.nombre if cat else ''} | {tienda}"

    prod = {
        "@type": "Product",
        "name": producto.nombre,
        "description": descripcion,
        "sku": producto.slug,
        "url": url,
        "brand": {"@type": "Brand", "name": tienda},
        "offers": {
            "@type": "Offer",
            "url": url,
            "priceCurrency": "BOB",
            "price": _precio(producto.precio),
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
            "seller": {"@type": "Organization", "name": tienda},
        },
    }
    if imagenes:
        prod["image"] = imagenes
    if cat:
        prod["category"] = cat.nombre
    props = [
        {"@type": "PropertyValue", "name": v.atributo.nombre, "value": v.valor}
        for v in producto.atributos.all()
    ]
    if props:
        prod["additionalProperty"] = props

    migas = [("Inicio", abs_url("/"))]
    if cat:
        migas.append((cat.nombre, url_categoria(cat)))
    migas.append((producto.nombre, url))

    return {
        "title": titulo,
        "description": descripcion,
        "robots": None,
        "canonical": url,
        "og_type": "product",
        "og_title": f"{producto.nombre} — Bs. {_precio(producto.precio)}",
        "image": imagenes[0] if imagenes else abs_url(static("img/logo.png")),
        "schema_json": _json({"@context": "https://schema.org", "@graph": [prod, _breadcrumb(migas)]}),
    }
