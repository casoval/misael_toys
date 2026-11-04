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

  const tablaCont = document.querySelector("[data-tabla-contenedor]");
  function aplicarVista(v) {
    if (v === "tabla" && !tablaCont) v = "tarjetas"; // la tabla solo existe en Stock
    contenedores.forEach((c) => {
      c.classList.toggle("vista-tarjetas", v === "tarjetas");
      c.classList.toggle("vista-lista", v === "lista");
      c.hidden = v === "tabla";
    });
    if (tablaCont) tablaCont.hidden = v !== "tabla";
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
  const selEstado = el("[data-f-estado]");
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

  ORDENES.otras_desc = (a, b) => num(b, "otras") - num(a, "otras") || cmp(a.dataset.nombre, b.dataset.nombre);
  let porColumna = null; // orden por clic en el título de una columna de la tabla: {clave, dir}
  function ordenar() {
    let f = ORDENES[selOrden.value] || ORDENES.nombre;
    if (porColumna) {
      const { clave, dir } = porColumna;
      f = clave === "nombre"
        ? (a, b) => dir * cmp(a.dataset.nombre, b.dataset.nombre)
        : (a, b) => dir * (num(a, clave) - num(b, clave)) || cmp(a.dataset.nombre, b.dataset.nombre);
    }
    document.querySelectorAll("[data-vista-contenedor]").forEach((c) => {
      const tarjetas = [...c.querySelectorAll(":scope > .p-prod")];
      tarjetas.sort(f).forEach((t) => c.appendChild(t)); // reubica las tarjetas existentes (no las recrea)
    });
    document.querySelectorAll("[data-tabla-contenedor] tbody").forEach((tb) => {
      [...tb.querySelectorAll(":scope > tr")].sort(f).forEach((t) => tb.appendChild(t));
    });
    document.querySelectorAll("[data-orden-col]").forEach((th) => {
      const on = porColumna && th.dataset.ordenCol === porColumna.clave;
      th.classList.toggle("orden-asc", !!on && porColumna.dir === 1);
      th.classList.toggle("orden-desc", !!on && porColumna.dir === -1);
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

    const estado = selEstado ? selEstado.value : "";
    // Tarjetas del selector + filas de la tabla (misma información, mismos filtros)
    const tarjetas = document.querySelectorAll("[data-vista-contenedor] .p-prod");
    const filasTabla = document.querySelectorAll("[data-tabla-contenedor] tbody tr");
    const sumaU = {};
    let visibles = 0, unidades = 0, sumaTotal = 0, sumaOtras = 0;
    const ESTADOS = {
      con: (t, o) => t > 0,
      bajo: (t, o) => t === 1,
      agotado: (t, o) => t <= 0,
      solo_otras: (t, o) => t <= 0 && o > 0,
      nada: (t, o) => t <= 0 && o <= 0,
    };
    const pasa = (d) => {
      let ok = true;
      if (estado) {
        const t = parseInt(d.total, 10) || 0, o = parseInt(d.otras, 10) || 0;
        ok = estado === "neg" ? (parseInt(d.neg, 10) || 0) > 0 : (ESTADOS[estado] ? ESTADOS[estado](t, o) : true);
      }
      if (ok && q && !(norm(d.nombre).includes(q) || norm(d.codigo).includes(q))) ok = false;
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
      return ok;
    };
    tarjetas.forEach((t) => {
      const ok = pasa(t.dataset);
      t.hidden = !ok;
      if (ok) { visibles++; unidades += Math.max(parseInt(t.dataset.total, 10) || 0, 0); }
    });
    filasTabla.forEach((t) => {
      const ok = pasa(t.dataset);
      t.hidden = !ok;
      if (!ok) return;
      sumaTotal += parseInt(t.dataset.total, 10) || 0;
      sumaOtras += parseInt(t.dataset.otras, 10) || 0;
      Object.keys(t.dataset).forEach((k) => { if (/^u\d+$/.test(k)) sumaU[k.slice(1)] = (sumaU[k.slice(1)] || 0) + (parseInt(t.dataset[k], 10) || 0); });
    });
    // Totales al pie de la tabla y chips de resumen por ubicación
    const pie = (sel, v) => { const x = document.querySelector(sel); if (x) x.textContent = v; };
    pie("[data-t-n]", filasTabla.length ? [...filasTabla].filter((r) => !r.hidden).length : 0);
    pie("[data-t-total]", sumaTotal);
    pie("[data-t-otras]", sumaOtras);
    document.querySelectorAll("[data-t-u]").forEach((c) => { c.textContent = sumaU[c.dataset.tU] || 0; });
    document.querySelectorAll("[data-r-lugar]").forEach((b) => b.classList.toggle("on", !!lugar && b.dataset.rLugar === lugar));

    // Cuántos filtros hay activos (sin contar el texto) y resumen
    const activos = (categoria ? 1 : 0) + listaGrupos.reduce((n, g) => n + g.length, 0)
      + (pmin !== null || pmax !== null ? 1 : 0) + (lugar ? 1 : 0) + (soloStock ? 1 : 0) + (estado ? 1 : 0);
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
  if (selEstado) selEstado.addEventListener("change", aplicarFiltros);
  // Resumen por ubicación: tocar una la usa como filtro «Hay en…» (otra vez la quita)
  document.querySelectorAll("[data-r-lugar]").forEach((b) => b.addEventListener("click", () => {
    selLugar.value = selLugar.value === b.dataset.rLugar ? "" : b.dataset.rLugar;
    aplicarFiltros();
  }));
  // Tabla: tocar el título de una columna ordena por ella (otra vez, al revés)
  document.querySelectorAll("[data-orden-col]").forEach((th) => th.addEventListener("click", () => {
    const clave = th.dataset.ordenCol;
    const dir = porColumna && porColumna.clave === clave ? -porColumna.dir : (clave === "nombre" ? 1 : -1);
    porColumna = { clave, dir };
    ordenar();
  }));
  selOrden.addEventListener("change", () => {
    porColumna = null;
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
    if (selEstado) selEstado.value = "";
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
