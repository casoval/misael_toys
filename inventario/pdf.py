"""PDFs del panel: cotización (para el cliente) y recibo de venta (respaldo).

Importante: ninguno incluye stock ni ubicaciones, solo lo que un cliente puede ver.
Todo el dinero va en enteros (sin decimales)."""
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepInFrame, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from PIL import Image as PILImage, ImageDraw, ImageOps

from catalogo.models import ConfiguracionSitio, _url_cloudinary_transformada

log = logging.getLogger(__name__)

TEAL = colors.HexColor("#0B5C53")
CLAY = colors.HexColor("#E4DFD3")
SOFT = colors.HexColor("#6B6459")
ROJO = colors.HexColor("#C0392B")

# Textos del pie de página (aparece en todas las hojas). Edítalos aquí a gusto.
PIE_TITULO = "Juguetes sensoriales  ·  Potosí, Bolivia  ·  Hechos por terapeutas"
PIE_GRUPO = "Misael Toys pertenece a Grupo Misael  ·  Página principal: neuromisael.com"
PIE_FRASE = "Pensados para estimular los sentidos, calmar y aprender jugando."
PIE_GRACIAS = "¡Gracias por confiar en nosotros!"
LINEA_GRUPO = "Parte de Grupo Misael"  # bajo el nombre de la tienda, arriba a la derecha

MARGEN_LATERAL = 18 * mm
MARGEN_INFERIOR = 33 * mm  # deja lugar al pie (el pie ocupa unos 30 mm desde el borde)


def _bs(valor):
    """Dinero siempre entero: 'Bs. 1234'."""
    return f"Bs. {Decimal(valor).quantize(Decimal('1'), rounding=ROUND_HALF_UP):.0f}"


# --------------------------------------------------------------------------
# Fotos pequeñas de referencia (cotización)
# --------------------------------------------------------------------------
FOTO_MM = 11          # lado de la miniatura en el PDF
FOTO_PX = 180         # resolución con la que se incrusta (nítida pero liviana)


def _url_miniatura(url):
    """Si la foto está en Cloudinary, se pide ya recortada y chica (JPG: el PDF no lee WebP)."""
    return _url_cloudinary_transformada(url, f"w_{FOTO_PX},h_{FOTO_PX},c_fill,q_auto,f_jpg")


@lru_cache(maxsize=256)
def _descargar(url):
    """Descarga una foto (con límite de tamaño y de tiempo). Se recuerda para no repetirla."""
    peticion = urllib.request.Request(url, headers={"User-Agent": "MisaelToys-PDF/1.0"})
    with urllib.request.urlopen(peticion, timeout=5) as respuesta:
        datos = respuesta.read(3_000_000 + 1)
    if len(datos) > 3_000_000:
        raise ValueError("foto demasiado grande")
    return datos


def _a_miniatura(datos):
    """Foto cuadrada, centrada y con esquinas redondeadas, en JPEG liviano."""
    img = PILImage.open(BytesIO(datos))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (FOTO_PX, FOTO_PX), method=PILImage.LANCZOS)
    mascara = PILImage.new("L", (FOTO_PX, FOTO_PX), 0)
    ImageDraw.Draw(mascara).rounded_rectangle((0, 0, FOTO_PX - 1, FOTO_PX - 1), radius=FOTO_PX // 7, fill=255)
    fondo = PILImage.new("RGB", (FOTO_PX, FOTO_PX), "white")
    fondo.paste(img, (0, 0), mascara)
    salida = BytesIO()
    fondo.save(salida, "JPEG", quality=82, optimize=True)
    return salida.getvalue()


def _marcador_sin_foto():
    """Cuadrito claro con las mismas esquinas, para que las filas sin foto no queden más bajas."""
    img = PILImage.new("RGB", (FOTO_PX, FOTO_PX), "white")
    ImageDraw.Draw(img).rounded_rectangle((0, 0, FOTO_PX - 1, FOTO_PX - 1), radius=FOTO_PX // 7, fill="#F1EEE6")
    salida = BytesIO()
    img.save(salida, "JPEG", quality=80)
    return salida.getvalue()


def _origen_foto(producto):
    """De dónde sale la foto principal: ('url', ...) si está en la nube, ('bytes', ...) si es un
    archivo local, o None si el producto no tiene foto. Se llama en el hilo principal (usa la BD)."""
    imagenes = list(producto.imagenes.all())
    if not imagenes:
        return None
    campo = imagenes[0].imagen
    try:
        url = campo.url
        if url.startswith("http"):
            return ("url", _url_miniatura(url))
        with campo.open("rb") as archivo:
            return ("bytes", archivo.read())
    except Exception:
        log.warning("No se pudo leer la foto del producto %s", producto.pk, exc_info=True)
        return None


def _miniatura_de(origen):
    try:
        datos = _descargar(origen[1]) if origen[0] == "url" else origen[1]
        return _a_miniatura(datos)
    except Exception:
        log.warning("No se pudo preparar una foto para el PDF", exc_info=True)
        return None  # sin foto no se rompe nada: la celda queda vacía


def miniaturas_de(productos):
    """{id de producto: bytes JPEG}. Las descargas van en paralelo; si una falla, ese producto
    simplemente sale sin foto."""
    origenes = {}
    for p in productos:
        if p.pk not in origenes:
            origenes[p.pk] = _origen_foto(p)
    pendientes = {pk: o for pk, o in origenes.items() if o}
    if not pendientes:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(pendientes))) as pool:
        resultados = list(pool.map(_miniatura_de, pendientes.values()))
    return {pk: foto for pk, foto in zip(pendientes, resultados) if foto}


