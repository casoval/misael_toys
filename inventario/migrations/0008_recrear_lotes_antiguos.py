"""Recrea como «entradas» (lotes) los ingresos de stock hechos ANTES de existir los lotes.

Regla: los movimientos de tipo «Ingreso» y «Cantidad inicial» que no pertenecen a ningún lote se agrupan por
DÍA (hora local de Bolivia) y UBICACIÓN: cada grupo pasa a ser un lote en modo «sumar».

Solo agrupa. NO calcula «Había / Quedó» de los movimientos antiguos (quedan vacíos): así la entrada se ve y se
imprime sin riesgo de mostrar números reconstruidos que no sean fiables.

No cambia ninguna cantidad de stock, ni ventas, ni otros movimientos. Es reversible.
La lógica está en `recrear_lotes()` para poder simularla con `manage.py recrear_lotes_antiguos --simular`.
"""
from collections import Counter, defaultdict

from django.db import migrations
from django.utils import timezone

MARCA = "Recreado de ingresos antiguos"
TIPOS_ENTRADA = ("inicial", "ingreso")


def recrear_lotes(MovimientoStock, LoteStock, simular=False):
    """Devuelve un dict con el resumen. Con `simular=True` no escribe nada."""
    grupos = defaultdict(list)  # (día local, ubicación) -> movimientos
    pendientes = MovimientoStock.objects.filter(lote__isnull=True, tipo__in=TIPOS_ENTRADA).order_by("creado", "id")
    for m in pendientes:
        grupos[(timezone.localtime(m.creado).date(), m.ubicacion_id)].append(m)

    # (el modelo histórico de usuario no trae USERNAME_FIELD: se usa «username», el del User estándar de Django)
    modelo_usuario = MovimientoStock._meta.get_field("usuario").related_model
    nombres = dict(modelo_usuario.objects.values_list("pk", "username"))

    resumen = {"lotes": 0, "movimientos": 0, "detalle": []}
    for (dia, ubi), movs in sorted(grupos.items(), key=lambda g: (g[0][0], g[0][1])):
        usuarios = Counter(m.usuario_id for m in movs if m.usuario_id)
        nota = MARCA
        if len(usuarios) > 1:
            nota += " · registraron: " + ", ".join(nombres.get(u, "?") for u, _ in usuarios.most_common())
        resumen["lotes"] += 1
        resumen["movimientos"] += len(movs)
        resumen["detalle"].append((dia, ubi, len(movs), len({m.producto_id for m in movs})))
        if simular:
            continue

        lote = LoteStock.objects.create(
            usuario_id=usuarios.most_common(1)[0][0] if usuarios else None,
            modo="sumar", nota=nota[:200], aplicados=len(movs), sin_cambio=0,
        )
        # `creado` es automático al crear: se corrige para que sea la hora del primer ingreso del grupo.
        LoteStock.objects.filter(pk=lote.pk).update(creado=movs[0].creado)
        MovimientoStock.objects.filter(pk__in=[m.pk for m in movs]).update(lote=lote.pk)
    return resumen


def adelante(apps, schema_editor):
    MovimientoStock = apps.get_model("inventario", "MovimientoStock")
    LoteStock = apps.get_model("inventario", "LoteStock")
    r = recrear_lotes(MovimientoStock, LoteStock)
    if r["lotes"]:
        print(f"\n    {r['lotes']} entradas recreadas con {r['movimientos']} ingresos antiguos", end="")


def atras(apps, schema_editor):
    MovimientoStock = apps.get_model("inventario", "MovimientoStock")
    LoteStock = apps.get_model("inventario", "LoteStock")
    mias = LoteStock.objects.filter(nota__startswith=MARCA)
    MovimientoStock.objects.filter(lote__in=mias).update(lote=None)
    mias.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0007_lotes_de_stock"),
    ]

    operations = [
        migrations.RunPython(adelante, atras),
    ]
