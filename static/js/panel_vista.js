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
  const conUnidades = barra.dataset.fUnidades === "1";
  let categoria = "";

  function numero(inp) {
    const v = parseInt(inp.value, 10);
    return Number.isNaN(v) ? null : v;
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
      if (q && !norm(d.nombre).includes(q)) ok = false;
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
  chkStock.addEventListener("change", aplicarFiltros);

  el("[data-f-limpiar]").addEventListener("click", () => {
    categoria = "";
    barra.querySelectorAll("[data-f-cat]").forEach((x) => x.classList.toggle("on", x.dataset.fCat === ""));
    barra.querySelectorAll("[data-f-valor]").forEach((x) => x.classList.remove("on"));
    entradaQ.value = entradaMin.value = entradaMax.value = "";
    selLugar.value = "";
    chkStock.checked = false;
    aplicarFiltros();
  });

  // Enter en el buscador no debe enviar el formulario de venta/cotización
  entradaQ.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
  [entradaMin, entradaMax].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); }));

  aplicarFiltros();
})();
