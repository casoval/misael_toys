import re
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalogo.models import Atributo, Categoria, ImagenProducto, Producto, ValorAtributo

from . import catalogo_pdf, servicios
from .models import Cotizacion, MovimientoStock, ReglaDescuento, Stock, Ubicacion, Venta
from .servicios import ErrorNegocio

User = get_user_model()
D = Decimal


# Hash rápido solo en tests (el real, PBKDF2, hace que la suite tarde un minuto).
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="x", is_staff=True)
        self.ana = User.objects.create_user("ana", password="x")
        self.luis = User.objects.create_user("luis", password="x")
        self.casa = Ubicacion.objects.create(nombre="Casa")
        self.local = Ubicacion.objects.create(nombre="Local")
        self.cat = Categoria.objects.create(nombre="Táctil")
        self.p1 = Producto.objects.create(nombre="Pop it", precio=D("10.00"), categoria=self.cat)
        self.p2 = Producto.objects.create(nombre="Cubo", precio=D("20.00"))
        # La migración de datos ya creó las reglas 5+ (5%) y 10+ (10%)

    def stock(self, producto, ubicacion):
        s = Stock.objects.filter(producto=producto, ubicacion=ubicacion).first()
        return s.cantidad if s else 0

    def item(self, producto, ubicacion, cantidad, precio=None):
        return {"producto": producto, "ubicacion": ubicacion, "cantidad": cantidad,
                "precio_unitario": precio if precio is not None else producto.precio}


class EnterosTests(Base):
    def test_nada_tiene_decimales(self):
        p = Producto.objects.create(nombre="Raro", precio=D("12.67"))
        t = servicios.calcular_totales([self.item(p, None, 7, D("12.67"))], descuento_extra=D("1.5"))
        for clave in ("subtotal", "descuento_reglas", "descuento_extra", "total"):
            self.assertEqual(t[clave], t[clave].to_integral_value(), clave)
        for a in t["aplicadas"]:
            self.assertEqual(a["monto"], a["monto"].to_integral_value())
        # 7 × 13 = 91; 5% = 4,55 → 5; extra 1,5 → 2; total 84
        self.assertEqual(t["total"], D("84"))

    def test_venta_guarda_enteros_y_se_muestra_sin_decimales(self):
        servicios.registrar_ingreso(self.p1, self.casa, 10, self.admin)
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 5, D("9.4"))])
        self.assertEqual(v.items.get().precio_unitario, D("9"))
        self.client.login(username="admin", password="x")
        r = self.client.get(reverse("panel:venta_detalle", args=[v.pk]))
        self.assertContains(r, "Bs. 43")
        self.assertNotContains(r, ",00")
        self.assertNotContains(r, "43,")


class ReglasTests(Base):
    def test_reglas_iniciales_existen(self):
        self.assertEqual(ReglaDescuento.objects.count(), 2)

    def test_menos_de_5_sin_descuento(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 4)])
        self.assertEqual(t["total"], D("40.00"))
        self.assertEqual(t["aplicadas"], [])
        self.assertIn("1 unidad más", t["sugerencia"])

    def test_5_unidades_5_por_ciento(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 5)])
        # 5% de 50 = 2,5 → se redondea a 3: nunca hay decimales
        self.assertEqual(t["descuento_reglas"], D("3"))
        self.assertEqual(t["total"], D("47"))

    def test_10_unidades_solo_10_no_se_suman(self):
        # 6 + 4 productos distintos = 10 unidades; 10% de 160 = 16 (no 5%+10%)
        t = servicios.calcular_totales([self.item(self.p1, None, 6), self.item(self.p2, None, 5)])  # 11 u, 160
        self.assertEqual(len(t["aplicadas"]), 1)
        self.assertEqual(t["descuento_reglas"], D("16.00"))
        self.assertEqual(t["total"], D("144.00"))

    def test_sin_aplicar_reglas(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 10)], aplicar_reglas=False)
        self.assertEqual(t["total"], D("100.00"))

    def test_precio_editado_y_descuento_extra(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 5, D("8.00"))], descuento_extra=D("3"))
        # 5*8=40; -5% = 2 ; -3 extra
        self.assertEqual(t["total"], D("35"))

    def test_descuento_extra_no_deja_total_negativo(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 1)], descuento_extra=D("999"))
        self.assertEqual(t["total"], D("0.00"))

    def test_promo_por_producto_acumulable(self):
        ReglaDescuento.objects.create(nombre="Promo pop it", minimo_unidades=2, porcentaje=20,
                                      alcance="producto", producto=self.p1, acumulable=True)
        t = servicios.calcular_totales([self.item(self.p1, None, 5), self.item(self.p2, None, 1)])
        # 5% de 70 (=3,5 → 4) + 20% de 50 (=10)
        self.assertEqual(t["descuento_reglas"], D("14"))

    def test_regla_vencida_o_inactiva_no_aplica(self):
        ReglaDescuento.objects.update(activa=False)
        t = servicios.calcular_totales([self.item(self.p1, None, 20)])
        self.assertEqual(t["total"], D("200.00"))


class StockTests(Base):
    def test_inicial_luego_ingreso(self):
        servicios.registrar_ingreso(self.p1, self.casa, 10, self.admin)
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        tipos = list(MovimientoStock.objects.order_by("id").values_list("tipo", flat=True))
        self.assertEqual(tipos, ["inicial", "ingreso"])
        self.assertEqual(self.stock(self.p1, self.casa), 15)

    def test_traslado(self):
        servicios.registrar_ingreso(self.p1, self.casa, 10, self.admin)
        servicios.trasladar(self.p1, self.casa, self.local, 4, self.admin)
        self.assertEqual((self.stock(self.p1, self.casa), self.stock(self.p1, self.local)), (6, 4))

    def test_traslado_sin_stock_no_cambia_nada(self):
        servicios.registrar_ingreso(self.p1, self.casa, 2, self.admin)
        with self.assertRaises(ErrorNegocio):
            servicios.trasladar(self.p1, self.casa, self.local, 5, self.admin)
        self.assertEqual(self.stock(self.p1, self.casa), 2)
        self.assertEqual(self.stock(self.p1, self.local), 0)

    def test_ajuste(self):
        servicios.registrar_ingreso(self.p1, self.casa, 10, self.admin)
        servicios.ajustar(self.p1, self.casa, 7, self.admin, "se rompieron 3")
        self.assertEqual(self.stock(self.p1, self.casa), 7)


