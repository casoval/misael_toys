// Formulario dinámico de venta / cotización del panel interno.
// - Tarjetas de producto (foto, precio y stock): tocar la tarjeta agrega o suma 1;
//   el botón «−» de la tarjeta resta 1 (y quita el producto al llegar a 0).
// - Detalle editable (producto, lugar, cantidad, precio) por si hay que ajustar.
// - NUNCA se bloquea una venta por falta de stock: solo se avisa.
(function () {
  const form = document.getElementById("form-lineas");
  if (!form) return;
  const esVenta = form.dataset.tipo === "venta";
  const productos = JSON.parse(document.getElementById("datos-productos").textContent);
  const ubicLista = JSON.parse(document.getElementById("datos-ubicaciones").textContent); // [{id, nombre}] en orden
  const previas = JSON.parse(document.getElementById("filas-previas").textContent);
  const ubic = {};
  ubicLista.forEach((u) => (ubic[String(u.id)] = u.nombre));
  const idsUbic = ubicLista.map((u) => String(u.id));
  const porId = {};
  productos.forEach((p) => (porId[p.id] = p));

  const cont = document.getElementById("filas");
  const picker = document.getElementById("picker");
  const aviso = document.getElementById("picker-aviso");
  const tpl = document.getElementById("tpl-fila");

  const avisar = () => form.dispatchEvent(new Event("change", { bubbles: true }));
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const filasActuales = () => [...cont.querySelectorAll(".p-fila-form")];
  const prodDeFila = (f) => f.querySelector("[name=producto]").value;
  const entero = (v) => parseInt(v, 10) || 0;

  let temporizador;
  function mensaje(texto, error) {
    aviso.textContent = texto;
    aviso.classList.toggle("error", !!error);
    clearTimeout(temporizador);
    temporizador = setTimeout(() => (aviso.textContent = ""), 2500);
  }

  // ---------- Tarjetas ----------
  function crearTarjeta(p) {
    const b = document.createElement("div"); // div (no button) porque lleva un botón «−» adentro
    b.className = "p-prod" + (p.total <= 0 ? " agotado" : "");
    b.setAttribute("role", "button");
    b.tabIndex = 0;
    b.dataset.id = p.id;
    b.dataset.nombre = p.nombre;
    const lugares = idsUbic
      .filter((id) => p.stock[id] !== 0)
      .map((id) => "<span" + (p.stock[id] < 0 ? ' class="neg"' : "") + ">" + esc(ubic[id]) + " <b>" + p.stock[id] + "</b></span>")
      .join("");
    b.innerHTML =
      '<span class="p-prod-foto">' + (p.foto ? '<img src="' + esc(p.foto) + '" alt="" loading="lazy">' : '<i class="p-sinfoto"></i>') + "</span>" +
      '<b class="p-badge' + (p.total <= 0 ? " cero" : "") + '">' + p.total + "</b>" +
      '<span class="p-enc" hidden><button type="button" class="p-menos" aria-label="Quitar uno">−</button><b></b></span>' +
      '<span class="p-prod-cuerpo"><span class="p-prod-nombre">' + esc(p.nombre) + "</span>" +
      '<span class="p-prod-precio">Bs. ' + esc(p.precio) + "</span>" +
      '<span class="p-prod-lugares">' + (lugares || '<span class="sin">Sin stock</span>') + "</span></span>";
    b.addEventListener("click", (e) => {
      if (e.target.closest(".p-menos")) { quitarUno(p.id); return; }
      agregarProducto(p.id);
    });
    b.addEventListener("keydown", (e) => {
      if (e.target !== b) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); agregarProducto(p.id); }
      if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); quitarUno(p.id); }
    });
    return b;
  }
  productos.forEach((p) => picker.appendChild(crearTarjeta(p)));

  // Cuántas unidades de cada producto hay ya en el detalle (se ve sobre la tarjeta)
  function contadores() {
    const suma = {};
    filasActuales().forEach((f) => {
      const id = prodDeFila(f);
      if (id) suma[id] = (suma[id] || 0) + entero(f.querySelector("[name=cantidad]").value);
    });
    picker.querySelectorAll(".p-prod").forEach((t) => {
      const n = suma[t.dataset.id] || 0;
      const e = t.querySelector(".p-enc");
      e.hidden = n === 0;
      e.querySelector("b").textContent = "×" + n;
      t.classList.toggle("elegido", n > 0);
    });
  }
  form.addEventListener("input", contadores);
  form.addEventListener("change", contadores);

  function mejorLugar(p) {
    // El lugar con más stock; si no hay en ninguno, el primero de la lista.
    let mejor = idsUbic[0] || "";
    idsUbic.forEach((id) => { if (p.stock[id] > (p.stock[mejor] ?? -Infinity)) mejor = id; });
    return mejor;
  }

  function agregarProducto(id) {
    const p = porId[id];
    const filas = filasActuales().filter((f) => prodDeFila(f) == id);
    if (filas.length) {
      const f = filas[filas.length - 1];
      const c = f.querySelector("[name=cantidad]");
      c.value = entero(c.value) + 1;
      avisoFila(f);
      f.classList.add("flash");
      setTimeout(() => f.classList.remove("flash"), 500);
    } else {
      agregarFila({ producto: String(id), ubicacion: esVenta ? mejorLugar(p) : "", cantidad: 1, precio: p.precio });
    }
    mensaje("✔ " + p.nombre);
    avisar();
  }

  function quitarUno(id) {
    const p = porId[id];
    const filas = filasActuales().filter((f) => prodDeFila(f) == id);
    if (!filas.length) return;
    const f = filas[filas.length - 1];
    const c = f.querySelector("[name=cantidad]");
    const n = entero(c.value) - 1;
    if (n <= 0) {
      f.remove();
      mensaje("✖ Quitaste " + p.nombre);
    } else {
      c.value = n;
      avisoFila(f);
      f.classList.add("flash");
      setTimeout(() => f.classList.remove("flash"), 500);
      mensaje("− " + p.nombre + " (quedan " + n + ")");
    }
    avisar();
  }

  // ---------- Detalle (filas editables) ----------
  function pintarUbicaciones(fila, elegida) {
    const sel = fila.querySelector("[name=ubicacion]");
    if (!sel) return;
    const p = porId[fila.querySelector("[name=producto]").value];
    sel.innerHTML = "";
    if (!p) {
      sel.innerHTML = '<option value="">—</option>';
      return;
    }
    idsUbic.forEach((id) => {
      const o = document.createElement("option");
      o.value = id;
      o.textContent = ubic[id] + " (hay " + p.stock[id] + ")";
      sel.appendChild(o);
    });
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "— No descontar de ningún lugar —";
    sel.appendChild(o);
    const validos = [...sel.options].map((x) => x.value);
    sel.value = elegida !== undefined && validos.includes(String(elegida)) ? String(elegida) : mejorLugar(p);
  }

  // Aviso suave (nunca bloquea) cuando se vende más de lo que el sistema tiene
  function avisoFila(fila) {
    const el = fila.querySelector(".f-aviso");
    if (!el || !esVenta) return;
    const p = porId[prodDeFila(fila)];
    const sel = fila.querySelector("[name=ubicacion]");
    if (!p || !sel) { el.textContent = ""; return; }
    const cant = entero(fila.querySelector("[name=cantidad]").value);
    if (sel.value === "") {
      el.textContent = "No se descontará stock de ningún lugar.";
      return;
    }
    const hay = p.stock[sel.value] ?? 0;
    el.textContent = cant > hay
      ? "⚠ En " + ubic[sel.value] + " hay " + hay + ". Se vende igual; el stock quedará en " + (hay - cant) + " (por cubrir)."
      : "";
  }

  function pintarFoto(fila) {
    const img = fila.querySelector(".f-foto img");
    const p = porId[fila.querySelector("[name=producto]").value];
    if (p && p.foto) { img.src = p.foto; img.hidden = false; } else { img.removeAttribute("src"); img.hidden = true; }
  }

  function agregarFila(datos) {
    const fila = tpl.content.firstElementChild.cloneNode(true);
    const selProd = fila.querySelector("[name=producto]");
    selProd.innerHTML = '<option value="">— elige un producto —</option>';
    productos.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.nombre + (esVenta ? "  ·  hay " + p.total : "");
      selProd.appendChild(o);
    });

    selProd.addEventListener("change", () => {
      const p = porId[selProd.value];
      fila.querySelector("[name=precio]").value = p ? p.precio : "";
      pintarUbicaciones(fila, p && esVenta ? mejorLugar(p) : "");
      pintarFoto(fila);
      avisoFila(fila);
    });
    fila.addEventListener("input", () => avisoFila(fila));
    fila.addEventListener("change", () => avisoFila(fila));
    fila.querySelector(".p-quitar").addEventListener("click", () => {
      fila.remove();
      avisar();
    });

    if (datos) {
      selProd.value = datos.producto;
      pintarUbicaciones(fila, datos.ubicacion);
      fila.querySelector("[name=cantidad]").value = datos.cantidad;
      fila.querySelector("[name=precio]").value = datos.precio;
      pintarFoto(fila);
    } else {
      pintarUbicaciones(fila);
    }
    cont.appendChild(fila);
    avisoFila(fila);
    contadores();
  }

  document.getElementById("agregar-fila").addEventListener("click", () => {
    agregarFila();
    avisar();
  });

  previas.forEach(agregarFila);
  contadores();
})();
