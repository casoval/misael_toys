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
from .models import Cotizacion, MovimientoStock, PerfilVendedor, ReglaDescuento, Stock, Ubicacion, Venta
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
        self.client.post(reverse("panel:usuarios"), {"username": "María José 2", "password": "1", "ubicaciones": [self.casa.pk]})
        u = User.objects.get(username="María José 2")
        self.assertFalse(u.is_staff)
        self.assertTrue(self.client.login(username="María José 2", password="1"))

    def test_clave_larguisima(self):
        self.client.login(username="admin", password="x")
        clave = "ñ" * 500
        self.client.post(reverse("panel:usuarios"), {"username": "largo", "password": clave, "ubicaciones": [self.casa.pk]})
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
            self.assertContains(r, 'value="categoria"')  # ordenar agrupando por categoría


class UbicacionesDelVendedorTests(Base):
    """Cada vendedor tiene sus ubicaciones: vende desde ellas y ve las demás solo como referencia."""

    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 7, self.admin)
        servicios.registrar_ingreso(self.p1, self.local, 4, self.admin)
        PerfilVendedor.objects.create(usuario=self.ana).ubicaciones.set([self.casa])

    def entrar(self, usuario):
        self.client.force_login(usuario)

    def test_crear_usuario_exige_ubicacion(self):
        self.entrar(self.admin)
        self.client.post(reverse("panel:usuarios"), {"username": "pepe", "password": "1"})
        self.assertFalse(User.objects.filter(username="pepe").exists())
        self.client.post(reverse("panel:usuarios"), {"username": "pepe", "password": "1",
                                                      "ubicaciones": [self.local.pk, self.casa.pk]})
        pepe = User.objects.get(username="pepe")
        self.assertEqual(set(pepe.perfil_vendedor.ubicaciones.all()), {self.local, self.casa})

    def test_admin_cambia_ubicaciones_de_un_usuario(self):
        self.entrar(self.admin)
        self.client.post(reverse("panel:usuario_ubicaciones", args=[self.ana.pk]), {"ubicaciones": [self.local.pk]})
        self.assertEqual(list(self.ana.perfil_vendedor.ubicaciones.all()), [self.local])
        self.client.post(reverse("panel:usuario_ubicaciones", args=[self.ana.pk]), {})  # sin elegir: no cambia
        self.assertEqual(list(self.ana.perfil_vendedor.ubicaciones.all()), [self.local])

    def test_vendedor_no_puede_cambiar_ubicaciones(self):
        self.entrar(self.ana)
        r = self.client.post(reverse("panel:usuario_ubicaciones", args=[self.ana.pk]), {"ubicaciones": [self.local.pk]})
        self.assertEqual(r.status_code, 403)

    def test_stock_muestra_primero_lo_propio_y_las_otras_como_referencia(self):
        self.entrar(self.ana)
        r = self.client.get(reverse("panel:stock"))
        fila = next(f for f in r.context["filas"] if f["producto"] == self.p1)
        self.assertEqual(fila["total"], 7)        # solo Casa
        self.assertEqual(fila["total_otras"], 4)  # Local, referencia
        self.assertEqual([l["propia"] for l in fila["lugares"]], [True, False])
        self.assertContains(r, 'otra">Local <b>4</b>')
        self.assertContains(r, 'data-otras="4"')

    def test_producto_sin_stock_aqui_pero_con_en_otra_ubicacion(self):
        self.entrar(self.ana)
        r = self.client.get(reverse("panel:stock"))
        self.assertNotContains(r, "Agotado</span>\n  </span>")  # p1 tiene en Casa
        PerfilVendedor.objects.filter(usuario=self.ana).first().ubicaciones.set([self.local])
        servicios.ajustar(self.p1, self.local, 0, self.admin)
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, "Sin stock aquí")

    def test_detalle_de_producto_separa_propias_y_otras(self):
        self.entrar(self.ana)
        r = self.client.get(reverse("panel:stock_producto", args=[self.p1.pk]))
        self.assertEqual(r.context["total"], 7)
        self.assertEqual(r.context["total_otras"], 4)
        self.assertContains(r, "En otras ubicaciones")

    def test_admin_y_usuario_sin_asignar_ven_todo_como_propio(self):
        for u in (self.admin, self.luis):  # luis no tiene perfil
            self.entrar(u)
            r = self.client.get(reverse("panel:stock"))
            fila = next(f for f in r.context["filas"] if f["producto"] == self.p1)
            self.assertEqual((fila["total"], fila["total_otras"]), (11, 0))
            self.assertNotContains(r, "solo como referencia")

    def test_formulario_de_venta_marca_las_ubicaciones_propias(self):
        self.entrar(self.ana)
        r = self.client.get(reverse("panel:venta_nueva"))
        marcas = {u["nombre"]: u["propia"] for u in r.context["ubicaciones_json"]}
        self.assertEqual(marcas, {"Casa": True, "Local": False})
        p1 = next(p for p in r.context["productos_json"] if p["id"] == self.p1.pk)
        self.assertEqual((p1["total"], p1["otras"]), (7, 4))

    def test_vender_desde_ubicacion_propia_funciona(self):
        self.entrar(self.ana)
        r = self.client.post(reverse("panel:venta_nueva"), {
            "producto": [self.p1.pk], "ubicacion": [self.casa.pk], "cantidad": ["2"], "precio": ["10"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.stock(self.p1, self.casa), 5)

    def test_vender_desde_ubicacion_ajena_se_rechaza_en_el_servidor(self):
        self.entrar(self.ana)
        r = self.client.post(reverse("panel:venta_nueva"), {
            "producto": [self.p1.pk], "ubicacion": [self.local.pk], "cantidad": ["1"], "precio": ["10"]})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "no puedes vender desde Local")
        self.assertEqual(self.stock(self.p1, self.local), 4)
        self.assertEqual(Venta.objects.count(), 0)

    def test_vender_sin_descontar_sigue_permitido(self):
        self.entrar(self.ana)
        r = self.client.post(reverse("panel:venta_nueva"), {
            "producto": [self.p1.pk], "ubicacion": [""], "cantidad": ["1"], "precio": ["10"]})
        self.assertEqual(r.status_code, 302)

    def test_admin_puede_vender_desde_cualquier_ubicacion(self):
        self.entrar(self.admin)
        r = self.client.post(reverse("panel:venta_nueva"), {
            "producto": [self.p1.pk], "ubicacion": [self.local.pk], "cantidad": ["1"], "precio": ["10"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.stock(self.p1, self.local), 3)


class CargaMasivaTests(Base):
    def post(self, datos, **extra):
        self.client.force_login(self.admin)
        return self.client.post(reverse("panel:stock_carga"), {**datos, **extra})

    def test_solo_admin(self):
        self.client.force_login(self.ana)
        self.assertEqual(self.client.get(reverse("panel:stock_carga")).status_code, 403)
        self.assertEqual(self.client.post(reverse("panel:stock_carga"), {}).status_code, 403)

    def test_la_tabla_trae_todos_los_productos_y_ubicaciones(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:stock_carga"))
        self.assertEqual(r.status_code, 200)
        for p in (self.p1, self.p2):
            for u in (self.casa, self.local):
                self.assertContains(r, f'name="c_{p.pk}_{u.pk}"')

    def test_sumar_varios_productos_y_ubicaciones_de_una_vez(self):
        servicios.registrar_ingreso(self.p1, self.casa, 2, self.admin)
        r = self.post({"modo": "sumar", "nota": "lote 1",
                       f"c_{self.p1.pk}_{self.casa.pk}": "5", f"c_{self.p1.pk}_{self.local.pk}": "3",
                       f"c_{self.p2.pk}_{self.casa.pk}": "10", f"c_{self.p2.pk}_{self.local.pk}": ""})
        self.assertRedirects(r, reverse("panel:stock"))
        self.assertEqual(self.stock(self.p1, self.casa), 7)
        self.assertEqual(self.stock(self.p1, self.local), 3)
        self.assertEqual(self.stock(self.p2, self.casa), 10)
        self.assertEqual(self.stock(self.p2, self.local), 0)
        m = MovimientoStock.objects.filter(producto=self.p2).get()
        self.assertEqual((m.tipo, m.usuario), (MovimientoStock.INICIAL, self.admin))
        self.assertIn("lote 1", m.nota)
        self.assertEqual(MovimientoStock.objects.filter(producto=self.p1, tipo=MovimientoStock.INGRESO).count(), 1)

    def test_sumar_compensa_stock_negativo(self):
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 3)])
        self.assertEqual(self.stock(self.p1, self.casa), -3)
        self.post({"modo": "sumar", f"c_{self.p1.pk}_{self.casa.pk}": "10"})
        self.assertEqual(self.stock(self.p1, self.casa), 7)

    def test_fijar_cantidad_real(self):
        servicios.registrar_ingreso(self.p1, self.casa, 8, self.admin)
        servicios.registrar_ingreso(self.p2, self.casa, 4, self.admin)
        r = self.post({"modo": "fijar", f"c_{self.p1.pk}_{self.casa.pk}": "5",
                       f"c_{self.p2.pk}_{self.casa.pk}": "4", f"c_{self.p2.pk}_{self.local.pk}": "0"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.stock(self.p1, self.casa), 5)
        self.assertEqual(self.stock(self.p2, self.casa), 4)  # misma cantidad: sin cambio, sin error
        self.assertEqual(MovimientoStock.objects.filter(producto=self.p2, tipo=MovimientoStock.AJUSTE).count(), 0)

    def test_un_dato_invalido_no_guarda_nada(self):
        r = self.post({"modo": "sumar", f"c_{self.p1.pk}_{self.casa.pk}": "5", f"c_{self.p2.pk}_{self.casa.pk}": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No se guardó nada")
        self.assertContains(r, 'value="5"')  # conserva lo escrito
        self.assertEqual(self.stock(self.p1, self.casa), 0)

    def test_sumar_no_acepta_cero_ni_negativos(self):
        for malo in ("0", "-2"):
            r = self.post({"modo": "sumar", f"c_{self.p1.pk}_{self.casa.pk}": malo})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock(self.p1, self.casa), 0)

    def test_sin_escribir_nada_avisa(self):
        r = self.post({"modo": "sumar"})
        self.assertContains(r, "No escribiste ninguna cantidad")

    def test_ignora_claves_falsas(self):
        r = self.post({"modo": "sumar", "c_999_999": "5", "c_x_y": "2", f"c_{self.p1.pk}_{self.casa.pk}": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.stock(self.p1, self.casa), 1)

    def test_boton_de_carga_solo_para_admin(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("panel:stock")), reverse("panel:stock_carga"))
        self.client.force_login(self.ana)
        self.assertNotContains(self.client.get(reverse("panel:stock")), reverse("panel:stock_carga"))


class VistasPorUbicacionTests(Base):
    def setUp(self):
        super().setUp()
        servicios.registrar_ingreso(self.p1, self.casa, 7, self.admin)
        servicios.registrar_ingreso(self.p1, self.local, 2, self.admin)
        servicios.registrar_ingreso(self.p2, self.local, 5, self.admin)
        PerfilVendedor.objects.create(usuario=self.ana).ubicaciones.set([self.casa])
        self.client.force_login(self.ana)

    def test_resumen_por_ubicacion(self):
        r = self.client.get(reverse("panel:stock"))
        res = {x["ubicacion"].nombre: x for x in r.context["resumen_ub"]}
        self.assertEqual((res["Casa"]["unidades"], res["Casa"]["productos"], res["Casa"]["propia"]), (7, 1, True))
        self.assertEqual((res["Local"]["unidades"], res["Local"]["productos"], res["Local"]["propia"]), (7, 2, False))
        self.assertContains(r, "data-r-lugar")

    def test_stock_bajo_es_exactamente_una_unidad(self):
        js = open("static/js/panel_vista.js", encoding="utf-8").read()
        self.assertIn("bajo: (t, o) => t === 1,", js)
        self.assertContains(self.client.get(reverse("panel:stock")), "Stock bajo (queda 1 unidad)")

    def test_hay_filtro_de_estado_y_vista_tabla(self):
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, "data-f-estado")
        self.assertContains(r, "Sin stock aquí, pero hay en otra ubicación")
        self.assertContains(r, 'data-vista="tabla"')
        self.assertContains(r, "data-tabla-contenedor")
        self.assertContains(r, f'data-id="{self.p2.pk}"')

    def test_tarjetas_y_filas_traen_datos_para_filtrar_por_estado(self):
        r = self.client.get(reverse("panel:stock"))
        # Cubo: nada en Casa, 5 en Local → total 0, otras 5 (aparece en tarjeta y en fila de tabla)
        self.assertContains(r, f'data-id="{self.p2.pk}" data-nombre="Cubo"', count=2)
        self.assertContains(r, 'data-total="0" data-otras="5" data-neg="0"', count=2)

    def test_vender_y_cotizar_tambien_tienen_filtro_de_estado_pero_no_tabla(self):
        for url in ("panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(url))
            self.assertContains(r, "data-f-estado")
            self.assertNotContains(r, 'data-vista="tabla"')

    def test_ventas_filtra_y_resume_por_ubicacion(self):
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)])
        self.client.force_login(self.admin)
        servicios.registrar_venta(self.admin, [self.item(self.p2, self.local, 1), self.item(self.p1, None, 1)])
        r = self.client.get(reverse("panel:ventas"))
        self.assertEqual(len(r.context["ventas"]), 2)
        nombres = {x["nombre"] for x in r.context["por_ubicacion"]}
        self.assertEqual(nombres, {"Casa", "Local", "Sin descontar de ningún lugar"})
        self.assertEqual(r.context["mas_vendidos"][0]["producto__nombre"], "Pop it")
        r = self.client.get(reverse("panel:ventas"), {"ubicacion": self.local.pk})
        self.assertEqual(len(r.context["ventas"]), 1)
        self.assertEqual(r.context["unidades"], 1)
        r = self.client.get(reverse("panel:ventas"), {"ubicacion": "ninguna"})
        self.assertEqual(len(r.context["ventas"]), 1)
        r = self.client.get(reverse("panel:ventas"), {"ubicacion": "basura"})
        self.assertEqual(r.status_code, 200)

    def test_el_vendedor_solo_resume_sus_ventas(self):
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2)])
        servicios.registrar_venta(self.admin, [self.item(self.p2, self.local, 1)])
        r = self.client.get(reverse("panel:ventas"))
        self.assertEqual([x["nombre"] for x in r.context["por_ubicacion"]], ["Casa"])


