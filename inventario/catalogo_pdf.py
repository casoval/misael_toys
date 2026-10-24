"""Catálogo imprimible en PDF (hoja carta) para el panel.

Se genera con los productos que el usuario está viendo (con sus filtros y su orden) y
muestra lo mismo que la tarjeta del catálogo público: foto, nombre, precio, descripción
corta, código, categoría, medidas y el sello «Recomendado». Nunca incluye stock ni lugares:
es un documento para el cliente.

Hay varios diseños (ver DISENOS), de 4 a 18 productos por hoja.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas

from PIL import Image as PILImage, ImageOps

from catalogo.models import ConfiguracionSitio, _url_cloudinary_transformada

from .pdf import CLAY, PIE_TITULO, SOFT, TEAL, _bs, _descargar

log = logging.getLogger(__name__)

ANCHO, ALTO = letter                      # 215,9 × 279,4 mm
MARGEN_X = 10 * mm
MARGEN_SUP = 8 * mm
ALTO_CABECERA = 15 * mm
ALTO_PIE = 15 * mm                         # de aquí hacia abajo va el pie de página
MAX_PRODUCTOS = 400                        # tope por catálogo (para que el PDF no tarde ni pese de más)
PX_POR_MM = 7.9                            # ~200 dpi: nítido al imprimir sin hacer pesado el PDF
PX_MAX = 800

GRIS_CLARO = colors.HexColor("#F1EEE6")
GRIS_TEXTO = colors.HexColor("#8F8778")
TINTA = colors.HexColor("#2B2A27")


@dataclass(frozen=True)
class Diseno:
    clave: str            # también es la cantidad de productos por hoja
    nombre: str
    columnas: int
    filas: int
    separacion: float     # mm entre tarjetas
    foto_alto: float = 0  # mm (solo tarjetas verticales; 0 = cuadrada, igual al ancho)
    lista: bool = False   # True = fila horizontal (foto a la izquierda, texto al centro, precio a la derecha)
    # Tamaños de letra (pt) y líneas máximas
    t_nombre: float = 10
    n_nombre: int = 2
    t_desc: float = 8
    n_desc: int = 3
    t_precio: float = 12
    t_codigo: float = 7.5
    t_medidas: float = 7
    categoria: bool = True
    medidas: bool = True

    @property
    def por_hoja(self):
        return self.columnas * self.filas


DISENOS = {d.clave: d for d in (
    Diseno("4", "Grande", 2, 2, 5, foto_alto=62, t_nombre=13, n_nombre=2, t_desc=9.5, n_desc=4,
           t_precio=17, t_codigo=9, t_medidas=8.5),
    Diseno("6", "Mediano", 3, 2, 4, foto_alto=0, t_nombre=11, n_nombre=2, t_desc=8.5, n_desc=4,
           t_precio=14, t_codigo=8, t_medidas=8),
    Diseno("9", "Estándar", 3, 3, 4, foto_alto=44, t_nombre=9.5, n_nombre=2, t_desc=7.5, n_desc=2,
           t_precio=12, t_codigo=7.5, t_medidas=7),
    Diseno("12", "Compacto", 4, 3, 3.5, foto_alto=0, t_nombre=8.5, n_nombre=2, t_desc=7, n_desc=2,
           t_precio=11, t_codigo=7, t_medidas=6.5, medidas=False),
    Diseno("16", "Mini", 4, 4, 3, foto_alto=34, t_nombre=8, n_nombre=2, t_desc=6.5, n_desc=2,
           t_precio=10.5, t_codigo=6.5, categoria=False, medidas=False),
    Diseno("18", "Lista con foto", 2, 9, 3, lista=True, t_nombre=9.5, n_nombre=2, t_desc=7.5, n_desc=2,
           t_precio=12, t_codigo=7, t_medidas=7),
)}
DISENO_POR_DEFECTO = "9"


def opciones():
    """Para el selector del panel: [(clave, texto)]."""
    detalle = {"4": "2×2", "6": "3×2", "9": "3×3", "12": "4×3", "16": "4×4", "18": "2 columnas × 9"}
    return [(d.clave, f"{d.clave} por hoja · {d.nombre} ({detalle[d.clave]})") for d in DISENOS.values()]


# --------------------------------------------------------------------------
# Texto
# --------------------------------------------------------------------------
def _t(texto):
    """Limpia el texto para las fuentes estándar del PDF (Helvetica = Latin-1/Windows):
    sin saltos de línea ni caracteres raros (los que no existen salen como «?»)."""
    texto = re.sub(r"\s+", " ", str(texto or "")).strip()
    return texto.encode("cp1252", "replace").decode("cp1252")


def _ajustar(texto, fuente, tam, ancho, max_lineas):
    """Parte el texto en líneas del ancho dado; si sobra, la última termina en «…»."""
    texto = _t(texto)
    if not texto or max_lineas < 1:
        return []
    lineas = simpleSplit(texto, fuente, tam, ancho)
    if len(lineas) <= max_lineas:
        return lineas
    lineas = lineas[:max_lineas]
    ultima = lineas[-1].rstrip()
    while ultima and stringWidth(ultima + "…", fuente, tam) > ancho:
        ultima = ultima[:-1]
    lineas[-1] = ultima.rstrip(" .,;:") + "…"
    return lineas


def _escribir(c, lineas, x, y_tope, fuente, tam, interlineado, color):
    """Escribe las líneas hacia abajo desde y_tope; devuelve la y donde termina el bloque."""
    c.setFillColor(color)
    c.setFont(fuente, tam)
    y = y_tope
    for linea in lineas:
        c.drawString(x, y - tam, linea)
        y -= interlineado
    return y


# --------------------------------------------------------------------------
# Fotos
# --------------------------------------------------------------------------
def _origen_foto(producto, ancho_px, alto_px):
    """('url', ...) si está en la nube (se pide ya recortada al tamaño justo), ('bytes', ...) si es un
    archivo local, o None si no tiene foto. Se llama en el hilo principal (usa la BD)."""
    imagenes = list(producto.imagenes.all())
    if not imagenes:
        return None
    campo = imagenes[0].imagen
    try:
        url = campo.url
        if url.startswith("http"):
            return ("url", _url_cloudinary_transformada(url, f"w_{ancho_px},h_{alto_px},c_fill,q_auto,f_jpg"))
        with campo.open("rb") as archivo:
            return ("bytes", archivo.read())
    except Exception:
        log.warning("No se pudo leer la foto del producto %s", producto.pk, exc_info=True)
        return None


def _preparar_foto(origen, ancho_px, alto_px):
    try:
        datos = _descargar(origen[1]) if origen[0] == "url" else origen[1]
        img = ImageOps.exif_transpose(PILImage.open(BytesIO(datos))).convert("RGB")
        img = ImageOps.fit(img, (ancho_px, alto_px), method=PILImage.LANCZOS)
        salida = BytesIO()
        img.save(salida, "JPEG", quality=84, optimize=True)
        return salida.getvalue()
    except Exception:
        log.warning("No se pudo preparar una foto para el catálogo", exc_info=True)
        return None  # sin foto no se rompe nada: ese producto sale con el recuadro «Sin foto»


def _fotos(productos, ancho_mm, alto_mm):
    """{id de producto: bytes JPEG}, con las descargas en paralelo."""
    ancho_px = min(PX_MAX, max(120, round(ancho_mm * PX_POR_MM)))
    alto_px = min(PX_MAX, max(120, round(alto_mm * PX_POR_MM)))
    origenes = {p.pk: _origen_foto(p, ancho_px, alto_px) for p in productos}
    pendientes = {pk: o for pk, o in origenes.items() if o}
    if not pendientes:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(pendientes))) as pool:
        resultados = list(pool.map(lambda o: _preparar_foto(o, ancho_px, alto_px), pendientes.values()))
    return {pk: foto for pk, foto in zip(pendientes, resultados) if foto}


# --------------------------------------------------------------------------
# Dibujo de una tarjeta
# --------------------------------------------------------------------------
def _recuadro_foto(c, x, y, w, h, foto):
    """Dibuja la foto (o el recuadro «Sin foto») en el rectángulo dado."""
    if foto:
        c.drawImage(ImageReader(BytesIO(foto)), x, y, w, h)
    else:
        c.setFillColor(GRIS_CLARO)
        c.rect(x, y, w, h, stroke=0, fill=1)
        c.setFillColor(GRIS_TEXTO)
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(x + w / 2, y + h / 2 - 2.5, "Sin foto")


def _chip(c, x, y_tope, texto, color, tam):
    """Etiqueta redondeada con el color de la categoría. Devuelve su alto."""
    texto = _t(texto)
    c.setFont("Helvetica-Bold", tam)
    alto = tam + 3.2
    ancho = stringWidth(texto, "Helvetica-Bold", tam) + 7
    c.setFillColor(color)
    c.roundRect(x, y_tope - alto, ancho, alto, alto / 2, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.drawString(x + 3.5, y_tope - alto + 1.8 + tam * 0.12, texto)
    return alto


def _sello_recomendado(c, x, y_tope, tam):
    texto = "Recomendado"
    c.setFont("Helvetica-Bold", tam)
    alto = tam + 3.6
    ancho = stringWidth(texto, "Helvetica-Bold", tam) + 8
    c.setFillColor(colors.white)
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.6)
    c.roundRect(x, y_tope - alto, ancho, alto, alto / 2, stroke=1, fill=1)
    c.setFillColor(TEAL)
    c.drawString(x + 4, y_tope - alto + 2 + tam * 0.1, texto)


def _tarjeta_vertical(c, d, x, y, w, h, it, foto):
    """Tarjeta como la del catálogo web: foto arriba y los datos debajo. (x, y) = esquina inferior izquierda."""
    radio = min(3 * mm, w * 0.06)
    foto_h = (d.foto_alto * mm) if d.foto_alto else w
    c.setFillColor(colors.white)
    c.setStrokeColor(CLAY)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, radio, stroke=0, fill=1)

    c.saveState()  # la foto se recorta con las esquinas redondeadas de la tarjeta
    ruta = c.beginPath()
    ruta.roundRect(x, y, w, h, radio)
    c.clipPath(ruta, stroke=0, fill=0)
    _recuadro_foto(c, x, y + h - foto_h, w, foto_h, foto)
    c.restoreState()
    if it["recomendado"]:
        _sello_recomendado(c, x + 2 * mm, y + h - 2 * mm, max(5.8, d.t_codigo - 0.7))
    c.setStrokeColor(CLAY)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, radio, stroke=1, fill=0)

    # --- Datos ---
    pad = max(2.2 * mm, w * 0.04)
    ancho_txt = w - 2 * pad
    cursor = y + h - foto_h - pad * 0.85
    base = y + pad * 0.8                                  # el precio va pegado abajo
    reserva_precio = d.t_precio * 1.25 + 1
    if d.categoria and it["categoria"]:
        cursor -= _chip(c, x + pad, cursor, it["categoria"], it["color"], max(5.8, d.t_codigo - 0.5)) + 2
    inter_n, inter_d = d.t_nombre * 1.18, d.t_desc * 1.22
    lineas_n = _ajustar(it["nombre"], "Helvetica-Bold", d.t_nombre, ancho_txt, d.n_nombre)
    cursor = _escribir(c, lineas_n, x + pad, cursor, "Helvetica-Bold", d.t_nombre, inter_n, TINTA) - 1.5

    medidas = it["medidas"] if d.medidas else ""
    inter_m = d.t_medidas * 1.25
    libre = cursor - (base + reserva_precio) - (inter_m if medidas else 0)
    n_desc = min(d.n_desc, int(libre // inter_d))
    lineas_d = _ajustar(it["descripcion"], "Helvetica", d.t_desc, ancho_txt, n_desc)
    cursor = _escribir(c, lineas_d, x + pad, cursor, "Helvetica", d.t_desc, inter_d, colors.HexColor("#5A554B"))
    if medidas and cursor - inter_m >= base + reserva_precio - 1:
        _escribir(c, [f"Medidas: {_t(medidas)}"], x + pad, cursor - 0.8, "Helvetica", d.t_medidas, inter_m, SOFT)

    # Precio a la izquierda y código a la derecha, como en la tarjeta web
    c.setFillColor(TEAL)
    c.setFont("Helvetica-Bold", d.t_precio)
    c.drawString(x + pad, base, it["precio"])
    if it["codigo"]:
        c.setFillColor(GRIS_TEXTO)
        c.setFont("Helvetica", d.t_codigo)
        c.drawRightString(x + w - pad, base, it["codigo"])


def _tarjeta_lista(c, d, x, y, w, h, it, foto):
    """Fila horizontal: foto a la izquierda, nombre y descripción al centro, precio y código a la derecha."""
    radio = 2.5 * mm
    c.setFillColor(colors.white)
    c.roundRect(x, y, w, h, radio, stroke=0, fill=1)
    pad = 2 * mm
    lado = h - 2 * pad
    c.saveState()
    ruta = c.beginPath()
    ruta.roundRect(x + pad, y + pad, lado, lado, 1.8 * mm)
    c.clipPath(ruta, stroke=0, fill=0)
    _recuadro_foto(c, x + pad, y + pad, lado, lado, foto)
    c.restoreState()
    # Franja con el color de la categoría, a la izquierda
    if it["categoria"]:
        c.saveState()
        ruta = c.beginPath()
        ruta.roundRect(x, y, w, h, radio)
        c.clipPath(ruta, stroke=0, fill=0)
        c.setFillColor(it["color"])
        c.rect(x, y, 1.3 * mm, h, stroke=0, fill=1)
        c.restoreState()
    c.setStrokeColor(CLAY)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, radio, stroke=1, fill=0)

    ancho_der = max(stringWidth(it["precio"], "Helvetica-Bold", d.t_precio),
                    stringWidth(it["codigo"], "Helvetica", d.t_codigo)) + 1
    x_txt = x + pad + lado + 3 * mm
    ancho_txt = (x + w - pad) - ancho_der - 2.5 * mm - x_txt
    inter_n, inter_d = d.t_nombre * 1.15, d.t_desc * 1.2
    cursor = y + h - pad - 0.5
    lineas_n = _ajustar(it["nombre"], "Helvetica-Bold", d.t_nombre, ancho_txt, d.n_nombre)
    cursor = _escribir(c, lineas_n, x_txt, cursor, "Helvetica-Bold", d.t_nombre, inter_n, TINTA) - 1
    medidas = f"Medidas: {_t(it['medidas'])}" if (d.medidas and it["medidas"]) else ""
    inter_m = d.t_medidas * 1.2
    libre = cursor - (y + pad) - (inter_m if medidas else 0)
    n_desc = max(0, min(d.n_desc, int(libre // inter_d)))
    lineas_d = _ajustar(it["descripcion"], "Helvetica", d.t_desc, ancho_txt, n_desc)
    cursor = _escribir(c, lineas_d, x_txt, cursor, "Helvetica", d.t_desc, inter_d, colors.HexColor("#5A554B"))
    if medidas and cursor - inter_m >= y + pad - 1:
        _escribir(c, [medidas], x_txt, cursor - 0.5, "Helvetica", d.t_medidas, inter_m, SOFT)

    # Precio (grande) y código (chico) a la derecha, centrados en vertical
    xd = x + w - pad
    c.setFillColor(TEAL)
    c.setFont("Helvetica-Bold", d.t_precio)
    c.drawRightString(xd, y + h / 2 + 1, it["precio"])
    if it["codigo"]:
        c.setFillColor(GRIS_TEXTO)
        c.setFont("Helvetica", d.t_codigo)
        c.drawRightString(xd, y + h / 2 - d.t_codigo - 2, it["codigo"])


# --------------------------------------------------------------------------
# Cabecera y pie
# --------------------------------------------------------------------------
def _cabecera(c, config, n_productos, filtros_texto, logo):
    y_sup = ALTO - MARGEN_SUP
    if logo:
        lw, lh = logo.getSize()
        alto = 11.5 * mm
        c.drawImage(logo, MARGEN_X, y_sup - alto - 1 * mm, alto * lw / lh, alto, mask="auto")
    else:
        c.setFillColor(TEAL)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(MARGEN_X, y_sup - 9 * mm, _t(config.nombre_tienda))
    xd = ANCHO - MARGEN_X
    c.setFillColor(TEAL)
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(xd, y_sup - 5.2 * mm, "Catálogo de productos")
    c.setFillColor(SOFT)
    c.setFont("Helvetica", 8)
    c.drawRightString(xd, y_sup - 9.4 * mm,
                      f"{date.today():%d/%m/%Y}  ·  {n_productos} producto{'' if n_productos == 1 else 's'}")
    if filtros_texto:
        ancho_max = 130 * mm
        texto = _ajustar("Filtros: " + filtros_texto, "Helvetica", 7.5, ancho_max, 1)
        c.setFont("Helvetica", 7.5)
        c.drawRightString(xd, y_sup - 13.2 * mm, texto[0])
    y_linea = ALTO - MARGEN_SUP - ALTO_CABECERA + 1 * mm
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.9)
    c.line(MARGEN_X, y_linea, ANCHO - MARGEN_X, y_linea)


def _pie(c, config, pagina, total):
    c.setStrokeColor(CLAY)
    c.setLineWidth(0.8)
    c.line(MARGEN_X, 11.5 * mm, ANCHO - MARGEN_X, 11.5 * mm)
    c.setFillColor(TEAL)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGEN_X, 7.6 * mm, PIE_TITULO)
    c.setFillColor(SOFT)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(ANCHO - MARGEN_X, 7.6 * mm, f"Página {pagina} de {total}")
    partes = []
    if config.contactos:
        partes.append("Tel: " + "  ·  ".join(_t(n) for n in config.contactos))
    if config.email_contacto:
        partes.append(_t(config.email_contacto))
    sitio = re.sub(r"^https?://", "", settings.SITE_URL or "").rstrip("/")
    if sitio:
        partes.append(f"Tienda en línea: {sitio}")
    disponible = ANCHO - 2 * MARGEN_X
    while len(partes) > 1 and stringWidth("  ·  ".join(partes), "Helvetica", 7.5) > disponible:
        partes.pop()
    if partes:
        c.drawString(MARGEN_X, 3.8 * mm, "  ·  ".join(partes))


def _logo():
    ruta = Path(settings.BASE_DIR) / "static" / "img" / "logo.png"
    try:
        return ImageReader(str(ruta)) if ruta.exists() else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Documento
# --------------------------------------------------------------------------
def _datos(p):
    cat = p.categoria
    return {
        "nombre": p.nombre, "codigo": p.codigo or "", "precio": _bs(p.precio),
        "descripcion": p.descripcion_corta, "medidas": p.medidas_resumen,
        "categoria": cat.nombre if cat else "",
        "color": colors.HexColor(cat.color_fondo) if cat else TEAL,
        "recomendado": p.recomendado_por_terapeutas,
    }


def generar_pdf_catalogo(productos, diseno=DISENO_POR_DEFECTO, filtros_texto=""):
    """PDF carta con los productos dados, en el orden recibido.

    `productos`: lista de Producto (con select_related('categoria') y prefetch_related('imagenes')
    para no hacer consultas de más). Devuelve los bytes del PDF."""
    productos = list(productos)[:MAX_PRODUCTOS]
    if not productos:
        raise ValueError("No hay productos para el catálogo.")
    d = DISENOS.get(diseno) or DISENOS[DISENO_POR_DEFECTO]
    config = ConfiguracionSitio.get()

    area_x, area_ancho = MARGEN_X, ANCHO - 2 * MARGEN_X
    area_sup = ALTO - MARGEN_SUP - ALTO_CABECERA - 3.5 * mm
    area_alto = area_sup - ALTO_PIE
    sep = d.separacion * mm
    w = (area_ancho - sep * (d.columnas - 1)) / d.columnas
    h = (area_alto - sep * (d.filas - 1)) / d.filas

    if d.lista:
        foto_w = foto_h = h - 4 * mm
    else:
        foto_w, foto_h = w, (d.foto_alto * mm if d.foto_alto else w)
    fotos = _fotos(productos, foto_w / mm, foto_h / mm)
    dibujar = _tarjeta_lista if d.lista else _tarjeta_vertical

    buf = BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Catálogo - {config.nombre_tienda}")
    c.setAuthor(config.nombre_tienda)
    logo = _logo()
    hojas = (len(productos) + d.por_hoja - 1) // d.por_hoja

    for n, p in enumerate(productos):
        pos = n % d.por_hoja
        if pos == 0:
            if n:
                c.showPage()
            _cabecera(c, config, len(productos), _t(filtros_texto), logo)
            _pie(c, config, n // d.por_hoja + 1, hojas)
        fila, col = divmod(pos, d.columnas)
        x = area_x + col * (w + sep)
        y = area_sup - (fila + 1) * h - fila * sep
        dibujar(c, d, x, y, w, h, _datos(p), fotos.get(p.pk))
    c.showPage()
    c.save()
    return buf.getvalue()
