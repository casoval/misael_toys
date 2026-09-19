from django.db import models
from django.utils.text import slugify
from django.urls import reverse


def _url_cloudinary_transformada(url, transformacion):
    """Si la URL es de Cloudinary, le inserta una transformación (redimensionar
    + calidad/formato automáticos) para no bajar la foto a resolución completa
    donde no hace falta (ej. una miniatura de 220px no necesita una foto de
    4000px de ancho). Si no es Cloudinary (ej. desarrollo local sin
    credenciales configuradas), devuelve la URL tal cual: sin ese servicio no
    hay forma de redimensionar sobre la marcha."""
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return url
    return url.replace("/upload/", f"/upload/{transformacion}/", 1)


class Categoria(models.Model):
    """Categorías de productos (ej. Sensorial táctil, Motricidad fina...).
    El administrador crea las que necesite desde el admin."""

    nombre = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    descripcion = models.TextField(blank=True)
    orden = models.PositiveIntegerField(default=0, help_text="Menor número = aparece primero")
    activa = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Categoría"
        verbose_name_plural = "Categorías"
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.nombre)
        super().save(*args, **kwargs)


class Atributo(models.Model):
    """Un tipo de filtro que el admin puede crear libremente:
    'Material', 'Edad recomendada', 'Color', 'Uso terapéutico', etc."""

    nombre = models.CharField(max_length=100, unique=True)
    orden = models.PositiveIntegerField(default=0, help_text="Orden en que aparece el filtro en la página")
    mostrar_como_filtro = models.BooleanField(
        default=True,
        help_text="Si está marcado, este atributo aparece como filtro en el catálogo público",
    )

    class Meta:
        verbose_name = "Atributo (filtro)"
        verbose_name_plural = "Atributos (filtros)"
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre


class ValorAtributo(models.Model):
    """Un valor posible de un atributo, ej. Atributo='Material', valor='PLA'."""

    atributo = models.ForeignKey(Atributo, on_delete=models.CASCADE, related_name="valores")
    valor = models.CharField(max_length=100)
    color_hex = models.CharField(
        max_length=20, blank=True,
        help_text="Solo para valores de color. Código hex (ej: #3B82F6) para mostrar un redondito "
                   "de ese color en los filtros, en vez del nombre en texto. Para un color variado, "
                   "escribe la palabra 'multicolor' (se dibuja un redondito estilo arcoíris). "
                   "Si no sabes el código exacto, déjalo vacío: se mostrará el nombre en texto, "
                   "que es más honesto que un color inventado."
    )

    class Meta:
        verbose_name = "Valor de atributo"
        verbose_name_plural = "Valores de atributo"
        unique_together = ("atributo", "valor")
        ordering = ["atributo__orden", "valor"]

    def __str__(self):
        return f"{self.atributo.nombre}: {self.valor}"

    @property
    def es_multicolor(self):
        return self.color_hex.strip().lower() == "multicolor"


class Filamento(models.Model):
    """Tipo de filamento usado para imprimir, con su costo por kilo.
    Al actualizar el precio aquí, se actualiza el cálculo de todos los
    productos que usan este filamento — no hay que tocarlos uno por uno."""

    nombre = models.CharField(max_length=80, unique=True, help_text="Ej: PLA blanco, PETG azul")
    costo_por_kg = models.DecimalField(max_digits=10, decimal_places=2, help_text="Costo del rollo/kg en Bs.")
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Filamento"
        verbose_name_plural = "Filamentos"
        ordering = ["nombre"]

    def __str__(self):
        return f"{self.nombre} (Bs. {self.costo_por_kg}/kg)"