class MontoPorUbicacionTests(Base):
    """El monto por ubicación es neto: reparte los descuentos de cada venta y SIEMPRE cuadra con el total."""

    def test_repartir_total_suma_exacto(self):
        for pesos, total in [([30, 30, 30], 100), ([10, 20], 25), ([1, 1, 1, 1, 1, 1, 1], 10), ([5], 5), ([7, 13, 29], 1)]:
            r = servicios.repartir_total(pesos, total)
            self.assertEqual(sum(r), total, (pesos, total))
            self.assertTrue(all(x == x.to_integral_value() and x >= 0 for x in r))

    def test_repartir_total_casos_borde(self):
        self.assertEqual(servicios.repartir_total([], 10), [])
        self.assertEqual([int(x) for x in servicios.repartir_total([0, 0], 0)], [0, 0])
        self.assertEqual([int(x) for x in servicios.repartir_total([10, 10], 20)], [10, 10])

    def test_descuento_se_reparte_proporcionalmente(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 5), self.item(self.p2, self.local, 5)],
                                      aplicar_reglas=True, descuento_extra=D("10"))
        # 5×10 + 5×20 = 150; 10 unidades → 10% = 15; extra 10 → total 125
        self.assertEqual(v.total, D("125"))
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:ventas"))
        montos = {x["nombre"]: x["monto"] for x in r.context["por_ubicacion"]}
        self.assertEqual(montos, {"Casa": D("42"), "Local": D("83")})  # 125 × 50/150 = 41,67→42 ; 125 × 100/150 = 83,33→83
        self.assertEqual(sum(montos.values()), r.context["resumen"]["total"])

    def test_la_suma_por_ubicacion_siempre_cuadra_con_el_total(self):
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 3, D("7")), self.item(self.p2, self.local, 4, D("9")),
                                             self.item(self.p1, None, 1, D("7"))], descuento_extra=D("5"))
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 6), self.item(self.p2, self.casa, 5)])
        anulada = servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 2)])
        servicios.anular_venta(anulada, self.admin)
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:ventas"))
        self.assertEqual(sum(x["monto"] for x in r.context["por_ubicacion"]), r.context["resumen"]["total"])

    def test_con_filtro_de_ubicacion_solo_esa_fila(self):
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 2), self.item(self.p2, self.local, 2)])
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:ventas"), {"ubicacion": self.local.pk})
        self.assertEqual([x["nombre"] for x in r.context["por_ubicacion"]], ["Local"])


