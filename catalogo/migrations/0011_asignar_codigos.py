from django.db import migrations


def asignar_codigos(apps, schema_editor):
    """Los productos que ya existen reciben su código en orden de creación: J001, J002…"""
    Producto = apps.get_model("catalogo", "Producto")
    pendientes = list(Producto.objects.filter(codigo__isnull=True).order_by("id"))
    usados = set(Producto.objects.exclude(codigo__isnull=True).values_list("codigo", flat=True))
    n = 0
    for producto in pendientes:
        while True:
            n += 1
            codigo = f"J{n:03d}"
            if codigo not in usados:
                break
        producto.codigo = codigo
        usados.add(codigo)
    Producto.objects.bulk_update(pendientes, ["codigo"])


class Migration(migrations.Migration):
    dependencies = [("catalogo", "0010_producto_codigo_interno")]
    operations = [migrations.RunPython(asignar_codigos, migrations.RunPython.noop)]