class Producto(models.Model):
    nombre = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True, blank=True)
    categoria = models.ForeignKey(
        Categoria, on_delete=models.SET_NULL, null=True, blank=True, related_name="productos"
    )
    descripcion_corta = models.CharField(
        max_length=200, blank=True, help_text="Aparece en la tarjeta del catálogo"
    )
    descripcion = models.TextField(blank=True, help_text="Descripción completa, en la ficha del producto")
    precio = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Precio real de venta en Bs. (el que ve el cliente). Tú decides el número final; "
                   "usa el 'precio sugerido' de abajo solo como referencia."
    )

    atributos = models.ManyToManyField(
        ValorAtributo, blank=True, related_name="productos",
        help_text="Ej: Material=PLA, Color=Azul, Edad recomendada=3-5 años"
    )

    disponible = models.BooleanField(default=True, help_text="Se muestra en el catálogo público")
    destacado = models.BooleanField(default=False, help_text="Se resalta primero en el catálogo")
    orden = models.PositiveIntegerField(default=0)

    # --- Datos para calcular un precio sugerido (uso interno, no se muestran al público) ---
    filamento = models.ForeignKey(
        Filamento, on_delete=models.SET_NULL, null=True, blank=True, related_name="productos",
        help_text="Filamento usado, para calcular el costo de material"
    )
    peso_gramos = models.DecimalField(
        max_digits=8, decimal_places=1, null=True, blank=True,
        help_text="Peso de la pieza impresa, en gramos"
    )
    horas_impresion = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Horas que tarda en imprimirse esta pieza"
    )

    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    # Contador de "me gusta" (se actualiza solo al dar/quitar like, ver LikeProducto)
    total_likes = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
        ordering = ["-destacado", "orden", "-creado"]

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.nombre)
            slug = base_slug
            i = 1
            while Producto.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                i += 1
                slug = f"{base_slug}-{i}"
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("catalogo:producto_detalle", kwargs={"slug": self.slug})

    @property
    def imagen_principal(self):
        primera = self.imagenes.first()
        return primera.imagen.url if primera else None

    @property
    def imagen_principal_miniatura(self):
        """Versión chica y optimizada de la primera foto, para las tarjetas
        del catálogo (ver ImagenProducto.miniatura). Ahí es donde más pesa
        la velocidad, porque se cargan varias tarjetas a la vez."""
        primera = self.imagenes.first()
        return primera.miniatura if primera else None

    def calcular_costo_y_precio_sugerido(self):
        """Calcula el costo estimado y un precio de venta sugerido, a partir de:
        - costo del material (peso x precio del filamento)
        - costo de máquina (horas de impresión x costo por hora de la impresora)
        - un % extra por fallas de impresión
        - el margen de ganancia configurado

        Devuelve (costo_estimado, precio_sugerido) o (None, None) si faltan datos
        (peso, horas o filamento) para poder calcularlo.
        """
        if not self.peso_gramos or not self.horas_impresion or not self.filamento:
            return None, None

        config = ConfiguracionSitio.get()

        costo_material = (self.peso_gramos / 1000) * self.filamento.costo_por_kg
        costo_maquina = self.horas_impresion * config.costo_hora_impresora
        costo_estimado = costo_material + costo_maquina

        tasa_fallas = config.tasa_fallas_pct / 100
        if tasa_fallas > 0:
            costo_estimado = costo_estimado / (1 - tasa_fallas)

        margen = config.margen_ganancia_pct / 100
        if margen > 0:
            precio_sugerido = costo_estimado / (1 - margen)
        else:
            precio_sugerido = costo_estimado

        from decimal import Decimal
        return costo_estimado.quantize(Decimal("0.01")), precio_sugerido.quantize(Decimal("0.01"))


