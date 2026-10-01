from urllib.parse import urlparse

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import Categoria, Producto


class _Base(Sitemap):
    protocol = "https"

    def get_domain(self, site=None):
        # Siempre el dominio público de la tienda, sin depender del Host de la petición.
        return urlparse(settings.SITE_URL).netloc


class InicioSitemap(_Base):
    changefreq = "weekly"
    priority = 1.0

    def items(self):
        return ["catalogo:home"]

    def location(self, item):
        return reverse(item)


class CategoriaSitemap(_Base):
    changefreq = "weekly"
    priority = 0.9

    def items(self):
        return Categoria.objects.filter(activa=True, productos__disponible=True).distinct()

    def location(self, obj):
        return reverse("catalogo:categoria_detalle", kwargs={"slug": obj.slug})


class ProductoSitemap(_Base):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Producto.objects.filter(disponible=True)

    def lastmod(self, obj):
        return obj.actualizado