class VentaTests(Base):
    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 10, self.admin)
        servicios.registrar_ingreso(self.p1, self.local, 3, self.admin)

    def test_venta_descuenta_del_lugar_elegido(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 5, D("9"))], cliente="Juan")
        self.assertEqual(self.stock(self.p1, self.casa), 5)
        self.assertEqual(self.stock(self.p1, self.local), 3)
        # 5 × 9 = 45, -5% = 2,25 → 2 → 43
        self.assertEqual(v.total, D("43"))
        self.assertEqual(v.items.get().precio_lista, D("10.00"))
        self.assertEqual(v.items.get().precio_unitario, D("9.00"))
        self.assertEqual(v.vendedor, self.ana)

    def test_vender_mas_de_lo_que_hay_nunca_se_bloquea(self):
        # En Local hay 3; se venden 5: se vende igual, queda en -2 y se anota el faltante
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 5)])
        self.assertEqual(Venta.objects.count(), 1)
        self.assertEqual(self.stock(self.p1, self.local), -2)
        it = v.items.get()
        self.assertEqual(it.faltante, 2)
        self.assertTrue(v.tiene_faltante)
        self.assertIn("sin stock", MovimientoStock.objects.filter(venta=v).get().nota)

    def test_vender_producto_sin_ningun_stock_registrado(self):
        # p2 nunca tuvo stock (producto por encargo)
        v = servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 3)])
        self.assertEqual(self.stock(self.p2, self.casa), -3)
        self.assertEqual(v.items.get().faltante, 3)

    def test_producir_despues_compensa_el_negativo(self):
        servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 3)])
        servicios.registrar_ingreso(self.p2, self.casa, 3, self.admin, "producción")
        self.assertEqual(self.stock(self.p2, self.casa), 0)

    def test_ingreso_parcial_sobre_stock_negativo(self):
        servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 5)])  # queda en -5
        servicios.registrar_ingreso(self.p2, self.casa, 3, self.admin)           # -2, no debe fallar
        self.assertEqual(self.stock(self.p2, self.casa), -2)
        servicios.registrar_ingreso(self.p2, self.casa, 2, self.admin)
        self.assertEqual(self.stock(self.p2, self.casa), 0)

    def test_anular_venta_cuando_el_stock_sigue_negativo(self):
        v1 = servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 3)])
        servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 4)])  # stock -7
        servicios.anular_venta(v1, self.admin)                                    # -4, no debe fallar
        self.assertEqual(self.stock(self.p2, self.casa), -4)

    def test_traslado_hacia_un_lugar_con_stock_negativo(self):
        # Parte de: Casa 10, Local 3 (setUp). Se venden 6 del Local → Local queda en -3.
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 6)])
        self.assertEqual(self.stock(self.p1, self.local), -3)
        servicios.trasladar(self.p1, self.casa, self.local, 2, self.admin)  # no debe fallar
        self.assertEqual((self.stock(self.p1, self.casa), self.stock(self.p1, self.local)), (8, -1))

    def test_corregir_cantidad_desde_negativo(self):
        servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 4)])
        servicios.ajustar(self.p2, self.casa, 0, self.admin, "ya estaba en físico")
        self.assertEqual(self.stock(self.p2, self.casa), 0)

    def test_venta_sin_descontar_de_ningun_lugar(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, None, 2)])
        self.assertEqual(v.items.get().ubicacion, None)
        self.assertEqual(self.stock(self.p1, self.casa), 10)
        self.assertEqual(MovimientoStock.objects.filter(venta=v).count(), 0)
        servicios.anular_venta(v, self.admin)  # anular una venta así no falla
        self.assertEqual(self.stock(self.p1, self.casa), 10)

    def test_parcialmente_sin_stock_cuenta_solo_lo_que_falta(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 3), self.item(self.p1, self.local, 2)])
        faltantes = list(v.items.order_by("id").values_list("faltante", flat=True))
        self.assertEqual(faltantes, [0, 2])  # la 1.ª usa las 3 que había; la 2.ª no tenía nada
        self.assertEqual(self.stock(self.p1, self.local), -2)

    def test_las_demas_operaciones_si_validan_stock(self):
        with self.assertRaises(ErrorNegocio):
            servicios.trasladar(self.p1, self.local, self.casa, 99, self.admin)

    def test_anular_devuelve_stock(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 4)])
        servicios.anular_venta(v, self.admin, "error")
        v.refresh_from_db()
        self.assertTrue(v.anulada)
        self.assertEqual(self.stock(self.p1, self.casa), 10)

    def test_anular_venta_sin_stock_vuelve_a_cero(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 3)])
        servicios.anular_venta(v, self.admin)
        self.assertEqual(self.stock(self.p2, self.casa), 0)
        with self.assertRaises(ErrorNegocio):
            servicios.anular_venta(v, self.admin)


