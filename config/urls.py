from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from django.contrib.sitemaps.views import sitemap

from catalogo.sitemaps import InicioSitemap, CategoriaSitemap, ProductoSitemap
from django.http import HttpResponse

handler404 = 'catalogo.views.error_404'

sitemaps = {"inicio": InicioSitemap, "categorias": CategoriaSitemap, "productos": ProductoSitemap}

urlpatterns = [
    path('admin/', admin.site.urls),
    path('robots.txt', TemplateView.as_view(template_name='robots.txt', content_type='text/plain',
                                            extra_context={'site_url': settings.SITE_URL})),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path('googleef643350cb747fe0.html',
         lambda request: HttpResponse('google-site-verification: googleef643350cb747fe0.html',
                                      content_type='text/html')),
    path('panel/', include('inventario.urls', namespace='panel')),
    path('', include('catalogo.urls', namespace='catalogo')),
]

# Las fotos de producto (media/) se sirven siempre desde Django, con DEBUG en
# True o False — es una tienda pequeña, así que esto es suficiente por ahora.
# El CSS/imágenes fijas de static/ las sirve whitenoise (ver MIDDLEWARE en
# settings.py), en cualquier entorno, sin necesitar esto.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