class DescuentoPorProductoTests(Base):
    """El descuento se aplica producto por producto (cada línea en enteros) y el total es la suma de las líneas."""

    def lineas(self):
        # 3×7=21, 4×9=36, 3×5=15 → 10 unidades → regla del 10 %
        extra = [Producto.objects.create(nombre=n, precio=D(pr)) for n, pr in (("A", 7), ("B", 9), ("C", 5))]
        return [self.item(extra[0], self.casa, 3), self.item(extra[1], self.local, 4), self.item(extra[2], None, 3)]

    def test_cada_producto_lleva_su_descuento_redondeado(self):
        t = servicios.calcular_totales(self.lineas())
        # 10 % de 21 = 2,1→2 ; de 36 = 3,6→4 ; de 15 = 1,5→2   (en vez de 10 % de 72 = 7,2→7)
        self.assertEqual([l["descuento"] for l in t["lineas"]], [D(2), D(4), D(2)])
        self.assertEqual(t["descuento_reglas"], D(8))
        self.assertEqual(t["total"], D(64))
        self.assertEqual(sum(l["neto"] for l in t["lineas"]), t["total"])
        self.assertEqual(t["aplicadas"][0]["monto"], D(8))  # el detalle de la regla coincide con la suma por línea

    def test_la_venta_guarda_el_descuento_de_cada_linea(self):
        v = servicios.registrar_venta(self.ana, self.lineas())
        self.assertEqual([i.descuento for i in v.items.order_by("id")], [D(2), D(4), D(2)])
        self.assertEqual(sum(i.neto for i in v.items.all()), v.total)
        self.assertEqual(v.total, D(64))

    def test_descuento_extra_se_reparte_entre_lineas_y_todo_sigue_en_enteros(self):
        t = servicios.calcular_totales(self.lineas(), descuento_extra=D("10"))
        self.assertEqual(t["total"], D(54))
        self.assertEqual(sum(l["descuento_extra"] for l in t["lineas"]), D(10))
        for l in t["lineas"]:
            for k in ("bruto", "descuento_reglas", "descuento_extra", "descuento", "neto"):
                self.assertEqual(l[k], l[k].to_integral_value(), k)
            self.assertTrue(0 <= l["neto"] <= l["bruto"])
        self.assertEqual(sum(l["neto"] for l in t["lineas"]), t["total"])

    def test_promo_por_categoria_solo_toca_sus_lineas(self):
        ReglaDescuento.objects.create(nombre="Táctil 20", minimo_unidades=2, porcentaje=D("20"),
                                      alcance=ReglaDescuento.CATEGORIA, categoria=self.cat, acumulable=True)
        t = servicios.calcular_totales([self.item(self.p1, None, 2), self.item(self.p2, None, 1)])
        self.assertEqual([l["descuento"] for l in t["lineas"]], [D(4), D(0)])  # 20 % de 20 = 4; el Cubo no tiene categoría
        self.assertEqual(t["total"], D(36))

    def test_nunca_se_descuenta_mas_de_lo_que_vale_un_producto(self):
        ReglaDescuento.objects.create(nombre="Loca", minimo_unidades=1, porcentaje=D("95"), acumulable=True)
        t = servicios.calcular_totales([self.item(self.p1, None, 5)])  # 5 % + 95 % acumulables = 100 %
        self.assertEqual(t["lineas"][0]["neto"], D(0))
        self.assertEqual(t["total"], D(0))

    def test_siempre_cuadra_con_muchas_combinaciones(self):
        import itertools
        productos = [Producto.objects.create(nombre=f"P{i}", precio=D(p)) for i, p in enumerate((3, 7, 11, 13, 17))]
        for cants, extra in itertools.product([(1, 1, 1, 1, 1), (2, 3, 1, 4, 5), (7, 0, 2, 9, 1)], (0, 1, 7, 33, 999)):
            lineas = [self.item(p, None, c) for p, c in zip(productos, cants) if c]
            t = servicios.calcular_totales(lineas, descuento_extra=D(extra))
            self.assertEqual(sum(l["neto"] for l in t["lineas"]), t["total"], (cants, extra))
            self.assertEqual(t["subtotal"] - t["descuento_reglas"] - t["descuento_extra"], t["total"])
            self.assertTrue(all(0 <= l["neto"] <= l["bruto"] for l in t["lineas"]), (cants, extra))

    def test_monto_por_ubicacion_suma_exacto_el_total(self):
        servicios.registrar_venta(self.ana, self.lineas(), descuento_extra=D("5"))
        servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 6), self.item(self.p2, self.casa, 5)])
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 2)])
        servicios.anular_venta(v, self.admin)  # no cuenta
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:ventas"))
        self.assertEqual(sum(x["monto"] for x in r.context["por_ubicacion"]), r.context["resumen"]["total"])

    def test_detalle_de_venta_muestra_el_neto_de_cada_linea(self):
        v = servicios.registrar_venta(self.ana, self.lineas())
        self.client.force_login(self.admin)
        r = self.client.get(reverse("panel:venta_detalle", args=[v.pk]))
        self.assertContains(r, "(− 4 desc.)")
        self.assertContains(r, "Bs. 32")  # 36 − 4

    def test_ventas_antiguas_se_reparten_en_la_migracion(self):
        import importlib
        from django.apps import apps as registro
        from .models import VentaItem
        mig = importlib.import_module("inventario.migrations.0005_descuento_por_linea")
        v = Venta.objects.create(vendedor=self.ana, subtotal=D(150), descuento_reglas=D(15), descuento_extra=D(10), total=D(125))
        VentaItem.objects.create(venta=v, producto=self.p1, cantidad=5, precio_lista=D(10), precio_unitario=D(10))
        VentaItem.objects.create(venta=v, producto=self.p2, cantidad=5, precio_lista=D(20), precio_unitario=D(20))
        sin_descuento = Venta.objects.create(vendedor=self.ana, subtotal=D(10), total=D(10))
        VentaItem.objects.create(venta=sin_descuento, producto=self.p1, cantidad=1, precio_lista=D(10), precio_unitario=D(10))
        mig.repartir_descuentos_antiguos(registro, None)
        self.assertEqual([i.descuento for i in v.items.order_by("id")], [D(8), D(17)])  # 25 en total
        self.assertEqual(sum(i.neto for i in v.items.all()), v.total)
        self.assertEqual(sin_descuento.items.get().descuento, D(0))


