"""Fotos que se suben a las cotizaciones a medida.

Una foto de celular pesa 3-8 MB y mide 4000 px: guardarla tal cual haría lento el panel y el PDF.
Aquí se valida, se corrige la orientación (EXIF), se reduce a un máximo razonable y se guarda
como JPEG liviano. Si el archivo no es una imagen, se rechaza con un mensaje claro.
"""
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from .servicios import ErrorNegocio

MAX_BYTES_SUBIDA = 15 * 1024 * 1024   # tope de lo que se acepta subir (antes de reducir)
LADO_MAXIMO_PX = 1600                 # suficiente para el PDF y para verla en pantalla
CALIDAD_JPEG = 85


def preparar_foto(archivo, nombre_base="foto"):
    """Devuelve un ContentFile JPEG listo para guardar en un ImageField.

    Lanza ErrorNegocio (mensaje apto para mostrar) si el archivo no es una imagen válida o pesa demasiado.
    """
    etiqueta = getattr(archivo, "name", "") or "una foto"
    if getattr(archivo, "size", 0) > MAX_BYTES_SUBIDA:
        raise ErrorNegocio(f"«{etiqueta}» pesa demasiado (máximo {MAX_BYTES_SUBIDA // (1024 * 1024)} MB por foto).")
    try:
        archivo.seek(0)
        img = Image.open(archivo)
        img.load()  # fuerza a leer todo: así un archivo dañado falla aquí y no más tarde
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ErrorNegocio(f"«{etiqueta}» no es una imagen válida (usa JPG, PNG o WEBP).") from None

    # Las transparencias se pasan a fondo blanco (JPEG no las soporta y el PDF es sobre papel).
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGB", img.size, "white")
        fondo.paste(img, mask=img.getchannel("A"))
        img = fondo
    else:
        img = img.convert("RGB")

    img.thumbnail((LADO_MAXIMO_PX, LADO_MAXIMO_PX), Image.LANCZOS)  # solo reduce, nunca agranda
    salida = BytesIO()
    img.save(salida, "JPEG", quality=CALIDAD_JPEG, optimize=True)
    return ContentFile(salida.getvalue(), name=f"{nombre_base}.jpg")
