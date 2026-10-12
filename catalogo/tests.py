from decimal import Decimal

from django.test import TestCase

from pathlib import Path

from django.conf import settings

from .models import Categoria, ConfiguracionSitio, Producto


class ContactoTests(TestCase):
    """Hasta 2 teléfonos y 2 WhatsApp; con 2 WhatsApp la burbuja ofrece las dos opciones."""

    def setUp(self):
        self.config = ConfiguracionSitio.get()
        self.producto = Producto.objects.create(nombre="Cubo sensorial", precio=Decimal("45"))

    def poner(self, **campos):
        for k, v in campos.items():
            setattr(self.config, k, v)
        self.config.save()

    def test_normalizacion_de_numeros(self):
        self.poner(whatsapp_numero="+591 7123-4567", whatsapp_numero_2="   ")
        self.assertEqual([w["numero"] for w in self.config.whatsapps], ["59171234567"])
        self.assertEqual(self.config.whatsapps[0]["visible"], "+59171234567")

    def test_sin_numeros_no_hay_burbuja(self):
        r = self.client.get("/")
        self.assertNotContains(r, "btn-whatsapp-flotante")
        self.assertNotContains(r, "wa-flotante")

    def test_un_solo_whatsapp_enlace_directo(self):
        self.poner(whatsapp_numero="59171234567")
        r = self.client.get("/")
        self.assertContains(r, "https://wa.me/59171234567?text=")
        self.assertContains(r, 'class="btn-whatsapp-flotante"')
        self.assertNotContains(r, 'id="wa-menu"')  # sin menú: solo hay una opción

    def test_dos_whatsapp_la_burbuja_muestra_dos_opciones(self):
        self.poner(whatsapp_numero="59171234567", whatsapp_etiqueta="Ventas",
                   whatsapp_numero_2="59176543210", whatsapp_etiqueta_2="Consultas")
        r = self.client.get("/")
        self.assertContains(r, 'id="wa-menu"')
        self.assertContains(r, "https://wa.me/59171234567?text=")
        self.assertContains(r, "https://wa.me/59176543210?text=")
        self.assertContains(r, "Ventas")
        self.assertContains(r, "Consultas")
        self.assertContains(r, "+59171234567")  # el número se ve debajo del nombre
        self.assertContains(r, 'aria-haspopup="true"')

    def test_dos_whatsapp_sin_nombre_muestran_el_numero(self):
        self.poner(whatsapp_numero="59171234567", whatsapp_numero_2="59176543210")
        r = self.client.get("/")
        self.assertContains(r, '<span class="wa-opcion-nombre">+59171234567</span>')
        self.assertContains(r, '<span class="wa-opcion-nombre">+59176543210</span>')

    def test_solo_el_segundo_whatsapp_tambien_funciona(self):
        self.poner(whatsapp_numero="", whatsapp_numero_2="59176543210")
        self.assertContains(self.client.get("/"), "https://wa.me/59176543210?text=")

    def test_encabezado_muestra_los_dos_telefonos(self):
        self.poner(telefono="72345678", telefono_2="62211111")
        self.assertContains(self.client.get("/"), "72345678 · 62211111")

    def test_encabezado_solo_muestra_telefonos_no_los_whatsapp(self):
        self.poner(telefono="72345678", whatsapp_numero="59171111111", whatsapp_numero_2="59172222222")
        html = self.client.get("/").content.decode()
        bloque = html.split('class="contacto-header"')[1].split("</div>")[0]
        self.assertIn("72345678", bloque)
        self.assertNotIn("59171111111", bloque)
        self.assertNotIn("+5917", bloque)
        # ...pero los WhatsApp siguen funcionando en la burbuja
        self.assertIn("https://wa.me/59171111111?text=", html)
        self.assertIn("https://wa.me/59172222222?text=", html)

    def test_encabezado_sin_telefono_usa_los_whatsapp(self):
        self.poner(whatsapp_numero="59171234567", whatsapp_numero_2="59176543210")
        self.assertContains(self.client.get("/"), "+59171234567 · +59176543210")

    def test_producto_con_dos_whatsapp_tiene_dos_botones(self):
        self.poner(whatsapp_numero="59171234567", whatsapp_etiqueta="Ventas",
                   whatsapp_numero_2="59176543210", whatsapp_etiqueta_2="Consultas")
        r = self.client.get(self.producto.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "WhatsApp · Ventas")
        self.assertContains(r, "WhatsApp · Consultas")
        self.assertContains(r, "https://wa.me/59176543210?text=Hola%2C%20quiero%20consultar%20sobre%20el%20producto")
        self.assertContains(r, "Cubo%20sensorial")

    def test_producto_con_un_whatsapp_conserva_el_boton_de_siempre(self):
        self.poner(whatsapp_numero="59171234567")
        r = self.client.get(self.producto.get_absolute_url())
        self.assertContains(r, "Consultar por WhatsApp")
        self.assertContains(r, "https://wa.me/59171234567?text=")

    def test_producto_sin_whatsapp_muestra_telefonos(self):
        self.poner(telefono="72345678", telefono_2="62211111", email_contacto="hola@x.com")
        r = self.client.get(self.producto.get_absolute_url())
        self.assertContains(r, "Para consultar sobre este producto")
        self.assertContains(r, "72345678 · 62211111")
        self.assertContains(r, "hola@x.com")

    def test_seo_usa_el_primer_telefono_disponible(self):
        self.poner(telefono="", telefono_2="62211111")
        self.assertContains(self.client.get("/"), "62211111")

    def test_404_con_burbuja_de_dos_opciones(self):
        self.poner(whatsapp_numero="59171234567", whatsapp_numero_2="59176543210")
        r = self.client.get("/no-existe-abc/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, 'id="wa-menu"', status_code=404)


class PortadaTests(TestCase):
    """Categorías, aviso de envíos y tarjetas en celular de la página principal."""

    def setUp(self):
        self.config = ConfiguracionSitio.get()
        self.cat = Categoria.objects.create(nombre="Comida sensorial")
        Producto.objects.create(nombre="Pizza fidget", precio=Decimal("95"), categoria=self.cat)

    def test_explora_por_categoria_trae_el_color_de_cada_categoria(self):
        r = self.client.get("/")
        self.assertContains(r, 'class="explora-link"')
        self.assertContains(r, f'style="--c: {self.cat.color_fondo};"')
        self.assertContains(r, "Comida sensorial")

    def test_aviso_de_envios_con_icono_y_detalle(self):
        self.config.envios_nacionales = True
        self.config.texto_envios = "Envíos a todo el país"
        self.config.detalle_envios = "Por flota o encomienda"
        self.config.save()
        r = self.client.get("/")
        self.assertContains(r, 'class="envios-icono"')
        self.assertContains(r, "<b>Envíos a todo el país</b>")
        self.assertContains(r, "<small>Por flota o encomienda</small>")

    def test_sin_envios_no_hay_aviso(self):
        self.config.envios_nacionales = False
        self.config.save()
        self.assertNotContains(self.client.get("/"), 'class="franja-envios"')

    def test_el_css_pone_dos_productos_por_fila_en_celular(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "style.css").read_text(encoding="utf-8")
        bloque = css[css.index("@media (max-width: 599px)"):]
        self.assertIn(".grid-productos { grid-template-columns: repeat(2, minmax(0, 1fr));", bloque)
        # y las clases del aviso de envíos y de las categorías ya no están sin estilo
        for clase in (".explora-link", ".explora-categorias", ".franja-envios", ".envios-icono"):
            self.assertIn(clase, css)


class CodigoInternoTests(TestCase):
    """Código interno de producto: J001, J002… automático, opcional y editable."""

    def crear(self, nombre="Producto", **extra):
        return Producto.objects.create(nombre=nombre, precio=Decimal("10"), **extra)

    def test_el_primer_producto_es_j001_y_siguen_en_orden(self):
        self.assertEqual([self.crear(f"P{i}").codigo for i in range(3)], ["J001", "J002", "J003"])

    def test_codigo_manual_se_respeta_y_se_guarda_en_mayusculas(self):
        p = self.crear("Manual", codigo="  kit-01 ")
        self.assertEqual(p.codigo, "KIT-01")

    def test_un_codigo_manual_con_otra_forma_no_altera_la_numeracion(self):
        self.crear("A"); self.crear("B", codigo="KIT-01")
        self.assertEqual(self.crear("C").codigo, "J002")

    def test_continua_desde_el_numero_mas_alto_aunque_haya_huecos(self):
        self.crear("A", codigo="J001"); self.crear("B", codigo="J009")
        self.assertEqual(self.crear("C").codigo, "J010")

    def test_pasa_de_999_sin_problemas(self):
        self.crear("A", codigo="J999")
        self.assertEqual(self.crear("B").codigo, "J1000")

    def test_si_se_borra_el_codigo_se_genera_uno_nuevo(self):
        p = self.crear("A"); self.assertEqual(p.codigo, "J001")
        p.codigo = ""
        p.save()
        self.assertEqual(p.codigo, "J001")  # era el único: no se pierde un número
        q = self.crear("B"); self.assertEqual(q.codigo, "J002")
        p.codigo = None; p.save()
        self.assertEqual(p.codigo, "J003")  # hay uno posterior: toma el siguiente libre
        self.assertEqual(Producto.objects.filter(codigo="J001").count(), 0)

    def test_guardar_otra_vez_no_cambia_el_codigo(self):
        p = self.crear("A")
        for _ in range(3):
            p.nombre += "!"; p.save()
        self.assertEqual(Producto.objects.get(pk=p.pk).codigo, "J001")

    def test_guardado_parcial_sin_codigo_no_lo_toca(self):
        p = self.crear("A")
        Producto.objects.filter(pk=p.pk).update(codigo=None)
        p.refresh_from_db()
        p.total_likes = 5
        p.save(update_fields=["total_likes"])  # p.ej. dar "me gusta": no debe inventar un código
        self.assertIsNone(Producto.objects.get(pk=p.pk).codigo)

    def test_no_se_puede_repetir_ni_cambiando_mayusculas(self):
        from django.core.exceptions import ValidationError
        self.crear("A", codigo="J005")
        otro = Producto(nombre="B", slug="b", precio=Decimal("1"), codigo="j005")
        with self.assertRaises(ValidationError) as ctx:
            otro.full_clean()
        self.assertIn("codigo", ctx.exception.message_dict)

    def test_la_migracion_numera_los_productos_existentes_en_orden_de_creacion(self):
        from importlib import import_module
        from django.apps import apps
        a, b, c = self.crear("Viejo 1"), self.crear("Viejo 2"), self.crear("Viejo 3")
        Producto.objects.update(codigo=None)
        import_module("catalogo.migrations.0011_asignar_codigos").asignar_codigos(apps, None)
        self.assertEqual(list(Producto.objects.order_by("id").values_list("codigo", flat=True)), ["J001", "J002", "J003"])

    def test_el_codigo_no_se_muestra_en_el_sitio_publico(self):
        p = self.crear("Cubo sensorial")
        for url in ("/", p.get_absolute_url()):
            self.assertNotContains(self.client.get(url), p.codigo)

    def test_el_admin_sugiere_el_siguiente_codigo_y_busca_por_codigo(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_superuser("root", "r@r.com", "x")
        self.client.login(username="root", password="x")
        self.crear("A"); self.crear("B")
        r = self.client.get("/admin/catalogo/producto/add/")
        self.assertContains(r, 'value="J003"')
        r = self.client.get("/admin/catalogo/producto/?q=J002")
        self.assertContains(r, "B")


class EstilosDelCatalogoTests(TestCase):
    """Protege los estilos del sitio: un edit del CSS ya borró por accidente los de colores, medidas y SEO."""

    CLASES_CON_ESTILO = [
        # colores disponibles
        ".detalle-colores", ".detalle-colores-titulo", ".detalle-colores-nota", ".colores-grid", ".color-item",
        ".swatch-color", ".color-nombre", ".detalle-colores-aviso",
        # medidas y envíos
        ".detalle-medidas", ".medidas-grid", ".medidas-nota", ".medidas-referencia", ".detalle-envio",
        ".tarjeta-medidas", ".footer-envios", ".franja-envios",
        # SEO, categorías y animaciones
        ".hero-h1", ".categoria-intro p", ".seo-texto", ".explora-categorias", ".explora-link", ".footer a",
        # tarjetas, filtros y colores de filtros
        ".grid-productos", ".tarjeta-nombre", ".filtro-color", ".swatch", ".swatch-multicolor",
    ]

    def test_el_css_conserva_los_estilos_de_cada_seccion(self):
        import re
        css = (Path(settings.BASE_DIR) / "static" / "css" / "style.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        definidos = set()
        for m in re.finditer(r"([^{}]+)\{", css):
            for sel in m.group(1).split(","):
                definidos.add(" ".join(sel.split()))
        faltan = [c for c in self.CLASES_CON_ESTILO if c not in definidos]
        self.assertEqual(faltan, [], f"Clases sin estilos en style.css: {faltan}")

    def test_la_ficha_usa_clases_que_tienen_estilo(self):
        p = Producto.objects.create(nombre="Cubo", precio=Decimal("10"))
        r = self.client.get(p.get_absolute_url())
        for clase in ("detalle-colores", "colores-grid", "color-item", "swatch-color", "color-nombre"):
            self.assertContains(r, clase)
