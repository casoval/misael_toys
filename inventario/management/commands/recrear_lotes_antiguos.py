"""Muestra (sin guardar nada) cómo se agruparían los ingresos antiguos en entradas.

Uso previo a la migración 0008:
    python manage.py migrate inventario 0007
    python manage.py recrear_lotes_antiguos --simular
    python manage.py migrate
"""
import importlib

from django.core.management.base import BaseCommand

from inventario.models import LoteStock, MovimientoStock, Ubicacion


class Command(BaseCommand):
    help = "Simula la recreación de lotes a partir de los ingresos antiguos (no guarda nada)."

    def add_arguments(self, parser):
        parser.add_argument("--simular", action="store_true", required=True,
                            help="Obligatorio: este comando solo simula. La recreación real la hace la migración 0008.")

    def handle(self, *args, **opciones):
        modulo = importlib.import_module("inventario.migrations.0008_recrear_lotes_antiguos")
        r = modulo.recrear_lotes(MovimientoStock, LoteStock, simular=True)
        if not r["lotes"]:
            self.stdout.write("No hay ingresos antiguos sin lote: no habría nada que recrear.")
            return
        nombres = dict(Ubicacion.objects.values_list("pk", "nombre"))
        self.stdout.write(f"Se crearían {r['lotes']} entradas con {r['movimientos']} ingresos antiguos:")
        for dia, ubi, n_mov, n_prod in r["detalle"][:50]:
            self.stdout.write(f"  {dia:%d/%m/%Y} · {nombres.get(ubi, ubi)}: {n_mov} ingreso(s), {n_prod} producto(s)")
        if len(r["detalle"]) > 50:
            self.stdout.write(f"  … y {len(r['detalle']) - 50} más")
        self.stdout.write("No se calcula «Había/Quedó» de lo antiguo (queda vacío); no cambia ninguna cantidad de stock.")
