from django.core.management.base import BaseCommand
from catalogo.models import Categoria, Atributo, ValorAtributo, Producto, ConfiguracionSitio, Filamento


class Command(BaseCommand):
    help = "Crea categorías, atributos (filtros) y productos de ejemplo para probar el catálogo."

    def handle(self, *args, **options):
        config = ConfiguracionSitio.get()
        config.nombre_tienda = "Misael Toys"
        config.texto_bienvenida = "Jugar · Explorar · Aprender — juguetes sensoriales impresos en 3D, pensados junto a nuestro equipo de terapeutas."
        config.whatsapp_numero = "59170000000"
        config.telefono = "+591 700 00000"
        # Datos de ejemplo para el cálculo de costo por hora de impresora
        # (ajusta estos valores a los reales desde el admin cuando los tengas)
        config.precio_impresora = 2500
        config.vida_util_horas = 5000
        config.potencia_impresora_kw = 0.12
        config.precio_kwh = 0.90
        config.mantenimiento_anual = 300
        config.horas_uso_anual = 1000
        config.tasa_fallas_pct = 10
        config.margen_ganancia_pct = 50
        config.save()

        sensorial, _ = Categoria.objects.get_or_create(nombre="Sensorial táctil", defaults={"orden": 1})
        motricidad, _ = Categoria.objects.get_or_create(nombre="Motricidad fina", defaults={"orden": 2})
        visual, _ = Categoria.objects.get_or_create(nombre="Estimulación visual", defaults={"orden": 3})

        material, _ = Atributo.objects.get_or_create(nombre="Material", defaults={"orden": 1})
        edad, _ = Atributo.objects.get_or_create(nombre="Edad recomendada", defaults={"orden": 2})
        color, _ = Atributo.objects.get_or_create(nombre="Color", defaults={"orden": 3})

        def valor(atributo, texto):
            v, _ = ValorAtributo.objects.get_or_create(atributo=atributo, valor=texto)
            return v

        pla = valor(material, "PLA")
        petg = valor(material, "PETG")
        edad_1_3 = valor(edad, "1-3 años")
        edad_3_6 = valor(edad, "3-6 años")
        azul = valor(color, "Azul")
        verde = valor(color, "Verde")
        multicolor = valor(color, "Multicolor")

        # Códigos de color de ejemplo, para ver los "redonditos" en los filtros
        azul.color_hex = "#3B82F6"
        azul.save()
        verde.color_hex = "#22C55E"
        verde.save()
        multicolor.color_hex = "multicolor"
        multicolor.save()

        filamento_pla, _ = Filamento.objects.get_or_create(
            nombre="PLA genérico", defaults={"costo_por_kg": 90}
        )
        filamento_petg, _ = Filamento.objects.get_or_create(
            nombre="PETG genérico", defaults={"costo_por_kg": 120}
        )

        productos_demo = [
            {
                "nombre": "Cubo sensorial texturizado",
                "categoria": sensorial,
                "precio": 45,
                "descripcion_corta": "Cubo con distintas texturas en cada cara",
                "descripcion": "Cubo diseñado para estimulación táctil, con seis texturas diferentes en sus caras. Recomendado por terapeutas ocupacionales para trabajar discriminación sensorial.",
                "destacado": True,
                "atributos": [pla, edad_1_3, multicolor],
                "filamento": filamento_pla,
                "peso_gramos": 80,
                "horas_impresion": 3,
            },
            {
                "nombre": "Torre de anillos motriz",
                "categoria": motricidad,
                "precio": 60,
                "descripcion_corta": "Ayuda a coordinación mano-ojo",
                "descripcion": "Set de anillos apilables de distintos tamaños, ideal para ejercicios de motricidad fina y coordinación.",
                "atributos": [petg, edad_1_3, azul],
                "filamento": filamento_petg,
                "peso_gramos": 110,
                "horas_impresion": 4,
            },
            {
                "nombre": "Engranajes encajables",
                "categoria": motricidad,
                "precio": 55,
                "descripcion_corta": "Piezas que encajan y giran entre sí",
                "descripcion": "Set de engranajes de colores que se acoplan entre sí, trabajando causa-efecto y precisión de pinza.",
                "atributos": [pla, edad_3_6, verde],
                "filamento": filamento_pla,
                "peso_gramos": 95,
                "horas_impresion": 3.5,
            },
            {
                "nombre": "Espiral de seguimiento visual",
                "categoria": visual,
                "precio": 38,
                "descripcion_corta": "Estimula el seguimiento con la mirada",
                "descripcion": "Figura en espiral con contrastes de color, usada en ejercicios de seguimiento visual y atención sostenida.",
                "atributos": [pla, edad_1_3, multicolor],
                "destacado": True,
                "filamento": filamento_pla,
                "peso_gramos": 60,
                "horas_impresion": 2,
            },
        ]

        for datos in productos_demo:
            atrs = datos.pop("atributos")
            prod, creado = Producto.objects.get_or_create(
                nombre=datos["nombre"], defaults=datos
            )
            prod.atributos.set(atrs)

        self.stdout.write(self.style.SUCCESS("Datos de ejemplo creados correctamente."))