class LikeProducto(models.Model):
    """Registro de un 'me gusta' anónimo, identificado por una cookie del navegador
    (no requiere cuentas de cliente). Sirve para no contar el mismo voto dos veces
    y para poder des-marcar el 'me gusta' si lo tocan de nuevo."""

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="likes")
    visitante_id = models.CharField(max_length=64, db_index=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Me gusta"
        verbose_name_plural = "Me gusta"
        unique_together = ("producto", "visitante_id")

    def __str__(self):
        return f"Like de {self.visitante_id[:8]}... a {self.producto.nombre}"


class ImagenProducto(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="imagenes")
    imagen = models.ImageField(upload_to="productos/%Y/%m/")
    orden = models.PositiveIntegerField(default=0)
    alt_text = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "Imagen de producto"
        verbose_name_plural = "Imágenes de producto"
        ordering = ["orden", "id"]

    def __str__(self):
        return f"Imagen de {self.producto.nombre} ({self.orden})"

    @property
    def miniatura(self):
        """Para las tarjetas del catálogo (se ven a ~220-280px; se pide al
        doble para que se vea nítida en pantallas retina)."""
        return _url_cloudinary_transformada(self.imagen.url, "w_500,h_500,c_fill,q_auto,f_auto")

    @property
    def miniatura_chica(self):
        """Para las miniaturas de la galería en la ficha de producto (se ven
        a 60px)."""
        return _url_cloudinary_transformada(self.imagen.url, "w_150,h_150,c_fill,q_auto,f_auto")

    @property
    def grande(self):
        """Para la foto principal de la ficha de producto: limita el ancho
        máximo (evita bajar fotos de varios MB cuando en pantalla se ven a
        unos 500-600px) sin recortar el encuadre."""
        return _url_cloudinary_transformada(self.imagen.url, "w_900,q_auto,f_auto")


class ConfiguracionSitio(models.Model):
    """Configuración general del sitio (singleton: solo debe existir 1 registro)."""

    nombre_tienda = models.CharField(max_length=100, default="Misael Toys")
    whatsapp_numero = models.CharField(
        max_length=20, blank=True,
        help_text="Con código de país, sin '+' ni espacios. Ej: 59171234567"
    )
    mensaje_whatsapp_base = models.CharField(
        max_length=250,
        default="Hola, quiero consultar sobre el producto: {producto}",
        help_text="Usa {producto} donde quieras que aparezca el nombre del producto",
    )
    telefono = models.CharField(max_length=30, blank=True)
    email_contacto = models.EmailField(blank=True)
    texto_bienvenida = models.CharField(
        max_length=250, blank=True,
        default="Jugar · Explorar · Aprender — juguetes sensoriales impresos en 3D",
    )
    horario_atencion = models.CharField(max_length=150, blank=True)

    # --- Costos para calcular el "costo por hora de impresora" ---
    precio_impresora = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Cuánto costó la impresora, en Bs. (para calcular la depreciación por hora)"
    )
    vida_util_horas = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Horas de vida útil estimadas de la impresora antes de necesitar reparación mayor. "
                   "Si no sabes, un estimado razonable es 4,000-5,000 horas."
    )
    potencia_impresora_kw = models.DecimalField(
        max_digits=5, decimal_places=3, null=True, blank=True,
        help_text="Consumo real de la impresora en kW mientras imprime (no la potencia máxima de la fuente). "
                   "Ej: 0.120"
    )
    precio_kwh = models.DecimalField(
        max_digits=6, decimal_places=4, null=True, blank=True,
        help_text="Precio del kWh según tu factura de luz, en Bs."
    )
    mantenimiento_anual = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Gasto anual estimado en repuestos (boquillas, correas, cama, etc.), en Bs."
    )
    horas_uso_anual = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Horas que usas la impresora al año aproximadamente (para repartir el mantenimiento)"
    )

    # --- Fallas y margen, para el precio sugerido ---
    tasa_fallas_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="% estimado de piezas que fallan al imprimir (ej. 10 = 10%). "
                   "Se usa para que las piezas buenas cubran el costo de las que fallan."
    )
    margen_ganancia_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="% de ganancia deseado sobre el costo (ej. 50 = 50%), usado para el precio sugerido."
    )

    class Meta:
        verbose_name = "Configuración del sitio"
        verbose_name_plural = "Configuración del sitio"

    def __str__(self):
        return self.nombre_tienda

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def costo_hora_impresora(self):
        """Costo por hora de impresora = depreciación + electricidad + mantenimiento.
        Devuelve 0 si falta algún dato necesario (para no romper cálculos)."""
        from decimal import Decimal

        total = Decimal("0")

        if self.precio_impresora and self.vida_util_horas:
            total += self.precio_impresora / self.vida_util_horas

        if self.potencia_impresora_kw and self.precio_kwh:
            total += self.potencia_impresora_kw * self.precio_kwh

        if self.mantenimiento_anual and self.horas_uso_anual:
            total += self.mantenimiento_anual / self.horas_uso_anual

        return total.quantize(Decimal("0.0001"))