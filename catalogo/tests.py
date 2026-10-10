from decimal import Decimal

from django.test import TestCase

from .models import ConfiguracionSitio, Producto


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
