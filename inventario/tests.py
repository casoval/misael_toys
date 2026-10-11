from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from catalogo.models import Atributo, Categoria, Producto, ValorAtributo

from . import servicios
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


class TarjetasCelularTests(Base):
    """Las tarjetas llevan categoría, descripción y medidas (se muestran en celular, como en la página principal)."""

    def setUp(self):
        super().setUp()
        self.p1.descripcion_corta = "Cubo con texturas para explorar con las manos"
        self.p1.largo_cm = D("8"); self.p1.ancho_cm = D("8"); self.p1.alto_cm = D("8")
        self.p1.save()
        self.client.login(username="ana", password="x")

    def test_stock_trae_categoria_descripcion_y_medidas(self):
        r = self.client.get(reverse("panel:stock"))
        self.assertContains(r, 'class="p-prod-cat p-x"')
        self.assertContains(r, "Táctil")
        self.assertContains(r, "Cubo con texturas para explorar con las manos")
        self.assertContains(r, "8 × 8 × 8 cm")

    def test_selector_de_venta_y_cotizacion_trae_los_mismos_datos(self):
        for nombre in ("panel:venta_nueva", "panel:cotizacion_nueva"):
            r = self.client.get(reverse(nombre))
            datos = {p["nombre"]: p for p in r.context["productos_json"]}
            self.assertEqual(datos["Pop it"]["cat"], "Táctil")
            self.assertTrue(datos["Pop it"]["cat_color"].startswith("#"))
            self.assertEqual(datos["Pop it"]["desc"], "Cubo con texturas para explorar con las manos")
            self.assertEqual(datos["Pop it"]["medidas"], "8 × 8 × 8 cm")
            self.assertEqual(datos["Cubo"]["cat"], "")
            self.assertEqual(datos["Cubo"]["medidas"], "")

    def test_el_selector_sigue_sin_consultas_por_producto(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.get(reverse("panel:venta_nueva"))
        with CaptureQueriesContext(connection) as pocas:
            self.client.get(reverse("panel:venta_nueva"))
        for i in range(10):
            Producto.objects.create(nombre=f"Más {i}", precio=D("5"), categoria=self.cat, descripcion_corta="x")
        with CaptureQueriesContext(connection) as muchas:
            self.client.get(reverse("panel:venta_nueva"))
        self.assertEqual(len(pocas), len(muchas))
        self.client.get(reverse("panel:stock"))
        with CaptureQueriesContext(connection) as stock_muchas:
            self.client.get(reverse("panel:stock"))
        Producto.objects.create(nombre="Uno más", precio=D("5"), categoria=self.cat)
        with CaptureQueriesContext(connection) as stock_mas:
            self.client.get(reverse("panel:stock"))
        self.assertEqual(len(stock_muchas), len(stock_mas))
