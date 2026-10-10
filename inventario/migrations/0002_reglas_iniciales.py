from django.db import migrations


def crear_reglas(apps, schema_editor):
    Regla = apps.get_model("inventario", "ReglaDescuento")
    if Regla.objects.exists():
        return
    Regla.objects.create(nombre="Descuento por 5 o más productos", minimo_unidades=5, porcentaje=5)
    Regla.objects.create(nombre="Descuento por 10 o más productos", minimo_unidades=10, porcentaje=10)


class Migration(migrations.Migration):
    dependencies = [("inventario", "0001_initial")]
    operations = [migrations.RunPython(crear_reglas, migrations.RunPython.noop)]