# ==========================================================================
# Cotización a medida: piezas que NO están en el catálogo
# ==========================================================================
def _subida(color="red", ancho=800, alto=600, nombre="foto.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(nombre, _png(color, ancho, alto), content_type="image/png")


class BaseAMedida(Base):
    """Impresora configurada: costo/hora 1,096 Bs; 10 % de fallas; 50 % de margen; PLA a 90 Bs/kg."""

    def setUp(self):
        import tempfile
        from catalogo.models import ConfiguracionSitio, Filamento
        super().setUp()
        self._media = tempfile.mkdtemp()
        self._ov = override_settings(MEDIA_ROOT=self._media)
        self._ov.enable()
        self.addCleanup(self._ov.disable)
        self.addCleanup(shutil.rmtree, self._media, True)
        c = ConfiguracionSitio.get()
        c.precio_impresora, c.vida_util_horas = D("3500"), 5000          # 0,70 /h
        c.potencia_impresora_kw, c.precio_kwh = D("0.12"), D("0.8")      # 0,096 /h
        c.mantenimiento_anual, c.horas_uso_anual = D("600"), 2000        # 0,30 /h
        c.tasa_fallas_pct, c.margen_ganancia_pct = D("10"), D("50")
        c.save()
        self.pla = Filamento.objects.create(nombre="PLA blanco", costo_por_kg=D("90"))

    def costeo(self, peso="120", horas="6.5"):
        return {"peso_gramos": D(peso), "horas": D(horas), "costo_por_kg": self.pla.costo_por_kg,
                "filamento": self.pla, "filamento_nombre": self.pla.nombre}

    def pieza(self, **extra):
        datos = {"nombre": "Dinosaurio", "descripcion": "Articulado, en PLA blanco.", "medidas": "Alto 12 cm",
                 "cantidad": 2, "precio_unitario": D("85"), "fotos": [], "costeo": self.costeo()}
        datos.update(extra)
        return datos

    def post_pieza(self, i, **campos):
        base = {"nombre": "Dinosaurio", "descripcion": "Articulado.", "medidas": "", "cantidad": "2", "precio": "85",
                "filamento": str(self.pla.pk), "peso": "120", "horas": "6", "minutos": "30"}
        base.update(campos)
        return {f"p{i}_{k}": v for k, v in base.items()}


class FormulaDeCostoTests(BaseAMedida):
    def test_desglose_de_una_pieza(self):
        from catalogo.models import ConfiguracionSitio
        d = ConfiguracionSitio.get().desglose_costo(D("120"), D("6.5"), D("90"))
        self.assertEqual(d["material"], D("10.80"))        # 0,12 kg × 90
        self.assertEqual(d["maquina"], D("7.12"))          # 6,5 h × 1,096
        self.assertEqual(d["costo"], D("19.92"))           # (10,80 + 7,12) / 0,9
        self.assertEqual(d["precio_sugerido"], D("39.83"))  # 19,92 / 0,5

    def test_costear_pieza_va_en_enteros(self):
        d = servicios.costear_pieza(D("120"), D("6.5"), D("90"))
        self.assertEqual((d["costo"], d["precio_sugerido"]), (D("20"), D("40")))
        self.assertFalse(d["config_incompleta"])

    def test_misma_cuenta_que_un_producto_del_catalogo(self):
        """El producto del catálogo y la cotización a medida comparten la fórmula: nunca se desfasan."""
        p = Producto.objects.create(nombre="Igual", precio=D("50"), filamento=self.pla,
                                    peso_gramos=D("120"), horas_impresion=D("6.5"))
        self.assertEqual(p.calcular_costo_y_precio_sugerido(), (D("19.92"), D("39.83")))

    def test_sin_configurar_la_impresora_se_avisa(self):
        from catalogo.models import ConfiguracionSitio
        ConfiguracionSitio.objects.update(precio_impresora=None, potencia_impresora_kw=None, mantenimiento_anual=None)
        d = servicios.costear_pieza(D("100"), D("5"), D("90"))
        self.assertTrue(d["config_incompleta"])
        self.assertEqual(d["maquina"], D("0"))


class LecturaDeCosteoTests(BaseAMedida):
    def test_horas_y_minutos(self):
        from .lineas import horas_desde
        self.assertEqual(horas_desde("5", "30"), D("5.50"))
        self.assertEqual(horas_desde("", "45"), D("0.75"))
        self.assertEqual(horas_desde("2", ""), D("2.00"))
        self.assertEqual(horas_desde("5", "20"), D("5.33"))
        self.assertIsNone(horas_desde("0", "0"))
        self.assertIsNone(horas_desde("x", ""))

    def test_todo_o_nada(self):
        from .lineas import leer_costeo
        self.assertEqual(leer_costeo("", "", "", ""), (None, None))          # nada escrito: sin costeo
        _, error = leer_costeo("100", "", "", "")                              # a medias: se explica qué falta
        self.assertIn("filamento", error)
        self.assertIn("tiempo", error)
        self.assertNotIn("peso", error)

    def test_costeo_correcto(self):
        from .lineas import leer_costeo
        costeo, error = leer_costeo("45,5", "2", "30", str(self.pla.pk))
        self.assertIsNone(error)
        self.assertEqual(costeo["peso_gramos"], D("45.5"))
        self.assertEqual(costeo["horas"], D("2.50"))
        self.assertEqual(costeo["filamento"], self.pla)

    def test_filamento_otro_pide_el_costo_por_kg(self):
        from .lineas import leer_costeo
        _, error = leer_costeo("50", "1", "0", "otro", "PLA seda", "")
        self.assertIn("costo por kg", error)
        costeo, error = leer_costeo("50", "1", "0", "otro", "PLA seda", "120")
        self.assertIsNone(error)
        self.assertIsNone(costeo["filamento"])
        self.assertEqual((costeo["filamento_nombre"], costeo["costo_por_kg"]), ("PLA seda", D("120")))

    def test_valores_invalidos(self):
        from .lineas import leer_costeo
        from catalogo.models import Filamento
        viejo = Filamento.objects.create(nombre="Descontinuado", costo_por_kg=D("50"), activo=False)
        for args in (("0", "1", "0", str(self.pla.pk)), ("abc", "1", "0", str(self.pla.pk)),
                     ("50", "0", "0", str(self.pla.pk)), ("50", "1", "0", "99999"), ("50", "1", "0", str(viejo.pk))):
            self.assertIsNotNone(leer_costeo(*args)[1], args)


class FotosSubidasTests(BaseAMedida):
    def test_se_reduce_y_queda_en_jpeg(self):
        from PIL import Image
        from .imagenes import LADO_MAXIMO_PX, preparar_foto
        salida = preparar_foto(_subida("blue", 4000, 3000))
        img = Image.open(BytesIO(salida.read()))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(max(img.size), LADO_MAXIMO_PX)
        self.assertEqual(img.size, (1600, 1200))        # conserva la proporción

    def test_una_foto_chica_no_se_agranda(self):
        from PIL import Image
        from .imagenes import preparar_foto
        self.assertEqual(Image.open(BytesIO(preparar_foto(_subida("red", 300, 200)).read())).size, (300, 200))

    def test_respeta_la_orientacion_del_celular(self):
        """Los celulares guardan la foto «acostada» + una nota de giro (EXIF): debe quedar derecha."""
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .imagenes import preparar_foto
        img = Image.new("RGB", (200, 100), "green")
        exif = img.getexif()
        exif[0x0112] = 6   # girar 90°
        buf = BytesIO()
        img.save(buf, "JPEG", exif=exif)
        salida = preparar_foto(SimpleUploadedFile("c.jpg", buf.getvalue(), content_type="image/jpeg"))
        self.assertEqual(Image.open(BytesIO(salida.read())).size, (100, 200))

    def test_la_transparencia_pasa_a_fondo_blanco(self):
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .imagenes import preparar_foto
        buf = BytesIO()
        Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(buf, "PNG")
        img = Image.open(BytesIO(preparar_foto(SimpleUploadedFile("t.png", buf.getvalue(), content_type="image/png")).read()))
        self.assertGreater(min(img.getpixel((25, 25))), 245)   # blanco, no negro

    def test_rechaza_lo_que_no_es_imagen_o_pesa_demasiado(self):
        from unittest import mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from . import imagenes
        with self.assertRaises(ErrorNegocio) as c:
            imagenes.preparar_foto(SimpleUploadedFile("x.png", b"esto no es una imagen", content_type="image/png"))
        self.assertIn("no es una imagen válida", str(c.exception))
        with mock.patch.object(imagenes, "MAX_BYTES_SUBIDA", 10), self.assertRaises(ErrorNegocio) as c:
            imagenes.preparar_foto(_subida())
        self.assertIn("pesa demasiado", str(c.exception))


class ServicioAMedidaTests(BaseAMedida):
    def test_crea_la_cotizacion_sin_tocar_catalogo_ni_stock(self):
        productos, stocks = Producto.objects.count(), Stock.objects.count()
        cot = servicios.crear_cotizacion_a_medida(
            self.admin, [self.pieza(), self.pieza(nombre="Llavero", cantidad=5, precio_unitario=D("15"), costeo=None)],
            cliente="María", validez_dias=10, descuento_extra=D("10"))
        self.assertEqual((Producto.objects.count(), Stock.objects.count()), (productos, stocks))
        self.assertEqual(cot.items.count(), 0)
        self.assertEqual(cot.items_personalizados.count(), 2)
        self.assertEqual((cot.subtotal, cot.descuento_extra, cot.total), (D("245"), D("10"), D("235")))  # 2×85 + 5×15 − 10
        self.assertEqual(cot.unidades, 7)
        self.assertTrue(cot.es_a_medida)
        self.assertIn("Dinosaurio", cot.codigos_resumen)

    def test_no_aplica_las_promociones_del_catalogo(self):
        """12 unidades activarían el 10 % del catálogo; aquí el precio de cada pieza ya es el final."""
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(cantidad=12, precio_unitario=D("10"))])
        self.assertEqual((cot.descuento_reglas, cot.detalle_reglas, cot.total), (D("0"), "", D("120")))

    def test_guarda_el_costeo_y_sus_fotos(self):
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(fotos=[_subida("red"), _subida("blue", 400, 900)])])
        it = cot.items_personalizados.get()
        self.assertEqual((it.costo_estimado, it.precio_sugerido, it.precio_unitario), (D("20"), D("40"), D("85")))
        self.assertEqual((it.filamento_nombre, it.peso_gramos, it.horas_impresion), ("PLA blanco", D("120.0"), D("6.50")))
        self.assertEqual(it.tiempo_texto, "6 h 30 min")
        self.assertEqual(it.ganancia_unitaria, D("65"))
        self.assertFalse(it.bajo_costo)
        self.assertEqual([f.orden for f in it.fotos.all()], [0, 1])
        self.assertTrue(all(f.imagen.name.endswith(".jpg") for f in it.fotos.all()))

    def test_el_costeo_guardado_no_cambia_si_despues_sube_el_filamento(self):
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza()])
        self.pla.costo_por_kg = D("500")
        self.pla.save()
        it = cot.items_personalizados.get()
        self.assertEqual((it.filamento_costo_kg, it.costo_estimado), (D("90.00"), D("20.00")))
        self.pla.delete()   # y si se borra el filamento, el nombre queda
        it.refresh_from_db()
        self.assertEqual((it.filamento, it.filamento_nombre), (None, "PLA blanco"))

    def test_una_pieza_sin_costeo_es_valida(self):
        it = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(costeo=None)]).items_personalizados.get()
        self.assertIsNone(it.costo_estimado)
        self.assertFalse(it.tiene_costeo)
        self.assertEqual(it.tiempo_texto, "")

    def test_avisa_si_se_cotiza_por_debajo_del_costo(self):
        it = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(precio_unitario=D("15"))]).items_personalizados.get()
        self.assertTrue(it.bajo_costo)
        self.assertEqual(it.ganancia_unitaria, D("-5"))

    def test_validaciones_y_no_queda_nada_a_medias(self):
        casos = {
            "sin nombre": self.pieza(nombre="  "), "sin descripción": self.pieza(descripcion=""),
            "cantidad 0": self.pieza(cantidad=0), "precio 0": self.pieza(precio_unitario=D("0")),
            "7 fotos": self.pieza(fotos=[_subida() for _ in range(7)]),
            "foto dañada": self.pieza(fotos=[_subida(), __import__("django.core.files.uploadedfile", fromlist=["x"]).SimpleUploadedFile("r.png", b"no", "image/png")]),
        }
        for nombre, pieza in casos.items():
            with self.subTest(nombre), self.assertRaises(ErrorNegocio):
                servicios.crear_cotizacion_a_medida(self.admin, [pieza])
        with self.assertRaises(ErrorNegocio):
            servicios.crear_cotizacion_a_medida(self.admin, [])
        self.assertEqual(Cotizacion.objects.count(), 0)   # ninguna falló a medias

    def test_el_descuento_no_supera_el_subtotal(self):
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(cantidad=1, precio_unitario=D("50"))], descuento_extra=D("999"))
        self.assertEqual((cot.descuento_extra, cot.total), (D("50"), D("0")))