class VistasTests(Base):
    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 20, self.admin)

    def test_requiere_login(self):
        for nombre in ["panel:stock", "panel:venta_nueva", "panel:ventas", "panel:cotizacion_nueva", "panel:usuarios"]:
            r = self.client.get(reverse(nombre))
            self.assertEqual(r.status_code, 302, nombre)
            self.assertIn("/panel/login/", r["Location"])

    def test_404_publico_funciona(self):
        self.assertEqual(self.client.get("/no-existe-xyz/").status_code, 404)

    def test_catalogo_publico_no_muestra_stock(self):
        r = self.client.get("/")
        self.assertNotContains(r, "Stock")
        r = self.client.get(self.p1.get_absolute_url())
        self.assertNotContains(r, "Casa")

    def test_vendedor_no_puede_entrar_a_zonas_admin(self):
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(reverse("panel:usuarios")).status_code, 403)
        self.assertEqual(self.client.post(reverse("panel:stock_ingreso", args=[self.p1.pk]),
                                          {"ubicacion": self.casa.pk, "cantidad": 5}).status_code, 403)
        self.assertEqual(self.client.get("/admin/").status_code, 302)  # al login del admin de Django

    def test_stock_en_tarjetas_con_precio_y_lugares(self):
        servicios.registrar_ingreso(self.p1, self.local, 3, self.admin)
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, 'class="p-prod ')
        self.assertContains(r, "Bs. 10")
        self.assertContains(r, "Local <b>3</b>")
        self.assertContains(r, 'data-vista="lista"')  # botón para cambiar a lista

    def test_stock_no_hace_consultas_por_producto(self):
        self.client.login(username="ana", password="x")
        self.client.get(reverse("panel:stock"))  # calienta cachés (sesión, etc.)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as pocas:
            self.client.get(reverse("panel:stock"))
        for i in range(15):
            Producto.objects.create(nombre=f"Extra {i}", precio=D("5"))
        with CaptureQueriesContext(connection) as muchas:
            self.client.get(reverse("panel:stock"))
        self.assertEqual(len(pocas), len(muchas))

    def test_formulario_de_venta_lista_todos_los_lugares_aunque_esten_en_cero(self):
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:venta_nueva"))
        datos = {p["nombre"]: p for p in r.context["productos_json"]}
        self.assertEqual(set(datos["Cubo"]["stock"]), {str(self.casa.pk), str(self.local.pk)})
        self.assertEqual(datos["Cubo"]["total"], 0)  # sin stock, pero igual se puede vender

    def test_formulario_de_venta_trae_datos_para_las_tarjetas(self):
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:venta_nueva"))
        self.assertContains(r, 'id="picker"')
        self.assertContains(r, "datos-productos")
        self.assertContains(r, "Pop it")

    def test_vendedor_ve_stock(self):
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, "Pop it")
        self.assertContains(r, "Casa")

    def _venta_post(self, **extra):
        data = {"producto": [self.p1.pk], "ubicacion": [self.casa.pk], "cantidad": ["5"], "precio": ["9,50"],
                "aplicar_reglas": "on", "cliente": "Maria", "observaciones": "x", "descuento_extra": ""}
        data.update(extra)
        return self.client.post(reverse("panel:venta_nueva"), data)

    def test_vender_desde_la_vista(self):
        self.client.login(username="ana", password="x")
        r = self._venta_post()
        v = Venta.objects.get()
        self.assertRedirects(r, reverse("panel:venta_detalle", args=[v.pk]))
        # precio "9,50" → 10 (entero); 5×10 = 50; -5% = 2,5 → 3; total 47
        self.assertEqual(v.total, D("47"))
        self.assertEqual(v.items.get().precio_unitario, D("10"))
        self.assertEqual(self.stock(self.p1, self.casa), 15)

    def test_venta_desde_la_vista_sin_stock_suficiente_se_registra(self):
        self.client.login(username="ana", password="x")
        r = self._venta_post(cantidad=["999"])
        v = Venta.objects.get()
        self.assertRedirects(r, reverse("panel:venta_detalle", args=[v.pk]))
        self.assertEqual(self.stock(self.p1, self.casa), 20 - 999)
        self.assertContains(self.client.get(reverse("panel:venta_detalle", args=[v.pk])), "sin stock: 979")
        self.assertContains(self.client.get(reverse("panel:ventas")), "SIN STOCK")

    def test_venta_desde_la_vista_sin_lugar(self):
        self.client.login(username="ana", password="x")
        self._venta_post(ubicacion=[""])
        self.assertEqual(Venta.objects.get().items.get().ubicacion, None)
        self.assertEqual(self.stock(self.p1, self.casa), 20)

    def test_venta_con_datos_invalidos_no_guarda_y_explica(self):
        self.client.login(username="ana", password="x")
        r = self._venta_post(cantidad=["0"])
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "la cantidad debe ser un número mayor que 0")
        self.assertEqual(Venta.objects.count(), 0)

    def test_stock_negativo_se_ve_en_rojo_y_avisa(self):
        servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 2)])
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, "vendida")
        self.assertContains(r, 'class="neg"')
        self.assertContains(self.client.get(reverse("panel:stock_producto", args=[self.p2.pk])), "sin stock registrado")

    def test_vendedor_solo_ve_sus_ventas(self):
        v = servicios.registrar_venta(self.luis, [self.item(self.p1, self.casa, 1)])
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(reverse("panel:venta_detalle", args=[v.pk])).status_code, 404)
        self.client.login(username="admin", password="x")
        self.assertEqual(self.client.get(reverse("panel:venta_detalle", args=[v.pk])).status_code, 200)

    def test_solo_admin_anula(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)])
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.post(reverse("panel:venta_anular", args=[v.pk])).status_code, 403)
        self.client.login(username="admin", password="x")
        self.client.post(reverse("panel:venta_anular", args=[v.pk]), {"motivo": "prueba"})
        v.refresh_from_db()
        self.assertTrue(v.anulada)
        self.assertEqual(self.stock(self.p1, self.casa), 20)

    def test_calcular_htmx(self):
        self.client.login(username="ana", password="x")
        r = self.client.post(reverse("panel:calcular"), {
            "producto": [self.p1.pk], "cantidad": ["10"], "precio": [""], "aplicar_reglas": "on"})
        self.assertContains(r, "10%")
        self.assertContains(r, "90")

    def test_cotizacion_y_pdf(self):
        self.client.login(username="ana", password="x")
        r = self.client.post(reverse("panel:cotizacion_nueva"), {
            "producto": [self.p1.pk, self.p2.pk], "cantidad": ["5", "1"], "precio": ["", ""],
            "aplicar_reglas": "on", "cliente": "Pedro & Cía <b>", "validez_dias": "10", "observaciones": "Entrega en 3 días"})
        cot = Cotizacion.objects.get()
        self.assertRedirects(r, reverse("panel:cotizacion_detalle", args=[cot.pk]))
        self.assertEqual(cot.total, D("66"))  # 70 - 5% (3,5 → 4)
        self.assertEqual(self.stock(self.p1, self.casa), 20)  # no toca stock
        pdf = self.client.get(reverse("panel:cotizacion_pdf", args=[cot.pk]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        # Se abre directo en el navegador (no se fuerza la descarga)
        self.assertTrue(pdf["Content-Disposition"].startswith("inline"))
        self.assertIn("cotizacion-%s.pdf" % cot.numero, pdf["Content-Disposition"])

    def test_vendedor_no_ve_cotizacion_ajena(self):
        cot = servicios.crear_cotizacion(self.luis, [self.item(self.p1, None, 1)])
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(reverse("panel:cotizacion_pdf", args=[cot.pk])).status_code, 404)


class ReciboTests(Base):
    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 50, self.admin)

    def test_recibo_pdf_de_una_sola_hoja(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)], cliente="Rosa")
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:venta_recibo", args=[v.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertIn("recibo-%05d.pdf" % v.pk, r["Content-Disposition"])
        self.assertEqual(r.content.count(b"/Type /Page\n"), 1)

    def test_recibo_con_muchisimos_productos_sigue_siendo_una_hoja(self):
        productos = [Producto.objects.create(nombre=f"Juguete {i}", precio=D("7")) for i in range(80)]
        v = servicios.registrar_venta(self.ana, [self.item(p, self.casa, 1) for p in productos])
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:venta_recibo", args=[v.pk]))
        self.assertEqual(r.content.count(b"/Type /Page\n"), 1)

    def test_recibo_de_venta_anulada(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)])
        servicios.anular_venta(v, self.admin)
        self.client.login(username="admin", password="x")
        self.assertEqual(self.client.get(reverse("panel:venta_recibo", args=[v.pk])).status_code, 200)

    def test_recibo_ajeno_no_se_ve(self):
        v = servicios.registrar_venta(self.luis, [self.item(self.p1, self.casa, 1)])
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(reverse("panel:venta_recibo", args=[v.pk])).status_code, 404)
        self.client.login(username="admin", password="x")
        self.assertEqual(self.client.get(reverse("panel:venta_recibo", args=[v.pk])).status_code, 200)

    def test_boton_de_recibo_en_el_detalle(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:venta_detalle", args=[v.pk]))
        self.assertContains(r, "Abrir recibo de respaldo")
        self.assertContains(r, 'target="_blank"')  # se abre en otra pestaña, sin descargar

    def test_documentos_solo_muestran_telefonos_de_contacto_no_whatsapp(self):
        from catalogo.models import ConfiguracionSitio
        from .pdf import _contacto_pie, lineas_contacto
        c = ConfiguracionSitio.get()
        c.telefono, c.telefono_2 = "72345678", "62211111"
        c.whatsapp_numero, c.whatsapp_etiqueta = "59171111111", "Ventas"
        c.whatsapp_numero_2 = "59172222222"
        c.email_contacto = "hola@neuromisael.com"
        c.save()
        cabecera = " ".join(lineas_contacto(c))
        self.assertIn("72345678", cabecera)
        self.assertIn("62211111", cabecera)
        self.assertIn("hola@neuromisael.com", cabecera)
        self.assertNotIn("WhatsApp", cabecera)
        self.assertNotIn("59171111111", cabecera)
        pie = " ".join(_contacto_pie(c))
        for repetido in ("72345678", "62211111", "hola@neuromisael.com", "WhatsApp", "5917"):
            self.assertNotIn(repetido, pie)  # el pie no repite lo que ya está arriba

    def test_si_no_hay_telefonos_los_documentos_usan_los_numeros_de_whatsapp(self):
        from catalogo.models import ConfiguracionSitio
        from .pdf import lineas_contacto
        c = ConfiguracionSitio.get()
        c.telefono = c.telefono_2 = ""
        c.whatsapp_numero = "59171111111"
        c.save()
        self.assertIn("+59171111111", " ".join(lineas_contacto(c)))

    def test_pdfs_con_dos_telefonos_y_dos_whatsapp(self):
        from catalogo.models import ConfiguracionSitio
        c = ConfiguracionSitio.get()
        c.telefono, c.telefono_2 = "72345678", "62211111"
        c.whatsapp_numero, c.whatsapp_etiqueta = "59171234567", "Ventas"
        c.whatsapp_numero_2, c.whatsapp_etiqueta_2 = "59176543210", "Consultas"
        c.email_contacto = "una.direccion.de.correo.bastante.larga@neuromisael.com"
        c.save()
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1)])
        from .pdf import generar_pdf_cotizacion, generar_pdf_recibo
        self.assertTrue(generar_pdf_cotizacion(cot).startswith(b"%PDF"))
        self.assertTrue(generar_pdf_recibo(v).startswith(b"%PDF"))

    def test_pdf_cotizacion_sigue_funcionando_con_logo_grande(self):
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 3)])
        from .pdf import generar_pdf_cotizacion
        self.assertTrue(generar_pdf_cotizacion(cot).startswith(b"%PDF"))


