# Misael Toys

Catálogo público de juguetes sensoriales impresos en 3D.
Proyecto **totalmente independiente** del sistema del centro de terapias
(otra base de datos, otro repo, pensado para vivir en `tienda.neuromisael.com`).

## Versión actual (v1)

- Catálogo público: la página principal **ya muestra los productos directamente**,
  sin pasar por otra página intermedia.
- Filtros dinámicos: categoría, precio, y **cualquier atributo que el administrador
  cree desde el panel** (Material, Edad recomendada, Color, etc.) — no están
  fijados en el código, el admin los controla todo.
- Sin carrito ni pagos todavía: cada producto tiene un botón de **"Consultar por
  WhatsApp"** que arma el mensaje automáticamente. La venta se coordina manual.
- **Precio sugerido calculado automáticamente**: cargas una sola vez el costo de
  tu impresora, tu tarifa de luz y tu mantenimiento anual, y el sistema calcula
  solo el "costo por hora de impresora". Luego, en cada producto, si indicas el
  filamento usado, el peso y las horas de impresión, ves un precio sugerido de
  referencia (considerando fallas de impresión y tu margen de ganancia). El
  precio real que ve el cliente lo sigues escribiendo tú a mano — el sugerido
  es solo una guía.
- Los filtros se aplican sin recargar la página completa (usando HTMX).
- Filtros "inteligentes": solo se muestran categorías/valores que tienen al
  menos un producto disponible (no aparecen filtros que llevarían a "0 resultados").
- Paginación (12 productos por página) para cuando el catálogo crezca.
- Vista previa al compartir un producto por WhatsApp (imagen, nombre y precio
  se ven en la tarjeta de enlace gracias a las meta-etiquetas Open Graph).
- Página 404 propia y `robots.txt` básico ya configurados.
- Filtros colapsables en celular (para no tener que hacer scroll por todos
  los filtros antes de ver un producto).
- Botón de **"Me gusta"** en la ficha de cada producto: cualquier visitante
  puede darle "me gusta" sin necesidad de cuenta (se identifica de forma
  anónima con una cookie, para no contar el mismo voto dos veces desde el
  mismo navegador). En el admin, la lista de productos se ordena por defecto
  de más a menos "me gusta" — así ves de un vistazo qué le gusta más a la
  gente.

- **Envíos a nivel nacional**: aviso visible en la portada, en la ficha de cada
  producto y en el pie de página. El texto se edita en *Configuración del sitio →
  Envíos* (puedes apagarlo o agregar una línea de detalle).
