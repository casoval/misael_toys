from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from catalogo.models import Categoria, Producto

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
        self.assertEqual(t["descuento_reglas"], D("2.50"))
        self.assertEqual(t["total"], D("47.50"))

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
        self.assertEqual(t["total"], D("35.00"))

    def test_descuento_extra_no_deja_total_negativo(self):
        t = servicios.calcular_totales([self.item(self.p1, None, 1)], descuento_extra=D("999"))
        self.assertEqual(t["total"], D("0.00"))

    def test_promo_por_producto_acumulable(self):
        ReglaDescuento.objects.create(nombre="Promo pop it", minimo_unidades=2, porcentaje=20,
                                      alcance="producto", producto=self.p1, acumulable=True)
        t = servicios.calcular_totales([self.item(self.p1, None, 5), self.item(self.p2, None, 1)])
        # 5% de 70 (=3.50) + 20% de 50 (=10)
        self.assertEqual(t["descuento_reglas"], D("13.50"))

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
        # 5 × 9 = 45, -5% = 2.25 → 42.75
        self.assertEqual(v.total, D("42.75"))
        self.assertEqual(v.items.get().precio_lista, D("10.00"))
        self.assertEqual(v.items.get().precio_unitario, D("9.00"))
        self.assertEqual(v.vendedor, self.ana)

    def test_no_se_puede_vender_mas_de_lo_que_hay_en_ese_lugar(self):
        with self.assertRaises(ErrorNegocio):
            servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 4)])
        self.assertEqual(Venta.objects.count(), 0)
        self.assertEqual(self.stock(self.p1, self.local), 3)

    def test_mismo_producto_en_dos_filas_no_burla_el_limite(self):
        with self.assertRaises(ErrorNegocio):
            servicios.registrar_venta(self.ana, [self.item(self.p1, self.local, 2), self.item(self.p1, self.local, 2)])
        self.assertEqual(Venta.objects.count(), 0)
        self.assertEqual(self.stock(self.p1, self.local), 3)

    def test_anular_devuelve_stock(self):
        v = servicios.registrar_venta(self.ana, [self.item(self.p1, self.casa, 4)])
        servicios.anular_venta(v, self.admin, "error")
        v.refresh_from_db()
        self.assertTrue(v.anulada)
        self.assertEqual(self.stock(self.p1, self.casa), 10)
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
        self.assertEqual(v.total, D("45.12"))  # 47.50 - 5% (2.375 → 2.38) = 45.12
        self.assertEqual(self.stock(self.p1, self.casa), 15)

    def test_venta_con_error_no_guarda_y_muestra_mensaje(self):
        self.client.login(username="ana", password="x")
        r = self._venta_post(cantidad=["999"])
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No hay suficiente")
        self.assertEqual(Venta.objects.count(), 0)

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
        self.assertEqual(cot.total, D("66.50"))  # 70 - 5%
        self.assertEqual(self.stock(self.p1, self.casa), 20)  # no toca stock
        pdf = self.client.get(reverse("panel:cotizacion_pdf", args=[cot.pk]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("attachment", pdf["Content-Disposition"])

    def test_vendedor_no_ve_cotizacion_ajena(self):
        cot = servicios.crear_cotizacion(self.luis, [self.item(self.p1, None, 1)])
        self.client.login(username="ana", password="x")
        self.assertEqual(self.client.get(reverse("panel:cotizacion_pdf", args=[cot.pk])).status_code, 404)


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