class PantallaAMedidaTests(BaseAMedida):
    def entrar(self, usuario="admin"):
        self.client.login(username=usuario, password="x")

    def test_solo_el_admin_ve_la_pestana_y_la_pantalla(self):
        url = reverse("panel:cotizacion_medida")
        self.assertEqual(self.client.get(url).status_code, 302)           # sin sesión: al login
        self.entrar("ana")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotIn(url, self.client.get(reverse("panel:cotizacion_nueva")).content.decode())
        self.client.logout()
        self.entrar()
        html = self.client.get(url).content.decode()
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn("Cotización a medida", html)
        self.assertIn(f'href="{url}"', self.client.get(reverse("panel:cotizacion_nueva")).content.decode())  # pestaña

    def test_la_pantalla_no_muestra_codigo_de_plantilla(self):
        self.entrar()
        html = self.client.get(reverse("panel:cotizacion_medida")).content.decode()
        for rastro in ("{#", "#}", "{%"):
            self.assertNotIn(rastro, html)

    def test_avisa_si_falta_configurar_la_impresora(self):
        from catalogo.models import ConfiguracionSitio
        self.entrar()
        self.assertNotIn("Aún no está configurado el costo", self.client.get(reverse("panel:cotizacion_medida")).content.decode())
        ConfiguracionSitio.objects.update(precio_impresora=None, potencia_impresora_kw=None, mantenimiento_anual=None)
        self.assertIn("Aún no está configurado el costo", self.client.get(reverse("panel:cotizacion_medida")).content.decode())

    def test_crear_con_dos_piezas_y_fotos(self):
        self.entrar()
        datos = {"pieza": ["3", "7"], "cliente": "María", "validez_dias": "10", "descuento_extra": "5", "observaciones": "Urge"}
        datos.update(self.post_pieza(3, nombre="Dinosaurio"))
        datos["p3_fotos"] = [_subida("red", nombre="a.png"), _subida("blue", nombre="b.png")]
        datos.update(self.post_pieza(7, nombre="Llavero", cantidad="3", precio="12", filamento="", peso="", horas="", minutos=""))
        r = self.client.post(reverse("panel:cotizacion_medida"), datos)
        cot = Cotizacion.objects.get()
        self.assertRedirects(r, reverse("panel:cotizacion_detalle", args=[cot.pk]))
        self.assertEqual((cot.cliente, cot.validez_dias, cot.total), ("María", 10, D("201")))   # 2×85 + 3×12 − 5
        piezas = list(cot.items_personalizados.all())
        self.assertEqual([p.nombre for p in piezas], ["Dinosaurio", "Llavero"])
        self.assertEqual([p.fotos.count() for p in piezas], [2, 0])
        self.assertIsNotNone(piezas[0].costo_estimado)
        self.assertIsNone(piezas[1].costo_estimado)

    def test_con_errores_se_conserva_lo_escrito(self):
        self.entrar()
        datos = {"pieza": ["1"]}
        datos.update(self.post_pieza(1, descripcion="", precio="", nombre="Mi dinosaurio especial"))
        r = self.client.post(reverse("panel:cotizacion_medida"), datos)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("escribe una descripción", html)
        self.assertIn("escribe el precio final", html)
        self.assertIn("Mi dinosaurio especial", html)     # vuelve a pintarse en el formulario
        self.assertEqual(Cotizacion.objects.count(), 0)

    def test_costeo_a_medias_es_un_error_claro(self):
        self.entrar()
        datos = {"pieza": ["1"]}
        datos.update(self.post_pieza(1, horas="", minutos=""))
        html = self.client.post(reverse("panel:cotizacion_medida"), datos).content.decode()
        self.assertIn("falta el tiempo de impresión", html)
        self.assertEqual(Cotizacion.objects.count(), 0)

    def test_mas_de_seis_fotos_es_un_error(self):
        self.entrar()
        datos = {"pieza": ["1"]}
        datos.update(self.post_pieza(1))
        datos["p1_fotos"] = [_subida(nombre=f"{k}.png") for k in range(7)]
        html = self.client.post(reverse("panel:cotizacion_medida"), datos).content.decode()
        self.assertIn("máximo 6 fotos", html)
        self.assertEqual(Cotizacion.objects.count(), 0)

    def test_sin_piezas_no_crea_nada(self):
        self.entrar()
        r = self.client.post(reverse("panel:cotizacion_medida"), {"cliente": "x"})
        self.assertIn("Agrega al menos una pieza", r.content.decode())
        self.assertEqual(Cotizacion.objects.count(), 0)

    def test_calculo_en_vivo(self):
        self.entrar()
        url = reverse("panel:cotizacion_medida_calcular")
        d = self.client.post(url, {"filamento": str(self.pla.pk), "peso": "120", "horas": "6", "minutos": "30"}).json()
        self.assertTrue(d["ok"])
        self.assertEqual((d["material"], d["maquina"], d["fallas"], d["costo"], d["sugerido"]), (11, 7, 2, 20, 40))
        self.assertEqual((d["fallas_pct"], d["margen_pct"], d["config_incompleta"]), ("10", "50", False))
        vacio = self.client.post(url, {}).json()
        self.assertEqual((vacio["ok"], vacio["vacio"]), (False, True))          # nada escrito: sin quejas
        medias = self.client.post(url, {"peso": "50"}).json()
        self.assertEqual((medias["ok"], medias["vacio"]), (False, False))
        self.assertIn("filamento", medias["mensaje"])
        self.client.logout(); self.entrar("ana")
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 403)   # y solo por POST