- **Medidas por producto**: largo, ancho y alto en cm, una nota libre (para sets,
  ej. "Anillos de 4 a 8 cm") y una comparación cotidiana (ej. "Cabe en la palma de
  una mano adulta"). Se cargan en *Productos → Medidas*, se muestran en la ficha
  y un resumen corto aparece en la tarjeta del catálogo.

## Panel interno: inventario, ventas y cotizaciones (`/panel/`)

Módulo **privado** (requiere login, `noindex`, fuera del sitemap). El catálogo
público no lo conoce: el cliente nunca ve el stock. Vive en la app `inventario`.

- **Stock por lugar**: cada producto tiene su cantidad en cada ubicación
  (crea tus lugares en *Admin → Ubicaciones*). Todos los usuarios ven cuántos
  hay y dónde. El admin puede **agregar** stock (la primera vez en un lugar queda
  como «cantidad inicial»), **trasladar** entre lugares y **corregir** tras un
  conteo. Todo queda en un historial (quién, cuándo, por qué).
- **Ventas**: el vendedor toca la foto del producto (otra vez para sumar, botón «−»
  para quitar), elige de qué lugar sale y el precio (se llena con el de lista pero
  es editable). **Nunca se bloquea una venta por falta de stock**: si el producto
  existe pero no estaba registrado, o se fabrica para entregar después, la venta
  se hace igual y el stock de ese lugar queda en **negativo** (= unidades por
  cubrir, en rojo). Al agregar stock nuevo se compensa solo; si el producto ya
  estaba físicamente, se corrige con «Corregir cantidad». También se puede vender
  sin descontar de ningún lugar. Se guarda quién vendió y a qué precio. Solo el
  admin puede **anular** una venta (el stock vuelve solo).
- **Descuentos / promociones** (*Admin → Reglas de descuento*): ya vienen las dos
  reglas actuales (5+ productos → 5 %, 10+ → 10 %). Puedes crear más: por
  categoría, por producto, con fechas de vigencia, acumulables o no. Las reglas
  no acumulables no se suman: se aplica solo la que más descuenta. Además cada
  venta/cotización admite un descuento adicional manual en Bs.
- **Diseño compacto**: el panel usa todo el ancho de la pantalla. En Vender/Cotizar, en pantallas
  grandes los productos van a la izquierda y el detalle con el total a la derecha (siempre
  visible); en celular, el total queda fijo abajo en una barra delgada y las opciones
  (descuento, cliente, notas) empiezan cerradas para hacer menos scroll.
- **Código de producto** (J001, J002…): cada producto tiene un código único. Es opcional: si lo dejas
  vacío se genera solo (el siguiente libre), y también puedes escribir el que quieras (ej. J025 o
  KIT-01; se guarda en mayúsculas y no se puede repetir). Se edita en *Admin → Productos* (al crear
  uno, el admin ya sugiere el siguiente); los productos existentes se numeran al actualizar
  (`migrate`), en orden de creación.
  - **Se muestra** de forma discreta (pequeño y en gris) donde aparece el nombre: en las tarjetas del
    catálogo (junto al precio), bajo el título de la ficha, en «otros productos», en el panel, en el
    detalle de ventas y cotizaciones, en el recibo y en la cotización en PDF. También viaja en el
    mensaje de WhatsApp de cada producto y como `sku` en los datos estructurados para Google.
  - **Se puede buscar** por código (o por parte de él) en el catálogo público, en Stock, Vender y
    Cotizar, en las listas de Ventas y Cotizaciones (también por nombre, cliente o N°) y en el admin.
- **Orden y búsqueda**:
  - Productos (Stock, Vender y Cotizar): por defecto A-Z por nombre, con selector «Ordenar» para
    cambiar a nombre Z-A, código, precio, stock o más recientes (se recuerda la última elección).
  - Listas de Ventas y Cotizaciones: más recientes por defecto; también por más antiguos, N°, monto,
    cliente o vendedor/creador. Se busca por código, producto, cliente, vendedor/creador, observaciones
    o N°, y se filtra por rango de fechas (Desde/Hasta) y por usuario (admin).
- **PDFs numerados**: la cotización y el recibo llevan una columna «N°» con el ítem (1, 2, 3…).
- **Tarjetas del panel**: compactas (foto, código, nombre, precio y stock por lugar), hasta 3 por fila
  en celular y muchas más en pantallas grandes. No muestran la descripción.
- **Filtros** (Stock, Vender y Cotizar): los mismos que se van generando en el catálogo público —
  categorías y atributos como Material, Edad o Color (los que tengan «mostrar como filtro»)— más
  precio, «hay en [lugar]» y «solo con stock». Se aplican al instante, sin recargar. Dentro de un
  mismo atributo se suman las opciones; entre atributos distintos se exige cumplir todos.
- **Recibo de respaldo**: cada venta tiene un botón «Recibo de respaldo (PDF)»
  (una sola hoja, con firmas, marca «ANULADA» si se anuló). No muestra stock ni lugares.
- **Sin decimales**: todo el dinero del panel y de los PDFs se calcula y se muestra
  en enteros (se redondea al entero más cercano; 0,5 sube).
- **Contacto**: en *Admin → Configuración del sitio → Contacto* hay hasta 2 **teléfonos de
  contacto** (los que se muestran en el sitio, las cotizaciones y los recibos) y hasta
  2 **WhatsApp** (se usan solo en los botones que abren WhatsApp, cada uno con un nombre
  opcional como «Ventas»). Pueden ser los mismos números. Con 2 WhatsApp, la burbuja
  del sitio abre un menú para elegir con quién hablar, y cada producto muestra un botón
  por número. Si no hay teléfonos de contacto, se muestran los de WhatsApp.
- **PDFs**: la cotización y el recibo se abren directo en el navegador (no se fuerza la descarga).
- **Página principal**: el panel y su login tienen un enlace «Página principal» para volver al sitio.
- **Acceso**: el sitio público tiene un enlace «Acceder» en el encabezado
  (pantallas grandes) y otro en el pie, que llevan a `/panel/`.
- **Fotos en la cotización**: cada producto lleva una foto pequeña (11 mm, cuadrada y con esquinas
  redondeadas) como referencia para el cliente. Si ningún producto tiene foto, la columna no aparece;
  si solo algunos no tienen, llevan un cuadrito claro. Las fotos se piden ya recortadas a Cloudinary
  (en paralelo y con límite de tiempo); si una falla, esa fila sale sin foto y el PDF se genera igual.
  El recibo de respaldo no lleva fotos.
- **Cotizaciones**: eliges productos y cantidades, ves el total con las
  promociones aplicadas y descargas un **PDF** para el cliente (sin stock ni
  lugares). No descuenta stock.
- **Usuarios** (solo admin, `/panel/usuarios/`): crear vendedor = usuario +
  contraseña, sin restricciones de formato ni de largo. Se puede cambiar la
  contraseña y desactivar. Los vendedores no entran a `/admin/`. Como las
  contraseñas no se validan, el login se bloquea 10 min tras 8 intentos fallidos.

Después de actualizar: `pip install -r requirements.txt` y `python manage.py migrate`.
Quién es «administrador» del panel: cualquier usuario con `is_staff` (tu superusuario).
Tests: `python manage.py test`.

## Cómo correrlo en local

```bash
python3 -m venv venv
source venv/bin/activate          # en Windows: venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser  # para entrar al panel de administración

# (opcional) crea categorías, filtros y 4 productos de ejemplo para probar:
python manage.py datos_demo

python manage.py runserver
```

Luego abre:
- **http://127.0.0.1:8000/** → catálogo público
- **http://127.0.0.1:8000/admin/** → panel de administración

## Cómo cargar productos reales

Todo se gestiona desde `/admin/`, sin tocar código:

1. **Configuración del sitio** → nombre de la tienda, número de WhatsApp,
   teléfono, texto de bienvenida.
2. **Categorías** → crea las que necesites (ej. "Sensorial táctil").
3. **Atributos (filtros)** → crea el filtro que quieras (ej. "Material") y
   sus valores posibles (ej. "PLA", "PETG") desde la misma pantalla.
4. **Filamentos** → carga los tipos de filamento que usas y su costo por kg
   (ej. "PLA blanco" → Bs. 90/kg). Si el precio del rollo sube, lo actualizas
   aquí una sola vez y afecta el cálculo de todos los productos que lo usan.
   - **Truco para el atributo "Color"**: al crear los valores de color dentro
     de un atributo (ej. Atributo "Color" → valor "Azul"), hay un campo
     `color_hex` opcional. Si lo llenas con un código de color (ej. `#3B82F6`),
     en el catálogo aparece un redondito de ese color en vez del nombre en
     texto. Para un color variado, escribe la palabra `multicolor` (se dibuja
     un redondito estilo arcoíris). Si no sabes el código exacto de un color,
     déjalo vacío y se mostrará el nombre en texto normalmente.
5. **Configuración del sitio** → además de nombre/WhatsApp/teléfono, completa
   la sección "Costo de la impresora": cuánto costó, vida útil estimada,
   consumo eléctrico, tarifa de luz, mantenimiento anual y horas de uso al
   año. Ahí mismo verás el "costo por hora de impresora" ya calculado. También
   defines tu % de fallas esperadas y tu % de margen de ganancia.
6. **Productos** → nombre, precio, descripción, categoría, selecciona los
   valores de atributo que apliquen, y sube las fotos (puedes subir varias,
   la primera es la que aparece en la tarjeta del catálogo). En la sección
   "Cálculo de costos" indica el filamento, el peso en gramos y las horas de
   impresión — al guardar verás el costo estimado y el precio sugerido.

Los filtros del catálogo público se arman **solos** a partir de lo que hayas
cargado — si creas un atributo nuevo, aparece como filtro automáticamente.

## Siguientes pasos (no incluidos en esta versión)

- Carrito de compras y checkout.
- Registro de pedidos con estados (pendiente/pagado/entregado).
- Pagos en línea.
- Despliegue en producción (Postgres, Gunicorn, Nginx, subdominio
  `tienda.neuromisael.com`, almacenamiento externo de imágenes tipo
  Cloudinary/S3 en vez de disco local).

## Estructura del proyecto

```
config/         # settings y urls del proyecto
catalogo/       # única app: modelos, vistas, admin del catálogo
templates/      # HTML (base.html + templates de catalogo/)
static/css/     # estilos propios (sin frameworks pesados)
```