// Formulario dinámico de venta / cotización del panel interno.
(function () {
  const form = document.getElementById("form-lineas");
  if (!form) return;
  const esVenta = form.dataset.tipo === "venta";
  const productos = JSON.parse(document.getElementById("datos-productos").textContent);
  const ubic = JSON.parse(document.getElementById("datos-ubicaciones").textContent);
  const previas = JSON.parse(document.getElementById("filas-previas").textContent);
  const porId = {};
  productos.forEach((p) => (porId[p.id] = p));

  const cont = document.getElementById("filas");
  const tpl = document.getElementById("tpl-fila");

  const avisar = () => form.dispatchEvent(new Event("change", { bubbles: true }));

  function pintarUbicaciones(fila, elegida) {
    const sel = fila.querySelector("[name=ubicacion]");
    if (!sel) return;
    const p = porId[fila.querySelector("[name=producto]").value];
    sel.innerHTML = "";
    const ids = p ? Object.keys(p.stock) : [];
    if (!ids.length) {
      sel.innerHTML = '<option value="">' + (p ? "Sin stock" : "—") + "</option>";
    } else {
      ids.forEach((id) => {
        const o = document.createElement("option");
        o.value = id;
        o.textContent = ubic[id] + " (hay " + p.stock[id] + ")";
        sel.appendChild(o);
      });
      if (elegida && p.stock[elegida]) sel.value = elegida;
    }
    topeCantidad(fila);
  }

  function topeCantidad(fila) {
    const sel = fila.querySelector("[name=ubicacion]");
    const cant = fila.querySelector("[name=cantidad]");
    const p = porId[fila.querySelector("[name=producto]").value];
    if (sel && p && p.stock[sel.value]) cant.max = p.stock[sel.value];
    else cant.removeAttribute("max");
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
      pintarUbicaciones(fila);
    });
    const selUb = fila.querySelector("[name=ubicacion]");
    if (selUb) selUb.addEventListener("change", () => topeCantidad(fila));
    fila.querySelector(".p-quitar").addEventListener("click", () => {
      fila.remove();
      if (!cont.children.length) agregarFila();
      avisar();
    });

    if (datos) {
      selProd.value = datos.producto;
      pintarUbicaciones(fila, datos.ubicacion);
      fila.querySelector("[name=cantidad]").value = datos.cantidad;
      fila.querySelector("[name=precio]").value = datos.precio;
    } else {
      pintarUbicaciones(fila);
    }
    cont.appendChild(fila);
  }

  document.getElementById("agregar-fila").addEventListener("click", () => {
    agregarFila();
    avisar();
  });

  if (previas.length) previas.forEach(agregarFila);
  else agregarFila();
})();