def _esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _estilos():
    base = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=base, fontName="Helvetica", fontSize=10, leading=14)
    return {
        "normal": normal,
        "chico": ParagraphStyle("c", parent=normal, fontSize=8.5, textColor=SOFT, leading=12),
        "titulo": ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=20, textColor=TEAL, leading=24),
        "negrita": ParagraphStyle("b", parent=normal, fontName="Helvetica-Bold"),
        "der": ParagraphStyle("d", parent=normal, alignment=2),
        "gracias": ParagraphStyle("g", parent=normal, fontName="Helvetica-Bold", textColor=TEAL),
    }


def lineas_contacto(config):
    """Líneas del bloque de contacto (arriba a la derecha). Solo teléfonos de
    contacto: los WhatsApp se usan en los botones del sitio, no en los documentos."""
    lineas = [f"<b>{_esc(config.nombre_tienda)}</b>", f"<font color='#0B5C53'>{LINEA_GRUPO}</font>"]
    if config.contactos:
        lineas.append("Tel: " + "  ·  ".join(_esc(n) for n in config.contactos))
    if config.email_contacto:
        lineas.append(_esc(config.email_contacto))
    return lineas


def _cabecera(config, st, logo_alto_mm=36, logo_ancho_mm=54):
    """Logo (grande) a la izquierda; nombre, grupo y contacto a la derecha."""
    contacto = lineas_contacto(config)

    logo_path = Path(settings.BASE_DIR) / "static" / "img" / "logo.png"
    if logo_path.exists():
        img = Image(str(logo_path))
        ratio = img.imageHeight / float(img.imageWidth)
        ancho, alto = logo_ancho_mm * mm, logo_ancho_mm * mm * ratio
        if alto > logo_alto_mm * mm:
            alto, ancho = logo_alto_mm * mm, logo_alto_mm * mm / ratio
        img.drawWidth, img.drawHeight = ancho, alto
        img.hAlign = "LEFT"
        izq = [img]
    else:
        izq = [Paragraph(_esc(config.nombre_tienda), st["titulo"])]

    cab = Table([[izq, Paragraph("<br/>".join(contacto), st["der"])]], colWidths=[96 * mm, 73 * mm])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return cab


def _contacto_pie(config):
    """El pie NO repite teléfonos ni correo (ya están arriba): solo la tienda en línea."""
    sitio = urlparse(settings.SITE_URL).netloc
    return [f"Tienda en línea: {sitio}"] if sitio else []


def _dibujar_pie(config):
    """Devuelve la función que dibuja el pie en cada página."""
    partes = _contacto_pie(config)

    def pie(canvas, doc):
        ancho, alto = A4
        centro = ancho / 2
        base = 11 * mm
        canvas.saveState()
        canvas.setStrokeColor(TEAL)
        canvas.setLineWidth(0.8)
        canvas.line(MARGEN_LATERAL, base + 17 * mm, ancho - MARGEN_LATERAL, base + 17 * mm)
        canvas.setFillColor(TEAL)
        canvas.setFont("Helvetica-Bold", 9.5)
        canvas.drawCentredString(centro, base + 12 * mm, PIE_TITULO)
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawCentredString(centro, base + 8 * mm, PIE_GRUPO)
        canvas.setFillColor(SOFT)
        canvas.setFont("Helvetica", 8.5)
        canvas.drawCentredString(centro, base + 4 * mm, PIE_FRASE)
        # Una sola línea: si hay muchos datos y no caben, se quitan los últimos
        disponible = ancho - 2 * MARGEN_LATERAL
        grupo = list(partes)
        while len(grupo) > 1 and canvas.stringWidth("  ·  ".join(grupo), "Helvetica", 8.5) > disponible:
            grupo.pop()
        if grupo:
            canvas.drawCentredString(centro, base, "  ·  ".join(grupo))
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(ancho - MARGEN_LATERAL, base - 5 * mm, f"Página {doc.page}")
        canvas.restoreState()

    return pie