class AccesoPublicoTests(Base):
    def test_la_pagina_principal_tiene_acceso_al_panel(self):
        r = self.client.get("/")
        self.assertContains(r, ">Acceder</a>")
        self.assertContains(r, 'href="/panel/"')
        self.assertNotContains(r, "🔑")
        self.assertNotContains(r, "Vendedores")

    def test_el_acceso_lleva_al_login_y_luego_al_panel(self):
        r = self.client.get("/panel/")
        self.assertRedirects(r, "/panel/login/?next=/panel/")
        r = self.client.post("/panel/login/?next=/panel/", {"username": "ana", "password": "x"})
        self.assertRedirects(r, "/panel/", fetch_redirect_response=False)


class UsuariosTests(Base):
    def test_crear_usuario_con_clave_corta_y_nombre_libre(self):
        self.client.login(username="admin", password="x")
        self.client.post(reverse("panel:usuarios"), {"username": "María José 2", "password": "1"})
        u = User.objects.get(username="María José 2")
        self.assertFalse(u.is_staff)
        self.assertTrue(self.client.login(username="María José 2", password="1"))

    def test_clave_larguisima(self):
        self.client.login(username="admin", password="x")
        clave = "ñ" * 500
        self.client.post(reverse("panel:usuarios"), {"username": "largo", "password": clave})
        self.assertTrue(self.client.login(username="largo", password=clave))

    def test_no_duplicados(self):
        self.client.login(username="admin", password="x")
        self.client.post(reverse("panel:usuarios"), {"username": "ANA", "password": "z"})
        self.assertEqual(User.objects.filter(username__iexact="ana").count(), 1)

    def test_cambiar_clave_y_desactivar(self):
        self.client.login(username="admin", password="x")
        self.client.post(reverse("panel:usuario_clave", args=[self.ana.pk]), {"password": "nueva"})
        self.assertTrue(self.client.login(username="ana", password="nueva"))
        self.client.login(username="admin", password="x")
        self.client.post(reverse("panel:usuario_activar", args=[self.ana.pk]))
        self.assertFalse(self.client.login(username="ana", password="nueva"))

    def test_bloqueo_por_intentos(self):
        for _ in range(8):
            self.client.post(reverse("panel:login"), {"username": "ana", "password": "mal"})
        r = self.client.post(reverse("panel:login"), {"username": "ana", "password": "x"})
        self.assertContains(r, "Demasiados intentos")
        self.assertNotIn("_auth_user_id", self.client.session)


class FiltrosPanelTests(Base):
    """Los filtros del panel se generan solos a partir de lo que el admin crea (igual que en el sitio público)."""

    def setUp(self):
        super().setUp()
        self.material = Atributo.objects.create(nombre="Material", orden=1)
        self.pla = ValorAtributo.objects.create(atributo=self.material, valor="PLA")
        self.petg = ValorAtributo.objects.create(atributo=self.material, valor="PETG")
        self.sin_uso = ValorAtributo.objects.create(atributo=self.material, valor="Madera")  # nadie lo usa
        self.color = Atributo.objects.create(nombre="Color", orden=2)
        self.azul = ValorAtributo.objects.create(atributo=self.color, valor="Azul", color_hex="#3B82F6")
        self.interno = Atributo.objects.create(nombre="Interno", orden=3, mostrar_como_filtro=False)
        self.val_interno = ValorAtributo.objects.create(atributo=self.interno, valor="Lote 7")
        self.cat_vacia = Categoria.objects.create(nombre="Sin productos")
        self.p1.atributos.add(self.pla, self.azul, self.val_interno)
        self.p2.atributos.add(self.petg)
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        self.client.login(username="ana", password="x")

    def test_stock_muestra_filtros_generados(self):
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, "data-filtros")
        self.assertContains(r, "Táctil")                      # categoría con productos
        self.assertNotContains(r, "Sin productos")            # sin productos: no hay filtro fantasma
        self.assertContains(r, "Material")
        for valor in ("PLA", "PETG", "Azul"):
            self.assertContains(r, valor)
        self.assertNotContains(r, "Madera")                   # valor que ningún producto usa
        self.assertNotContains(r, "Lote 7")                   # atributo oculto de los filtros
        self.assertContains(r, 'style="background-color: #3B82F6;"')   # puntito de color
        self.assertContains(r, "Hay en Casa")                 # filtro por lugar

    def test_tarjetas_de_stock_traen_datos_para_filtrar(self):
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, f'data-categoria="{self.cat.pk}"')
        self.assertContains(r, f' {self.pla.pk} ')
        self.assertContains(r, 'data-precio="10"')
        self.assertContains(r, f'data-u{self.casa.pk}="5"')
        self.assertContains(r, 'data-total="5"')

    def test_vender_y_cotizar_tienen_los_mismos_filtros(self):
        for nombre in ("panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            self.assertContains(r, "data-filtros", msg_prefix=nombre)
            self.assertContains(r, "PETG", msg_prefix=nombre)
            datos = {p["nombre"]: p for p in r.context["productos_json"]}
            self.assertEqual(datos["Pop it"]["categoria"], str(self.cat.pk))
            self.assertCountEqual(datos["Pop it"]["valores"], [self.pla.pk, self.azul.pk, self.val_interno.pk])
            self.assertEqual(datos["Cubo"]["categoria"], "")

    def test_el_selector_sigue_sin_consultas_por_producto(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.get(reverse("panel:venta_nueva"))
        with CaptureQueriesContext(connection) as pocas:
            self.client.get(reverse("panel:venta_nueva"))
        for i in range(12):
            extra = Producto.objects.create(nombre=f"Extra {i}", precio=D("5"), categoria=self.cat)
            extra.atributos.add(self.pla)
        with CaptureQueriesContext(connection) as muchas:
            self.client.get(reverse("panel:venta_nueva"))
        self.assertEqual(len(pocas), len(muchas))


class EnlacesPaginaPrincipalTests(Base):
    def test_el_panel_tiene_enlace_a_la_pagina_principal(self):
        self.client.login(username="ana", password="x")
        for nombre in ("panel:stock", "panel:venta_nueva", "panel:ventas", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            self.assertContains(r, 'href="/"', msg_prefix=nombre)
            self.assertContains(r, "Página principal", msg_prefix=nombre)

    def test_el_login_tiene_enlace_para_volver(self):
        r = self.client.get(reverse("panel:login"))
        self.assertContains(r, "Volver a la página principal")
        self.assertContains(r, 'href="/"')


class PlantillasLimpiasTests(Base):
    def test_ninguna_pantalla_muestra_codigo_de_plantilla(self):
        """Un comentario {# #} de varias líneas se vería escrito en pantalla: que no vuelva a pasar."""
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1)])
        self.client.login(username="admin", password="x")
        urls = [reverse(n) for n in ("panel:stock", "panel:venta_nueva", "panel:cotizacion_nueva", "panel:ventas",
                                     "panel:cotizaciones", "panel:usuarios")]
        urls += [reverse("panel:stock_producto", args=[self.p1.pk]), reverse("panel:venta_detalle", args=[v.pk]),
                 reverse("panel:cotizacion_detalle", args=[cot.pk])]
        for url in urls:
            html = self.client.get(url).content.decode()
            self.assertNotIn("{#", html, url)
            self.assertNotIn("#}", html, url)
            self.assertNotIn("{%", html, url)
        self.client.logout()
        self.assertNotIn("{#", self.client.get(reverse("panel:login")).content.decode())


