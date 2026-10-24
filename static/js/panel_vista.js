// Vista de productos del panel: tarjetas / lista (se recuerda) y filtros al instante.
// Los filtros son los mismos que se van generando en el catálogo público:
// texto, categoría, atributos (Material, Edad, Color…), precio, lugar y "solo con stock".
// Dentro de un mismo atributo se suman las opciones (o); entre atributos distintos se
// exige cumplir todos (y), igual que en el sitio público.
(function () {
  const KEY = "panel-vista";
  const contenedores = document.querySelectorAll("[data-vista-contenedor]");
  const botones = document.querySelectorAll("[data-vista]");

  function leer() { try { return localStorage.getItem(KEY) || "tarjetas"; } catch (e) { return "tarjetas"; } }
  function guardar(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function aplicarVista(v) {
    contenedores.forEach((c) => {
      c.classList.toggle("vista-tarjetas", v === "tarjetas");
      c.classList.toggle("vista-lista", v === "lista");
    });
    botones.forEach((b) => b.classList.toggle("on", b.dataset.vista === v));
  }
  botones.forEach((b) => b.addEventListener("click", () => { guardar(b.dataset.vista); aplicarVista(b.dataset.vista); }));
  aplicarVista(leer());

  // ---------- Filtros ----------
  const barra = document.querySelector("[data-filtros]");
  if (!barra) return;

  const norm = (t) => (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const el = (sel) => barra.querySelector(sel);
  const entradaQ = el("[data-f-q]");
  const entradaMin = el("[data-f-pmin]");
  const entradaMax = el("[data-f-pmax]");
  const selLugar = el("[data-f-lugar]");
  const chkStock = el("[data-f-constock]");
  const selOrden = el("[data-f-orden]");
  const conUnidades = barra.dataset.fUnidades === "1";
  let categoria = "";

  function numero(inp) {
    const v = parseInt(inp.value, 10);
    return Number.isNaN(v) ? null : v;
  }

  // ---------- Orden ----------
  // Por defecto A-Z por nombre (así llegan del servidor). El orden elegido se recuerda.
  const KEY_ORDEN = "panel-orden";
  const cmp = (a, b) => (a || "").localeCompare(b || "", "es", { numeric: true, sensitivity: "base" });
  const num = (t, k) => parseInt(t.dataset[k], 10) || 0;
  // Posición de cada categoría = su lugar en los botones de categoría (el mismo orden que el admin define)
  const posCat = {};
  barra.querySelectorAll("[data-f-cat]").forEach((b, i) => { if (b.dataset.fCat) posCat[b.dataset.fCat] = i; });
  const lugarCat = (t) => (t.dataset.categoria in posCat ? posCat[t.dataset.categoria] : 9999); // sin categoría, al final
  const ORDENES = {
    // Agrupa por categoría y dentro de cada una ordena por nombre
    categoria: (a, b) => lugarCat(a) - lugarCat(b) || cmp(a.dataset.nombre, b.dataset.nombre),
    nombre: (a, b) => cmp(a.dataset.nombre, b.dataset.nombre),
    nombre_desc: (a, b) => cmp(b.dataset.nombre, a.dataset.nombre),
    // Sin código al final; con código, "natural" (J2 antes que J10)
    codigo: (a, b) => (!a.dataset.codigo - !b.dataset.codigo) || cmp(a.dataset.codigo, b.dataset.codigo) || cmp(a.dataset.nombre, b.dataset.nombre),
    precio_asc: (a, b) => num(a, "precio") - num(b, "precio") || cmp(a.dataset.nombre, b.dataset.nombre),
    precio_desc: (a, b) => num(b, "precio") - num(a, "precio") || cmp(a.dataset.nombre, b.dataset.nombre),
    stock_desc: (a, b) => num(b, "total") - num(a, "total") || cmp(a.dataset.nombre, b.dataset.nombre),
    stock_asc: (a, b) => num(a, "total") - num(b, "total") || cmp(a.dataset.nombre, b.dataset.nombre),
    recientes: (a, b) => num(b, "id") - num(a, "id"),
  };

  function ordenar() {
    const f = ORDENES[selOrden.value] || ORDENES.nombre;
    document.querySelectorAll("[data-vista-contenedor]").forEach((c) => {
      const tarjetas = [...c.querySelectorAll(":scope > .p-prod")];
      tarjetas.sort(f).forEach((t) => c.appendChild(t)); // reubica las tarjetas existentes (no las recrea)
    });
  }

  function aplicarFiltros() {
    const q = norm(entradaQ.value.trim());
    const pmin = numero(entradaMin), pmax = numero(entradaMax);
    const lugar = selLugar.value, soloStock = chkStock.checked;

    // Valores elegidos, agrupados por atributo
    const grupos = {};
    barra.querySelectorAll("[data-f-valor].on").forEach((b) => {
      const g = b.closest("[data-f-grupo]").dataset.fGrupo;
      (grupos[g] = grupos[g] || []).push(b.dataset.fValor);
    });
    const listaGrupos = Object.values(grupos);

    const tarjetas = document.querySelectorAll("[data-vista-contenedor] .p-prod");
    let visibles = 0, unidades = 0;
    tarjetas.forEach((t) => {
      const d = t.dataset;
      let ok = true;
      if (q && !(norm(d.nombre).includes(q) || norm(d.codigo).includes(q))) ok = false;
      if (ok && categoria && d.categoria !== categoria) ok = false;
      if (ok && listaGrupos.length) {
        const suyos = d.valores || "";
        ok = listaGrupos.every((ids) => ids.some((id) => suyos.includes(" " + id + " ")));
      }
      const precio = parseInt(d.precio, 10);
      if (ok && pmin !== null && !(precio >= pmin)) ok = false;
      if (ok && pmax !== null && !(precio <= pmax)) ok = false;
      if (ok && lugar && !(parseInt(d["u" + lugar], 10) > 0)) ok = false;
      if (ok && soloStock && !(parseInt(d.total, 10) > 0)) ok = false;
      t.hidden = !ok;
      if (ok) { visibles++; unidades += Math.max(parseInt(d.total, 10) || 0, 0); }
    });

    // Cuántos filtros hay activos (sin contar el texto) y resumen
    const activos = (categoria ? 1 : 0) + listaGrupos.reduce((n, g) => n + g.length, 0)
      + (pmin !== null || pmax !== null ? 1 : 0) + (lugar ? 1 : 0) + (soloStock ? 1 : 0);
    const masActivos = activos - (categoria ? 1 : 0); // lo que está dentro de "Más filtros"
    const cuenta = el("[data-f-cuenta]");
    cuenta.hidden = masActivos === 0;
    cuenta.textContent = masActivos;
    el("[data-f-limpiar]").hidden = activos === 0 && !q;
    el("[data-f-resumen]").textContent =
      visibles + " de " + tarjetas.length + " producto" + (tarjetas.length === 1 ? "" : "s") +
      (conUnidades ? " · " + unidades + " unidades" : "");
    actualizarInfoCatalogo(visibles);
  }

  // ---------- Catálogo imprimible (PDF carta) ----------
  // Abre en otra pestaña un PDF con los productos que se ven ahora (mismos filtros y mismo orden).
  const imp = el("[data-imprimir]");
  const selDiseno = imp && imp.querySelector("[data-i-diseno]");
  const KEY_DISENO = "panel-catalogo-diseno";

  function actualizarInfoCatalogo(n) {
    if (!imp) return;
    const info = imp.querySelector("[data-i-info]");
    const porHoja = parseInt(selDiseno.value, 10) || 9;
    const hojas = Math.ceil(n / porHoja);
    info.textContent = n ? n + " → " + hojas + " hoja" + (hojas === 1 ? "" : "s") : "sin productos";
  }

  // Resumen de los filtros activos para el encabezado del PDF (sin stock ni lugares: es para el cliente)
  function textoFiltros() {
    const partes = [];
    const cat = barra.querySelector("[data-f-cat].on");
    if (cat && cat.dataset.fCat) partes.push("Categoría: " + cat.textContent.trim());
    barra.querySelectorAll("[data-f-grupo]").forEach((g) => {
      const on = [...g.querySelectorAll("[data-f-valor].on")].map((b) => b.textContent.trim());
      const nombre = g.querySelector("legend");
      if (on.length && nombre) partes.push(nombre.textContent.trim() + ": " + on.join(" / "));
    });
    const pmin = numero(entradaMin), pmax = numero(entradaMax);
    if (pmin !== null && pmax !== null) partes.push("Precio: Bs. " + pmin + " a " + pmax);
    else if (pmin !== null) partes.push("Precio: desde Bs. " + pmin);
    else if (pmax !== null) partes.push("Precio: hasta Bs. " + pmax);
    if (entradaQ.value.trim()) partes.push("Búsqueda: \u201c" + entradaQ.value.trim() + "\u201d");
    return partes.join("  ·  ");
  }

  if (imp) {
    try { const g = localStorage.getItem(KEY_DISENO); if (g && [...selDiseno.options].some((o) => o.value === g)) selDiseno.value = g; } catch (e) {}
    selDiseno.addEventListener("change", () => {
      try { localStorage.setItem(KEY_DISENO, selDiseno.value); } catch (e) {}
      aplicarFiltros();
    });
    imp.querySelector("[data-i-btn]").addEventListener("click", () => {
      const ids = [...document.querySelectorAll("[data-vista-contenedor] .p-prod:not([hidden])")].map((t) => t.dataset.id);
      if (!ids.length) { alert("No hay productos para imprimir con estos filtros."); return; }
      const f = document.createElement("form");
      f.method = "post"; f.action = imp.dataset.url; f.target = "_blank"; f.hidden = true;
      [["csrfmiddlewaretoken", imp.dataset.csrf], ["ids", ids.join(",")], ["diseno", selDiseno.value], ["filtros", textoFiltros()]]
        .forEach(([n, v]) => { const i = document.createElement("input"); i.type = "hidden"; i.name = n; i.value = v; f.appendChild(i); });
      document.body.appendChild(f);
      f.submit();
      setTimeout(() => f.remove(), 0);
    });
  }

  barra.querySelectorAll("[data-f-cat]").forEach((b) => b.addEventListener("click", () => {
    categoria = b.dataset.fCat;
    barra.querySelectorAll("[data-f-cat]").forEach((x) => x.classList.toggle("on", x === b));
    aplicarFiltros();
  }));
  barra.querySelectorAll("[data-f-valor]").forEach((b) => b.addEventListener("click", () => {
    b.classList.toggle("on");
    aplicarFiltros();
  }));
  [entradaQ, entradaMin, entradaMax].forEach((i) => i.addEventListener("input", aplicarFiltros));
  selLugar.addEventListener("change", aplicarFiltros);
  selOrden.addEventListener("change", () => {
    try { localStorage.setItem(KEY_ORDEN, selOrden.value); } catch (e) {}
    ordenar();
  });
  chkStock.addEventListener("change", aplicarFiltros);

  el("[data-f-limpiar]").addEventListener("click", () => {
    categoria = "";
    barra.querySelectorAll("[data-f-cat]").forEach((x) => x.classList.toggle("on", x.dataset.fCat === ""));
    barra.querySelectorAll("[data-f-valor]").forEach((x) => x.classList.remove("on"));
    entradaQ.value = entradaMin.value = entradaMax.value = "";
    selLugar.value = "";
    chkStock.checked = false;
    aplicarFiltros();
  });  // (Limpiar filtros no cambia el orden elegido)

  // Enter en el buscador no debe enviar el formulario de venta/cotización
  entradaQ.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
  [entradaMin, entradaMax].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); }));

  // El panel "Más filtros" se cierra al tocar en cualquier otro lado
  const fmas = barra.querySelector(".p-fmas");
  document.addEventListener("click", (e) => { if (fmas && fmas.open && !fmas.contains(e.target)) fmas.open = false; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && fmas) fmas.open = false; });

  try { const g = localStorage.getItem(KEY_ORDEN); if (g && ORDENES[g]) selOrden.value = g; } catch (e) {}
  ordenar();
  aplicarFiltros();
})();