def _canvas_con_marca(texto):
    """Canvas que estampa una marca de agua translúcida ENCIMA del contenido de cada página."""
    class CanvasConMarca(pdfcanvas.Canvas):
        def showPage(self):
            ancho, alto = A4
            self.saveState()
            self.setFillColor(colors.Color(0.75, 0.22, 0.17, alpha=0.2))
            self.setFont("Helvetica-Bold", 84)
            self.translate(ancho / 2, alto / 2)
            self.rotate(35)
            self.drawCentredString(0, 0, texto)
            self.restoreState()
            super().showPage()
    return CanvasConMarca


def _documento(buf, titulo, config):
    return SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGEN_LATERAL, rightMargin=MARGEN_LATERAL, topMargin=14 * mm,
        bottomMargin=MARGEN_INFERIOR, title=f"{titulo} - {config.nombre_tienda}", author=config.nombre_tienda,
    )


def _tabla_totales(subtotal, detalle_reglas, descuento_extra, total, st):
    tot = [["Subtotal", _bs(subtotal)]]
    for linea in filter(None, detalle_reglas.split("\n")):
        nombre, _, monto = linea.rpartition(": ")
        tot.append([Paragraph(f"Promoción: {_esc(nombre)}", st["normal"]), monto.replace("-Bs.", "- Bs.")])
    if descuento_extra:
        tot.append(["Descuento adicional", f"- {_bs(descuento_extra)}"])
    tot.append([Paragraph("<b>TOTAL</b>", st["normal"]), Paragraph(f"<b>{_bs(total)}</b>", st["der"])])
    t = Table(tot, colWidths=[119 * mm, 50 * mm])
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, -1), (-1, -1), 1, TEAL), ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _tabla_items(filas, anchos):
    t = Table(filas, colWidths=anchos, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAF7F1")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, CLAY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


# --------------------------------------------------------------------------
# Cotización
# --------------------------------------------------------------------------
def generar_pdf_cotizacion(cot):
    config = ConfiguracionSitio.get()
    st = _estilos()
    buf = BytesIO()
    doc = _documento(buf, f"Cotización {cot.numero}", config)

    el = [_cabecera(config, st), Spacer(1, 4 * mm)]

    fecha = timezone.localtime(cot.creado).date()
    vence = fecha + timedelta(days=cot.validez_dias)
    el += [Paragraph(f"COTIZACIÓN N° {cot.numero}", st["titulo"]), Spacer(1, 1.5 * mm)]
    # Cliente a la izquierda, fechas a la derecha: una línea menos
    izquierda = f"Cliente: <b>{_esc(cot.cliente)}</b>" if cot.cliente else ""
    derecha = f"Fecha: {fecha:%d/%m/%Y}<br/>Válida hasta: {vence:%d/%m/%Y}"
    datos = Table([[Paragraph(izquierda, st["normal"]), Paragraph(derecha, st["der"])]], colWidths=[100 * mm, 69 * mm])
    datos.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    el += [datos, Spacer(1, 4 * mm)]

    # Foto pequeña de cada producto como referencia para el cliente (11 mm: casi no ocupa espacio).
    # Si NINGÚN producto tiene foto, la columna ni se muestra; si algunos no tienen, llevan un cuadrito claro.
    items = list(cot.items.select_related("producto").prefetch_related("producto__imagenes"))
    fotos = miniaturas_de([it.producto for it in items])
    con_fotos = bool(fotos)
    marcador = _marcador_sin_foto() if con_fotos else None
    filas = [(["" ] if con_fotos else []) + ["Producto", "Cant.", "P. unitario", "Subtotal"]]
    for it in items:
        fila = []
        if con_fotos:
            fila.append(Image(BytesIO(fotos.get(it.producto_id) or marcador), width=FOTO_MM * mm, height=FOTO_MM * mm))
        fila += [Paragraph(_esc(it.producto.nombre), st["normal"]), str(it.cantidad),
                 _bs(it.precio_unitario), _bs(it.subtotal)]
        filas.append(fila)
    if con_fotos:
        tabla = _tabla_items(filas, [16 * mm, 67 * mm, 16 * mm, 35 * mm, 35 * mm])
        tabla.setStyle(TableStyle([
            ("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (1, 0), (1, -1), "LEFT"),
            ("TOPPADDING", (0, 1), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 1), (-1, -1), 1.5),
        ]))
    else:
        tabla = _tabla_items(filas, [81 * mm, 18 * mm, 35 * mm, 35 * mm])
    el += [tabla, Spacer(1, 4 * mm),
           _tabla_totales(cot.subtotal, cot.detalle_reglas, cot.descuento_extra, cot.total, st)]

    if cot.observaciones:
        el += [Spacer(1, 5 * mm), Paragraph("Observaciones", st["negrita"]),
               Paragraph(_esc(cot.observaciones).replace("\n", "<br/>"), st["normal"])]

    notas = [f"Precios en bolivianos (Bs.). Cotización válida por {cot.validez_dias} días, sujeta a disponibilidad."]
    if config.envios_nacionales and config.texto_envios:
        notas.append(_esc(config.texto_envios).rstrip(".") + (f". {_esc(config.detalle_envios).rstrip('.')}" if config.detalle_envios else "") + ".")
    # Las notas finales van juntas: nunca queda "¡Gracias!" solo en otra hoja
    el.append(KeepTogether([Spacer(1, 3.5 * mm), Paragraph(" ".join(notas), st["chico"]),
                            Spacer(1, 2 * mm), Paragraph(PIE_GRACIAS, st["gracias"])]))

    pie = _dibujar_pie(config)
    doc.build(el, onFirstPage=pie, onLaterPages=pie)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Recibo de venta (respaldo): SIEMPRE una sola hoja
# --------------------------------------------------------------------------
def generar_pdf_recibo(venta):
    config = ConfiguracionSitio.get()
    st = _estilos()
    buf = BytesIO()
    numero = f"{venta.pk:05d}"
    doc = _documento(buf, f"Recibo de venta {numero}", config)

    el = [_cabecera(config, st, logo_alto_mm=26, logo_ancho_mm=40), Spacer(1, 4 * mm)]

    fecha = timezone.localtime(venta.creado)
    el += [Paragraph(f"RECIBO DE VENTA N° {numero}", st["titulo"]), Spacer(1, 2 * mm)]
    datos = [f"Fecha: {fecha:%d/%m/%Y}  ·  Hora: {fecha:%H:%M}",
             f"Atendió: <b>{_esc(venta.vendedor.username) if venta.vendedor else '—'}</b>"]
    if venta.cliente:
        datos.insert(1, f"Cliente: <b>{_esc(venta.cliente)}</b>")
    if venta.anulada:
        datos.append("<font color='#C0392B'><b>VENTA ANULADA</b></font>")
    el += [Paragraph("<br/>".join(datos), st["normal"]), Spacer(1, 4 * mm)]

    filas = [["Cant.", "Cód.", "Producto", "P. unitario", "Subtotal"]]
    for it in venta.items.select_related("producto"):
        filas.append([str(it.cantidad), it.producto.codigo or "—", Paragraph(_esc(it.producto.nombre), st["normal"]),
                      _bs(it.precio_unitario), _bs(it.subtotal)])
    tabla = _tabla_items(filas, [14 * mm, 19 * mm, 66 * mm, 35 * mm, 35 * mm])
    tabla.setStyle(TableStyle([("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (1, 0), (2, -1), "LEFT")]))
    el += [tabla, Spacer(1, 3 * mm),
           _tabla_totales(venta.subtotal, venta.detalle_reglas, venta.descuento_extra, venta.total, st)]

    if venta.observaciones:
        el += [Spacer(1, 4 * mm), Paragraph("Observaciones", st["negrita"]),
               Paragraph(_esc(venta.observaciones).replace("\n", "<br/>"), st["normal"])]

    firmas = Table(
        [["", "", ""], [Paragraph("Entregado por", st["chico"]), "", Paragraph("Recibí conforme", st["chico"])]],
        colWidths=[70 * mm, 29 * mm, 70 * mm], rowHeights=[14 * mm, 5 * mm],
    )
    firmas.setStyle(TableStyle([("LINEBELOW", (0, 0), (0, 0), 0.6, SOFT), ("LINEBELOW", (2, 0), (2, 0), 0.6, SOFT),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    el += [Spacer(1, 8 * mm), firmas, Spacer(1, 5 * mm),
           Paragraph("Recibo de respaldo. No reemplaza a una factura.", st["chico"]),
           Spacer(1, 2 * mm), Paragraph(PIE_GRACIAS, st["gracias"])]

    # Pase lo que pase (aunque la venta tenga muchísimos productos) cabe en UNA hoja:
    # si el contenido es más alto que la hoja, se reduce proporcionalmente.
    marco = KeepInFrame(doc.width - 12, doc.height - 12, el, mode="shrink")
    pie = _dibujar_pie(config)
    extra = {"canvasmaker": _canvas_con_marca("ANULADA")} if venta.anulada else {}
    doc.build([marco], onFirstPage=pie, onLaterPages=pie, **extra)
    return buf.getvalue()
