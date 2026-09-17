# Misael Toys

Catálogo público de juguetes y objetos de estimulación impresos en 3D.
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