class TarjetasPanelTests(Base):
    """Las tarjetas del panel son compactas: foto, código, nombre, precio y stock (sin descripción)."""

    def setUp(self):
        super().setUp()
        self.p1.descripcion_corta = "Cubo con texturas para explorar con las manos"
        self.p1.largo_cm = D("8"); self.p1.ancho_cm = D("8"); self.p1.alto_cm = D("8")
        self.p1.save()
        self.client.login(username="ana", password="x")

    def test_el_panel_no_muestra_descripcion_ni_medidas(self):
        for nombre in ("panel:stock", "panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            self.assertNotContains(r, "Cubo con texturas para explorar con las manos", msg_prefix=nombre)
            self.assertNotContains(r, "8 × 8 × 8 cm", msg_prefix=nombre)
            self.assertNotContains(r, "p-prod-desc", msg_prefix=nombre)

    def test_las_tarjetas_muestran_el_codigo(self):
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, f'<span class="p-prod-cod">{self.p1.codigo}</span>')
        self.assertContains(r, f'data-codigo="{self.p1.codigo}"')

    def test_el_selector_trae_el_codigo_y_no_la_descripcion(self):
        for nombre in ("panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            datos = {p["nombre"]: p for p in r.context["productos_json"]}
            self.assertEqual(datos["Pop it"]["codigo"], self.p1.codigo)
            self.assertNotIn("desc", datos["Pop it"])
            self.assertNotIn("medidas", datos["Pop it"])

    def test_el_css_vuelve_a_tres_tarjetas_por_fila_en_celular(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / "static" / "css" / "panel.css").read_text(encoding="utf-8")
        self.assertIn(".p-productos.vista-tarjetas { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", css)

    def test_los_detalles_muestran_el_codigo(self):
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1)])
        for url in (reverse("panel:stock_producto", args=[self.p1.pk]), reverse("panel:venta_detalle", args=[v.pk]),
                    reverse("panel:cotizacion_detalle", args=[cot.pk])):
            self.assertContains(self.client.get(url), self.p1.codigo, msg_prefix=url)

    def test_el_recibo_pdf_incluye_el_codigo(self):
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        from .pdf import generar_pdf_recibo
        self.assertTrue(generar_pdf_recibo(v).startswith(b"%PDF"))

    def test_el_selector_sigue_sin_consultas_por_producto(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for url in ("panel:venta_nueva", "panel:stock"):
            self.client.get(reverse(url))
            with CaptureQueriesContext(connection) as pocas:
                self.client.get(reverse(url))
            for i in range(8):
                Producto.objects.create(nombre=f"Más {url} {i}", precio=D("5"), categoria=self.cat)
            with CaptureQueriesContext(connection) as muchas:
                self.client.get(reverse(url))
            self.assertEqual(len(pocas), len(muchas), url)


class CssValidoTests(Base):
    def test_las_llaves_de_los_css_estan_balanceadas(self):
        """Una '}' suelta hace que el navegador descarte la regla siguiente (ya pasó una vez)."""
        import re
        from pathlib import Path
        from django.conf import settings
        for nombre in ("panel.css", "style.css"):
            css = (Path(settings.BASE_DIR) / "static" / "css" / nombre).read_text(encoding="utf-8")
            css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
            css = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", css)  # textos entre comillas
            profundidad = 0
            for i, c in enumerate(css):
                profundidad += (c == "{") - (c == "}")
                self.assertGreaterEqual(profundidad, 0, f"{nombre}: '}}' sobrante cerca de: {css[max(0, i - 60):i + 1]!r}")
            self.assertEqual(profundidad, 0, f"{nombre}: falta cerrar {profundidad} llave(s)")


def _png(color, ancho=300, alto=200):
    """Una imagen de prueba (no cuadrada, para comprobar el recorte)."""
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (ancho, alto), color).save(buf, "PNG")
    return buf.getvalue()