class DetalleListaYPdfAMedidaTests(BaseAMedida):
    def setUp(self):
        super().setUp()
        self.cot = servicios.crear_cotizacion_a_medida(
            self.admin, [self.pieza(nombre="Dinosaurio azul", fotos=[_subida("red"), _subida("blue"), _subida("green")]),
                         self.pieza(nombre="Llavero", cantidad=5, precio_unitario=D("15"), costeo=None, medidas="")],
            cliente="María Pérez")

    def pdf(self, cot=None, sin_comprimir=False):
        from unittest import mock
        from reportlab import rl_config
        from .pdf import generar_pdf_cotizacion
        if not sin_comprimir:
            return generar_pdf_cotizacion(cot or self.cot)
        with mock.patch.object(rl_config, "pageCompression", 0), mock.patch.object(rl_config, "useA85", 0):
            return generar_pdf_cotizacion(cot or self.cot)

    def test_el_admin_ve_el_costeo_en_el_detalle(self):
        self.client.login(username="admin", password="x")
        html = self.client.get(reverse("panel:cotizacion_detalle", args=[self.cot.pk])).content.decode()
        self.assertIn("Dinosaurio azul", html)
        self.assertIn("Costeo (interno", html)
        self.assertIn("PLA blanco", html)
        self.assertIn("6 h 30 min", html)
        self.assertIn("Ganancia", html)
        self.assertEqual(html.count('class="fotos"'), 1)              # solo la pieza con fotos
        self.assertEqual(html.count("<img src=\"/media/cotizaciones/"), 3)
        for rastro in ("{#", "#}", "{%"):
            self.assertNotIn(rastro, html)

    def test_un_no_admin_no_ve_costos(self):
        otra = servicios.crear_cotizacion_a_medida(self.ana, [self.pieza(nombre="Pieza de Ana")])
        self.client.login(username="ana", password="x")
        html = self.client.get(reverse("panel:cotizacion_detalle", args=[otra.pk])).content.decode()
        self.assertIn("Pieza de Ana", html)
        self.assertNotIn("Costeo (interno", html)
        self.assertNotIn("Sugerido", html)

    def test_la_lista_las_marca_y_se_puede_buscar_por_pieza(self):
        self.client.login(username="admin", password="x")
        otra = servicios.crear_cotizacion(self.admin, [self.item(self.p1, None, 1)])   # una normal, del catálogo
        html = self.client.get(reverse("panel:cotizaciones")).content.decode()
        self.assertEqual(html.count("p-tag-medida"), 1)
        self.assertIn("Dinosaurio azul", html)
        self.assertIn("7 unid.", html)                                                   # 2 + 5
        buscada = self.client.get(reverse("panel:cotizaciones"), {"q": "dinosaurio"}).content.decode()
        self.assertIn(f"N° {self.cot.numero}", buscada)
        self.assertNotIn(f"N° {otra.numero}", buscada)
        por_descripcion = self.client.get(reverse("panel:cotizaciones"), {"q": "articulado"}).content.decode()
        self.assertIn(f"N° {self.cot.numero}", por_descripcion)

    def test_el_pdf_lleva_nombre_descripcion_medidas_y_todas_las_fotos(self):
        from .pdf import generar_pdf_cotizacion
        base = Cotizacion.objects.create(creador=self.admin, total=D("1"))
        servicios_base = self.pdf(base).count(b"/Subtype /Image")        # solo el logo
        pdf = self.pdf()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Subtype /Image"), servicios_base + 3)
        crudo = self.pdf(sin_comprimir=True)
        for texto in (b"Dinosaurio azul", b"Articulado, en PLA blanco.", b"Medidas:", b"Alto 12 cm", b"Llavero", b"Bs. 245"):
            self.assertIn(texto, crudo, texto)
        self.assertEqual(crudo.count(b"Medidas:"), 1)                    # la pieza sin medidas no lleva la línea

    def test_el_pdf_nunca_muestra_costos(self):
        crudo = self.pdf(sin_comprimir=True)
        self.assertIn(b"Dinosaurio azul", crudo)                          # control: así se sabe que el texto SÍ se lee
        for secreto in (b"Costeo", b"costo", b"Costo", b"sugerido", b"Sugerido", b"Ganancia", b"Filamento", b"filamento", b"120 g"):
            self.assertNotIn(secreto, crudo, secreto)

    def test_cotizacion_solo_a_medida_no_dice_sujeta_a_disponibilidad(self):
        crudo = self.pdf(sin_comprimir=True)
        self.assertIn(b"Precios en bolivianos", crudo)      # control: la nota final sí está
        self.assertNotIn(b"disponibilidad", crudo)

    def test_sin_fotos_no_hay_columna_de_fotos(self):
        solo = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(costeo=None)])
        base = self.pdf(Cotizacion.objects.create(creador=self.admin, total=D("1"))).count(b"/Subtype /Image")
        self.assertEqual(self.pdf(solo).count(b"/Subtype /Image"), base)

    def test_mixta_catalogo_y_a_medida_comparte_totales(self):
        """Productos del catálogo + piezas a medida en la misma cotización: ambas tablas y un solo total."""
        from .models import CotizacionItem, CotizacionItemPersonalizado
        mixta = servicios.crear_cotizacion(self.admin, [self.item(self.p1, None, 2)], aplicar_reglas=False)
        CotizacionItemPersonalizado.objects.create(cotizacion=mixta, nombre="Extra a medida", descripcion="Algo único",
                                                    cantidad=1, precio_unitario=D("30"))
        crudo = self.pdf(mixta, sin_comprimir=True)
        self.assertIn(b"Pop it", crudo)
        self.assertIn(b"Extra a medida", crudo)
        self.assertIn(b"disponibilidad", crudo)                           # lleva productos del catálogo
        self.client.login(username="admin", password="x")
        html = self.client.get(reverse("panel:cotizacion_detalle", args=[mixta.pk])).content.decode()
        self.assertIn("N° 2 · Extra a medida", html)                       # sigue la numeración (1 producto + 1 pieza)

    def test_la_vista_del_pdf_responde(self):
        self.client.login(username="admin", password="x")
        r = self.client.get(reverse("panel:cotizacion_pdf", args=[self.cot.pk]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_una_foto_perdida_no_rompe_el_pdf(self):
        """Si falta el archivo de una foto (disco o nube), ese PDF igual se genera."""
        for f in self.cot.items_personalizados.first().fotos.all():
            f.imagen.storage.delete(f.imagen.name)
        self.assertTrue(self.pdf().startswith(b"%PDF"))

    def test_descripcion_larga_con_saltos_de_linea_cabe_en_el_pdf(self):
        larga = "\n".join(f"Línea {k}: detalle de la pieza con varias palabras para ocupar ancho." for k in range(12))
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(descripcion=larga, fotos=[_subida() for _ in range(6)])])
        self.assertTrue(self.pdf(cot).startswith(b"%PDF"))

    def test_caracteres_especiales_no_rompen_el_pdf(self):
        cot = servicios.crear_cotizacion_a_medida(self.admin, [self.pieza(nombre="Rana <b>&</b> sapo", descripcion="Ñandú & «cía» <i>x</i>")])
        crudo = self.pdf(cot, sin_comprimir=True)
        self.assertTrue(crudo.startswith(b"%PDF"))
        self.assertNotIn(b"<b>", crudo)                                  # el HTML escrito por el usuario no se interpreta
