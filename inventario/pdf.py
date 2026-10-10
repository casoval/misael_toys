"""PDF de cotización para enviar al cliente.

Importante: el PDF NO incluye stock ni ubicaciones, solo lo que el cliente
debe ver (productos, cantidades, precios, descuentos y total)."""
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from catalogo.models import ConfiguracionSitio

TEAL = colors.HexColor("#0B5C53")
CLAY = colors.HexColor("#E4DFD3")
SOFT = colors.HexColor("#6B6459")


def _bs(valor):
    return f"Bs. {valor:,.2f}"


def _esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generar_pdf_cotizacion(cot):
    config = ConfiguracionSitio.get()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Cotización {cot.numero} - {config.nombre_tienda}", author=config.nombre_tienda,
    )
    base = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=base, fontName="Helvetica", fontSize=10, leading=14)
    chico = ParagraphStyle("c", parent=normal, fontSize=8.5, textColor=SOFT, leading=12)
    titulo = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=20, textColor=TEAL, leading=24)
    negrita = ParagraphStyle("b", parent=normal, fontName="Helvetica-Bold")
    der = ParagraphStyle("d", parent=normal, alignment=2)

    el = []

    # Encabezado: logo + datos de la tienda
    logo_path = Path(settings.BASE_DIR) / "static" / "img" / "logo.png"
    contacto = [f"<b>{_esc(config.nombre_tienda)}</b>"]
    if config.telefono:
        contacto.append(f"Tel: {_esc(config.telefono)}")
    if config.whatsapp_numero:
        contacto.append(f"WhatsApp: +{_esc(config.whatsapp_numero)}")
    if config.email_contacto:
        contacto.append(_esc(config.email_contacto))
    izq = []
    if logo_path.exists():
        img = Image(str(logo_path))
        ratio = img.imageHeight / float(img.imageWidth)
        img.drawWidth, img.drawHeight = 38 * mm, 38 * mm * ratio
        if img.drawHeight > 22 * mm:
            img.drawHeight, img.drawWidth = 22 * mm, 22 * mm / ratio
        img.hAlign = "LEFT"
        izq.append(img)
    else:
        izq.append(Paragraph(_esc(config.nombre_tienda), titulo))
    cab = Table([[izq, Paragraph("<br/>".join(contacto), der)]], colWidths=[86 * mm, 83 * mm])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    el += [cab, Spacer(1, 8 * mm)]

    # Título y datos
    fecha = timezone.localtime(cot.creado).date()
    vence = fecha + timedelta(days=cot.validez_dias)
    el.append(Paragraph(f"COTIZACIÓN N° {cot.numero}", titulo))
    el.append(Spacer(1, 2 * mm))
    datos = [f"Fecha: {fecha:%d/%m/%Y}", f"Válida hasta: {vence:%d/%m/%Y}"]
    if cot.cliente:
        datos.insert(0, f"Cliente: <b>{_esc(cot.cliente)}</b>")
    el.append(Paragraph("<br/>".join(datos), normal))
    el.append(Spacer(1, 6 * mm))

    # Tabla de productos
    filas = [["Producto", "Cant.", "P. unitario", "Subtotal"]]
    for it in cot.items.select_related("producto"):
        filas.append([Paragraph(_esc(it.producto.nombre), normal), str(it.cantidad),
                      _bs(it.precio_unitario), _bs(it.subtotal)])
    tabla = Table(filas, colWidths=[81 * mm, 18 * mm, 35 * mm, 35 * mm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAF7F1")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, CLAY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    el += [tabla, Spacer(1, 4 * mm)]

    # Totales
    tot = [["Subtotal", _bs(cot.subtotal)]]
    for linea in filter(None, cot.detalle_reglas.split("\n")):
        nombre, _, monto = linea.rpartition(": ")
        tot.append([Paragraph(f"Promoción: {_esc(nombre)}", normal), monto.replace("-Bs.", "- Bs.")])
    if cot.descuento_extra:
        tot.append(["Descuento adicional", f"- {_bs(cot.descuento_extra)}"])
    tot.append([Paragraph("<b>TOTAL</b>", normal), Paragraph(f"<b>{_bs(cot.total)}</b>", der)])
    t2 = Table(tot, colWidths=[119 * mm, 50 * mm])
    t2.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, -1), (-1, -1), 1, TEAL), ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    el.append(t2)

    if cot.observaciones:
        el += [Spacer(1, 8 * mm), Paragraph("Observaciones", negrita),
               Paragraph(_esc(cot.observaciones).replace("\n", "<br/>"), normal)]

    notas = [f"Precios en bolivianos (Bs.). Cotización válida por {cot.validez_dias} días, sujeta a disponibilidad."]
    if config.envios_nacionales and config.texto_envios:
        notas.append(_esc(config.texto_envios) + (f". {_esc(config.detalle_envios)}" if config.detalle_envios else ""))
    el += [Spacer(1, 10 * mm)] + [Paragraph(n, chico) for n in notas]

    doc.build(el)
    return buf.getvalue()