class FotosEnCotizacionTests(Base):
    """La cotización lleva una foto pequeña de cada producto como referencia para el cliente."""

    def setUp(self):
        import tempfile
        from django.test import override_settings
        super().setUp()
        self._media = tempfile.mkdtemp()
        self._ov = override_settings(MEDIA_ROOT=self._media)
        self._ov.enable()
        self.addCleanup(self._ov.disable)
        self.addCleanup(__import__("shutil").rmtree, self._media, True)

    def con_foto(self, producto, color, **kw):
        from django.core.files.base import ContentFile
        from catalogo.models import ImagenProducto
        ImagenProducto.objects.create(producto=producto, imagen=ContentFile(_png(color, **kw), name=f"{producto.slug}.png"))

    def imagenes_base(self):
        """Imágenes que tiene un PDF sin fotos de productos: solo el logo (un PNG con transparencia
        cuenta como 2 objetos dentro del PDF). Se mide en vez de suponerlo, por si cambias el logo."""
        sin_foto = Producto.objects.create(nombre="Sin foto (base)", precio=D("1"))
        return self.cotizacion(sin_foto).count(b"/Subtype /Image")

    def cotizacion(self, *productos):
        cot = servicios.crear_cotizacion(self.ana, [self.item(p, None, 1) for p in productos])
        from .pdf import generar_pdf_cotizacion
        return generar_pdf_cotizacion(cot)

    def test_sin_fotos_no_hay_columna_de_fotos(self):
        pdf = self.cotizacion(self.p1, self.p2)
        self.assertEqual(pdf.count(b"/Subtype /Image"), self.imagenes_base())  # solo el logo

    def test_cada_producto_con_foto_la_lleva_incrustada(self):
        p3 = Producto.objects.create(nombre="Tercero", precio=D("5"))
        base = self.imagenes_base()
        for p, color in ((self.p1, "red"), (self.p2, "blue"), (p3, "green")):
            self.con_foto(p, color)
        self.assertEqual(self.cotizacion(self.p1, self.p2, p3).count(b"/Subtype /Image"), base + 3)

    def test_producto_sin_foto_entre_otros_con_foto_lleva_un_cuadrito_claro(self):
        base = self.imagenes_base()
        self.con_foto(self.p1, "red")
        # logo + foto del p1 + marcador del p2
        self.assertEqual(self.cotizacion(self.p1, self.p2).count(b"/Subtype /Image"), base + 1 + 1)

    def test_la_miniatura_es_cuadrada_liviana_y_jpeg(self):
        from io import BytesIO
        from PIL import Image
        from .pdf import FOTO_PX, _a_miniatura
        for ancho, alto in ((800, 600), (600, 800), (500, 500)):
            datos = _a_miniatura(_png("orange", ancho, alto))
            img = Image.open(BytesIO(datos))
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (FOTO_PX, FOTO_PX))
            self.assertLess(len(datos), 20_000)

    def test_una_foto_que_falla_no_rompe_el_pdf(self):
        from unittest import mock
        base = self.imagenes_base()
        self.con_foto(self.p1, "red"); self.con_foto(self.p2, "blue")
        with mock.patch("inventario.pdf._origen_foto", return_value=("url", "http://no-existe.invalid/x.jpg")), \
             mock.patch("inventario.pdf._descargar", side_effect=OSError("sin internet")):
            pdf = self.cotizacion(self.p1, self.p2)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Subtype /Image"), base)  # sin ninguna foto: solo el logo

    def test_una_foto_dañada_no_rompe_el_pdf(self):
        from django.core.files.base import ContentFile
        from catalogo.models import ImagenProducto
        ImagenProducto.objects.create(producto=self.p1, imagen=ContentFile(b"esto no es una imagen", name="rota.png"))
        self.assertTrue(self.cotizacion(self.p1).startswith(b"%PDF"))

    def test_las_fotos_en_la_nube_se_piden_ya_recortadas_y_en_jpg(self):
        from .pdf import _url_miniatura
        url = _url_miniatura("https://res.cloudinary.com/demo/image/upload/v1/productos/a.png")
        self.assertIn("w_180,h_180,c_fill", url)
        self.assertIn("f_jpg", url)

    def test_la_descarga_en_la_nube_funciona_y_va_en_paralelo(self):
        from unittest import mock
        from .pdf import miniaturas_de
        productos = [Producto.objects.create(nombre=f"Nube {i}", precio=D("5")) for i in range(5)]
        llamadas = []
        def falsa(url):
            llamadas.append(url)
            return _png("purple", 640, 480)
        with mock.patch("inventario.pdf._origen_foto", side_effect=lambda p: ("url", f"https://x/{p.pk}.jpg")), \
             mock.patch("inventario.pdf._descargar", side_effect=falsa):
            fotos = miniaturas_de(productos)
        self.assertEqual(sorted(fotos), sorted(p.pk for p in productos))
        self.assertEqual(len(llamadas), 5)

    def test_hasta_8_productos_con_foto_caben_en_una_hoja(self):
        productos = [Producto.objects.create(nombre=f"Producto {i}", precio=D("7")) for i in range(8)]
        for i, p in enumerate(productos):
            self.con_foto(p, ("red", "blue", "green", "orange")[i % 4])
        pdf = self.cotizacion(*productos)
        self.assertEqual(pdf.count(b"/Type /Page\n"), 1)

    def test_el_recibo_de_respaldo_sigue_sin_fotos(self):
        base = self.imagenes_base()
        self.con_foto(self.p1, "red")
        servicios.registrar_ingreso(self.p1, self.casa, 5, self.admin)
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)])
        from .pdf import generar_pdf_recibo
        self.assertEqual(generar_pdf_recibo(v).count(b"/Subtype /Image"), base)  # solo el logo


class BuscarPorCodigoEnElPanelTests(Base):
    """El código se puede buscar en ventas, cotizaciones y en el admin."""

    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 50, self.admin)
        servicios.registrar_ingreso(self.p2, self.casa, 50, self.admin)
        self.v1 = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)], cliente="María")
        self.v2 = servicios.registrar_venta(self.ana, [self.item(self.p2, self.casa, 1)], cliente="Pedro")
        self.v3 = servicios.registrar_venta(
            self.ana, [self.item(self.p1, self.casa, 1), self.item(self.p2, self.casa, 1)], cliente="Luz")
        self.c1 = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1)], cliente="Escuela")
        self.c2 = servicios.crear_cotizacion(self.ana, [self.item(self.p2, None, 3)], cliente="Colegio")
        self.client.login(username="admin", password="x")

    def ids(self, r, lista):
        return sorted(o.pk for o in r.context[lista])

    def test_ventas_se_busca_por_codigo(self):
        r = self.client.get(reverse("panel:ventas"), {"q": self.p1.codigo})
        self.assertEqual(self.ids(r, "ventas"), sorted([self.v1.pk, self.v3.pk]))

    def test_ventas_se_busca_por_nombre_cliente_y_numero(self):
        url = reverse("panel:ventas")
        self.assertEqual(self.ids(self.client.get(url, {"q": "cubo"}), "ventas"), sorted([self.v2.pk, self.v3.pk]))
        self.assertEqual(self.ids(self.client.get(url, {"q": "mar"}), "ventas"), [self.v1.pk])   # cliente "María"
        self.assertEqual(self.ids(self.client.get(url, {"q": f"#{self.v2.pk}"}), "ventas"), [self.v2.pk])

    def test_la_busqueda_en_ventas_no_duplica_los_totales(self):
        # v3 tiene los dos productos: coincide por ambos, pero cuenta una sola vez
        r = self.client.get(reverse("panel:ventas"), {"q": "J00"})
        self.assertEqual(r.context["resumen"]["n"], 3)
        self.assertEqual(r.context["resumen"]["total"], self.v1.total + self.v2.total + self.v3.total)

    def test_ventas_busqueda_sin_resultados_ofrece_quitarla(self):
        r = self.client.get(reverse("panel:ventas"), {"q": "ZZZ999"})
        self.assertContains(r, "Quitar la búsqueda")

    def test_cotizaciones_se_busca_por_codigo_y_cliente(self):
        url = reverse("panel:cotizaciones")
        self.assertEqual(self.ids(self.client.get(url, {"q": self.p2.codigo}), "cotizaciones"), [self.c2.pk])
        self.assertEqual(self.ids(self.client.get(url, {"q": "escuela"}), "cotizaciones"), [self.c1.pk])
        self.assertEqual(self.ids(self.client.get(url), "cotizaciones"), sorted([self.c1.pk, self.c2.pk]))

    def test_el_vendedor_solo_encuentra_lo_suyo(self):
        otro = servicios.registrar_venta(self.luis, [self.item(self.p1, self.casa, 1)])
        self.client.login(username="luis", password="x")
        r = self.client.get(reverse("panel:ventas"), {"q": self.p1.codigo})
        self.assertEqual(self.ids(r, "ventas"), [otro.pk])

    def test_las_listas_muestran_los_codigos_de_cada_documento(self):
        r = self.client.get(reverse("panel:ventas"))
        self.assertContains(r, f'<span class="p-codigos">{self.p1.codigo} · {self.p2.codigo}</span>')
        r = self.client.get(reverse("panel:cotizaciones"))
        self.assertContains(r, f'<span class="p-codigos">{self.p1.codigo}</span>')

    def test_el_resumen_de_codigos_se_acorta_con_muchos_productos(self):
        productos = [Producto.objects.create(nombre=f"Extra {i}", precio=D("3")) for i in range(7)]
        v = servicios.registrar_venta(self.ana, [self.item(p, None, 1) for p in productos])
        v = Venta.objects.prefetch_related("items__producto").get(pk=v.pk)
        self.assertTrue(v.codigos_resumen.endswith("+3"))
        self.assertEqual(v.codigos_resumen.count("·"), 3)

    def test_las_listas_no_hacen_consultas_por_cada_venta(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        url = reverse("panel:ventas")
        self.client.get(url)
        with CaptureQueriesContext(connection) as pocas:
            self.client.get(url)
        for _ in range(6):
            servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1), self.item(self.p2, self.casa, 1)])
        with CaptureQueriesContext(connection) as muchas:
            self.client.get(url)
        self.assertEqual(len(pocas), len(muchas))

    def test_el_admin_busca_ventas_y_cotizaciones_por_codigo(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_superuser("root", "r@r.com", "x")
        self.client.login(username="root", password="x")
        r = self.client.get("/admin/inventario/venta/", {"q": self.p2.codigo})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["cl"].result_count, 2)   # v2 y v3
        r = self.client.get("/admin/inventario/cotizacion/", {"q": self.p2.codigo})
        self.assertEqual(r.context["cl"].result_count, 1)

    def test_el_pdf_de_cotizacion_lleva_el_codigo_junto_al_nombre(self):
        from .pdf import _nombre_con_codigo, generar_pdf_cotizacion
        texto = _nombre_con_codigo(self.p1)
        self.assertIn("Pop it", texto)
        self.assertIn(self.p1.codigo, texto)
        self.assertTrue(generar_pdf_cotizacion(self.c1).startswith(b"%PDF"))

    def test_producto_sin_codigo_no_rompe_el_pdf(self):
        from .pdf import _nombre_con_codigo
        Producto.objects.filter(pk=self.p2.pk).update(codigo=None)
        self.p2.refresh_from_db()
        self.assertEqual(_nombre_con_codigo(self.p2), "Cubo")


