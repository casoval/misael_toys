from catalogo.models import _url_cloudinary_transformada


def foto_panel(producto):
    """Foto cuadrada de 300px para las tarjetas del panel (rápida de cargar,
    nítida en celular). Usa las imágenes ya precargadas: cero consultas extra
    si el queryset hizo prefetch_related('imagenes')."""
    imgs = list(producto.imagenes.all())
    if not imgs:
        return None
    return _url_cloudinary_transformada(imgs[0].imagen.url, "w_300,h_300,c_fill,q_auto,f_auto")