class OrdenBusquedaYNumeracionTests(Base):
    """Listas de Ventas/Cotizaciones con orden y fechas; PDFs con productos numerados."""

    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 50, self.admin)
        servicios.registrar_ingreso(self.p2, self.casa, 50, self.admin)
        self.v1 = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1)], cliente="Zoe")
        self.v2 = servicios.registrar_venta(self.luis, [self.item(self.p2, self.casa, 3)], cliente="Ana María")
        self.v3 = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)], cliente="Mario")
        self.c1 = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1)], cliente="Zoe")
        self.c2 = servicios.crear_cotizacion(self.luis, [self.item(self.p2, None, 5)], cliente="Beto")
        self.client.login(username="admin", password="x")

    def ids(self, r, clave):
        return [o.pk for o in r.context[clave]]

    def test_orden_por_defecto_es_mas_reciente_primero(self):
        r = self.client.get(reverse("panel:ventas"))
        self.assertEqual(self.ids(r, "ventas"), [self.v3.pk, self.v2.pk, self.v1.pk])

    def test_ventas_ordenan_por_fecha_monto_cliente_y_vendedor(self):
        url = reverse("panel:ventas")
        casos = {
            "antiguos": [self.v1.pk, self.v2.pk, self.v3.pk],
            "numero": [self.v1.pk, self.v2.pk, self.v3.pk],
            "total_desc": [self.v2.pk, self.v3.pk, self.v1.pk],   # 60, 20, 10
            "total_asc": [self.v1.pk, self.v3.pk, self.v2.pk],
            "cliente": [self.v2.pk, self.v3.pk, self.v1.pk],      # Ana María, Mario, Zoe
            "usuario": [self.v3.pk, self.v1.pk, self.v2.pk],      # ana (más nueva primero), luis
        }
        for orden, esperado in casos.items():
            with self.subTest(orden=orden):
                self.assertEqual(self.ids(self.client.get(url, {"orden": orden}), "ventas"), esperado)

    def test_orden_invalido_cae_en_el_por_defecto(self):
        r = self.client.get(reverse("panel:ventas"), {"orden": "'; drop table"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["f"]["orden"], "recientes")

    def test_cotizaciones_ordenan_y_filtran_por_fecha(self):
        url = reverse("panel:cotizaciones")
        self.assertEqual(self.ids(self.client.get(url), "cotizaciones"), [self.c2.pk, self.c1.pk])
        self.assertEqual(self.ids(self.client.get(url, {"orden": "cliente"}), "cotizaciones"), [self.c2.pk, self.c1.pk])
        self.assertEqual(self.ids(self.client.get(url, {"orden": "total_desc"}), "cotizaciones"), [self.c2.pk, self.c1.pk])
        hoy = timezone.localdate()
        ayer, manana = hoy - timedelta(days=1), hoy + timedelta(days=1)
        self.assertEqual(len(self.ids(self.client.get(url, {"desde": hoy.isoformat(), "hasta": hoy.isoformat()}), "cotizaciones")), 2)
        self.assertEqual(self.ids(self.client.get(url, {"hasta": ayer.isoformat()}), "cotizaciones"), [])
        self.assertEqual(self.ids(self.client.get(url, {"desde": manana.isoformat()}), "cotizaciones"), [])

    def test_fecha_mal_escrita_no_da_error(self):
        for nombre in ("panel:ventas", "panel:cotizaciones"):
            r = self.client.get(reverse(nombre), {"desde": "ayer", "hasta": "99-99-99"})
            self.assertEqual(r.status_code, 200)

    def test_busca_por_vendedor_codigo_y_nombre(self):
        url = reverse("panel:ventas")
        self.assertEqual(sorted(self.ids(self.client.get(url, {"q": "luis"}), "ventas")), [self.v2.pk])
        self.assertEqual(sorted(self.ids(self.client.get(url, {"q": self.p1.codigo}), "ventas")), sorted([self.v1.pk, self.v3.pk]))
        self.assertEqual(self.ids(self.client.get(url, {"q": "cubo"}), "ventas"), [self.v2.pk])
        r = self.client.get(reverse("panel:cotizaciones"), {"q": "luis"})
        self.assertEqual(self.ids(r, "cotizaciones"), [self.c2.pk])

    def test_admin_filtra_cotizaciones_por_creador_y_vendedor_no_ve_ajenas(self):
        r = self.client.get(reverse("panel:cotizaciones"), {"vendedor": self.luis.pk})
        self.assertEqual(self.ids(r, "cotizaciones"), [self.c2.pk])
        self.client.login(username="ana", password="x")
        r = self.client.get(reverse("panel:cotizaciones"), {"vendedor": self.luis.pk})
        self.assertEqual(self.ids(r, "cotizaciones"), [self.c1.pk])

    def test_selector_de_orden_de_productos_en_vender_cotizar_y_stock(self):
        for nombre in ("panel:venta_nueva", "panel:cotizacion_nueva", "panel:stock"):
            self.assertContains(self.client.get(reverse(nombre)), "data-f-orden")

    def _numeros_de_pdf(self, generar, documento):
        """Captura la tabla de productos que se arma para el PDF y devuelve su primera columna."""
        from unittest import mock
        from . import pdf
        capturadas = []
        original = pdf._tabla_items

        def espia(filas, anchos):
            capturadas.append(filas)
            return original(filas, anchos)

        with mock.patch.object(pdf, "_tabla_items", espia):
            self.assertTrue(generar(documento).startswith(b"%PDF"))
        return [fila[0] for fila in capturadas[0]]

    def test_pdf_cotizacion_numera_los_productos(self):
        from .pdf import generar_pdf_cotizacion
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1), self.item(self.p2, None, 2)])
        self.assertEqual(self._numeros_de_pdf(generar_pdf_cotizacion, cot), ["N°", "1", "2"])

    def test_pdf_recibo_numera_los_productos(self):
        from .pdf import generar_pdf_recibo
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1), self.item(self.p2, self.casa, 2)])
        self.assertEqual(self._numeros_de_pdf(generar_pdf_recibo, v), ["N°", "1", "2", ])


class DetalleEnTablaNumeradaTests(Base):
    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 50, self.admin)
        servicios.registrar_ingreso(self.p2, self.casa, 50, self.admin)
        self.client.login(username="admin", password="x")

    def test_detalle_de_cotizacion_es_tabla_numerada(self):
        cot = servicios.crear_cotizacion(self.ana, [self.item(self.p1, None, 1), self.item(self.p2, None, 2)])
        r = self.client.get(reverse("panel:cotizacion_detalle", args=[cot.pk]))
        self.assertContains(r, '<table class="p-tabla">')
        self.assertContains(r, '<td class="n">1</td>')
        self.assertContains(r, '<td class="n">2</td>')
        self.assertContains(r, "Pop it")
        self.assertContains(r, "Cubo")

    def test_detalle_de_venta_es_tabla_numerada(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 1), self.item(self.p2, self.casa, 3)])
        r = self.client.get(reverse("panel:venta_detalle", args=[v.pk]))
        self.assertContains(r, '<table class="p-tabla">')
        self.assertContains(r, '<td class="n">1</td>')
        self.assertContains(r, '<td class="n">2</td>')
        self.assertContains(r, "Casa")


def _paginas(pdf):
    return len(re.findall(rb"/Type /Page(?![s\w])", pdf))


class CatalogoPdfTests(Base):
    """Catálogo imprimible (hoja carta) con los productos visibles en el panel."""

    def setUp(self):
        super().setUp()
        self.url = reverse("panel:catalogo_pdf")
        self.client.login(username="ana", password="x")  # un vendedor también puede imprimirlo

    def ids(self, *productos):
        return ",".join(str(p.pk) for p in productos)

    def test_exige_login_y_post(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"ids": self.ids(self.p1)}).status_code, 302)
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_devuelve_un_pdf_carta_en_linea(self):
        r = self.client.post(self.url, {"ids": self.ids(self.p1, self.p2), "diseno": "9", "filtros": "Categoría: Táctil"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn(b"/MediaBox [ 0 0 612 792 ]", r.content)  # carta

    def test_cantidad_de_hojas_segun_el_diseno(self):
        extra = [Producto.objects.create(nombre=f"P{i}", precio=D("5")) for i in range(25)]
        todos = self.ids(self.p1, self.p2, *extra)  # 27 productos
        for clave, d in catalogo_pdf.DISENOS.items():
            r = self.client.post(self.url, {"ids": todos, "diseno": clave})
            esperado = -(-27 // d.por_hoja)
            self.assertEqual(_paginas(r.content), esperado, f"diseño {clave}")
            self.assertEqual(d.por_hoja, int(clave))

    def test_diseno_invalido_usa_el_estandar(self):
        r = self.client.post(self.url, {"ids": self.ids(self.p1), "diseno": "999"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_sin_productos_o_ocultos_no_genera_pdf(self):
        oculto = Producto.objects.create(nombre="Oculto", precio=D("5"), disponible=False)
        for ids in ("", "abc,,-3", self.ids(oculto), "99999"):
            r = self.client.post(self.url, {"ids": ids})
            self.assertEqual(r.status_code, 400, ids)
            self.assertNotEqual(r["Content-Type"], "application/pdf")

    def test_oculto_se_ignora_pero_el_resto_se_imprime(self):
        oculto = Producto.objects.create(nombre="Oculto", precio=D("5"), disponible=False)
        r = self.client.post(self.url, {"ids": self.ids(oculto, self.p1)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_nombres_largos_y_caracteres_raros_no_rompen(self):
        raro = Producto.objects.create(
            nombre="Súper juguete " * 12 + "→ ★", precio=D("12.5"), categoria=self.cat,
            descripcion_corta="Descripción con ñ, tildes y símbolos ✓ ☺ " * 5, recomendado_por_terapeutas=True,
            largo_cm=D("10"), ancho_cm=D("5.5"), alto_cm=D("3"),
        )
        for clave in catalogo_pdf.DISENOS:
            r = self.client.post(self.url, {"ids": self.ids(raro), "diseno": clave})
            self.assertEqual(r.status_code, 200, clave)

    def test_foto_local_se_incrusta(self):
        from PIL import Image
        carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, carpeta, True)
        buf = BytesIO()
        Image.new("RGB", (600, 400), "orange").save(buf, "PNG")
        with self.settings(MEDIA_ROOT=carpeta):
            ImagenProducto.objects.create(producto=self.p1, imagen=ContentFile(buf.getvalue(), name="f.png"))
            con = self.client.post(self.url, {"ids": self.ids(self.p1), "diseno": "9"}).content
            sin = self.client.post(self.url, {"ids": self.ids(self.p2), "diseno": "9"}).content
        # El logo es una imagen en ambos PDFs; la foto del producto es una más solo en el primero
        self.assertEqual(con.count(b"/Subtype /Image"), sin.count(b"/Subtype /Image") + 1)

    def test_no_incluye_stock(self):
        servicios.registrar_ingreso(self.p1, self.casa, 7, self.admin)
        pdf = catalogo_pdf.generar_pdf_catalogo([self.p1], "6")
        self.assertNotIn(b"Casa", pdf)

    def test_botones_y_opciones_en_stock_vender_y_cotizar(self):
        for nombre in ("panel:stock", "panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            self.assertContains(r, "data-imprimir")
            self.assertContains(r, self.url)
            for clave, _ in catalogo_pdf.opciones():
                self.assertContains(r, f'value="{clave}"')
