// Cotización a medida (panel interno): piezas que NO están en el catálogo.
// - Cada pieza: nombre, descripción, medidas, fotos, costeo (filamento + peso + tiempo) y precio final.
// - El costo y el precio sugerido los calcula el servidor (misma fórmula que los productos del catálogo).
// - Las fotos se reducen en el navegador antes de subir (una foto de celular pesa varios MB).
(function () {
  const form = document.getElementById("form-medida");
  if (!form) return;

  const URL_CALCULO = form.dataset.calc;
  const MAX_FOTOS = parseInt(form.dataset.maxFotos, 10) || 6;
  const MAX_PIEZAS = parseInt(form.dataset.maxPiezas, 10) || 20;
  const LADO_MAX_PX = 1600;
  const filamentos = JSON.parse(document.getElementById("datos-filamentos").textContent);
  const previas = JSON.parse(document.getElementById("piezas-previas").textContent);
  const csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;

  const cont = document.getElementById("piezas");
  const tpl = document.getElementById("tpl-pieza");
  const btnAgregar = document.getElementById("agregar-pieza");
  const btnGuardar = document.getElementById("guardar-medida");
  const resumen = document.getElementById("resumen-medida");

  let contador = 0;
  const entero = (v) => parseInt(String(v || "").replace(/[^\d]/g, ""), 10) || 0;
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bs = (n) => "Bs. " + n;
  const campo = (nodo, nombre) => nodo.querySelector(`[data-f="${nombre}"]`);
  const piezas = () => [...cont.querySelectorAll(".p-pieza")];

  // ---------------------------------------------------------------- Fotos
  async function reducir(archivo) {
    // Devuelve el archivo reducido (JPEG, lado mayor 1600 px). Si el navegador no puede
    // leerlo, devuelve el original: el servidor también lo optimiza.
    if (!/^image\/(jpeg|png|webp)$/.test(archivo.type)) return archivo;
    try {
      const bmp = await createImageBitmap(archivo, { imageOrientation: "from-image" });
      const escala = Math.min(1, LADO_MAX_PX / Math.max(bmp.width, bmp.height));
      if (escala === 1 && archivo.size < 1.2e6) { bmp.close && bmp.close(); return archivo; }
      const lienzo = document.createElement("canvas");
      lienzo.width = Math.round(bmp.width * escala);
      lienzo.height = Math.round(bmp.height * escala);
      const ctx = lienzo.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, lienzo.width, lienzo.height);
      ctx.drawImage(bmp, 0, 0, lienzo.width, lienzo.height);
      bmp.close && bmp.close();
      const blob = await new Promise((ok) => lienzo.toBlob(ok, "image/jpeg", 0.85));
      if (!blob || blob.size >= archivo.size) return archivo;
      return new File([blob], archivo.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg", lastModified: archivo.lastModified });
    } catch (e) {
      return archivo;
    }
  }

  function pintarFotos(nodo) {
    const st = nodo._fotos;
    const prev = nodo.querySelector(".p-fotos-prev");
    st.urls.forEach((u) => URL.revokeObjectURL(u));
    st.urls = [];
    prev.textContent = "";
    st.archivos.forEach((f, k) => {
      const url = URL.createObjectURL(f);
      st.urls.push(url);
      const caja = document.createElement("div");
      caja.className = "p-foto" + (k === 0 ? " principal" : "");
      const img = document.createElement("img");
      img.src = url; img.alt = "";
      const quitar = document.createElement("button");
      quitar.type = "button"; quitar.className = "p-foto-x"; quitar.dataset.k = k;
      quitar.setAttribute("aria-label", "Quitar esta foto"); quitar.textContent = "✕";
      caja.append(img, quitar);
      if (k === 0) { const e = document.createElement("span"); e.className = "p-foto-tag"; e.textContent = "Principal"; caja.append(e); }
      prev.append(caja);
    });
    // El input real lleva los archivos ya reducidos (así es lo que se sube).
    const dt = new DataTransfer();
    st.archivos.forEach((f) => dt.items.add(f));
    campo(nodo, "fotos").files = dt.files;
    nodo.querySelector(".p-fotos-cuenta").textContent =
      st.archivos.length ? `${st.archivos.length} de ${MAX_FOTOS} fotos · la primera es la principal` : `Hasta ${MAX_FOTOS} fotos (opcional)`;
  }

  async function agregarFotos(nodo, lista) {
    const st = nodo._fotos;
    const cuenta = nodo.querySelector(".p-fotos-cuenta");
    const nuevos = [...lista].filter((f) => f.type.startsWith("image/") || /\.(jpe?g|png|webp|heic)$/i.test(f.name));
    if (!nuevos.length) return;
    cuenta.textContent = "Preparando fotos…";
    let sobran = 0;
    for (const f of nuevos) {
      if (st.archivos.length >= MAX_FOTOS) { sobran++; continue; }
      const clave = `${f.name}|${f.size}|${f.lastModified}`;
      if (st.claves.has(clave)) continue; // la misma foto elegida dos veces
      st.claves.add(clave);
      st.archivos.push(await reducir(f));
    }
    pintarFotos(nodo);
    if (sobran) cuenta.textContent = `Máximo ${MAX_FOTOS} fotos por pieza: se agregaron las primeras.`;
  }

  // ---------------------------------------------------------------- Costo (lo calcula el servidor)
  function pintarCalculo(nodo, d) {
    const caja = nodo.querySelector(".p-calculo");
    const btn = nodo.querySelector(".p-usar-sug");
    if (!d || !d.ok) {
      nodo.dataset.sugerido = ""; nodo.dataset.costo = "";
      btn.hidden = true;
      caja.className = "p-calculo" + (d && d.mensaje ? " pendiente" : "");
      const m = d && d.mensaje ? d.mensaje.charAt(0).toUpperCase() + d.mensaje.slice(1) + "." : "";
      caja.textContent = m;
      actualizarGanancia(nodo);
      return;
    }
    nodo.dataset.sugerido = d.sugerido; nodo.dataset.costo = d.costo;
    caja.className = "p-calculo listo";
    caja.innerHTML =
      `<div class="p-calc-desglose"><span>Filamento <b>${bs(d.material)}</b></span>` +
      `<span>Impresora (${esc(d.costo_hora)} Bs/h) <b>${bs(d.maquina)}</b></span>` +
      (d.fallas > 0 ? `<span>Fallas (${esc(d.fallas_pct)}%) <b>${bs(d.fallas)}</b></span>` : "") + `</div>` +
      `<div class="p-calc-fin"><span>Costo estimado <b>${bs(d.costo)}</b></span>` +
      `<span class="sug">Precio sugerido <b>${bs(d.sugerido)}</b>` + (d.margen_pct !== "0" ? ` <small>(margen ${esc(d.margen_pct)}%)</small>` : "") + `</span></div>` +
      (d.config_incompleta ? `<div class="p-calc-nota">Falta configurar el costo de la impresora: este costo solo incluye el filamento.</div>` : "");
    btn.hidden = false;
    btn.textContent = `Usar sugerido: ${bs(d.sugerido)}`;
    const precio = campo(nodo, "precio");
    if (!precio.value.trim() || precio.dataset.auto === "1") { precio.value = d.sugerido; precio.dataset.auto = "1"; }
    actualizarGanancia(nodo);
    actualizarTotal();
  }

  async function calcular(nodo) {
    const fd = new FormData();
    ["peso", "horas", "minutos", "filamento", "otro_nombre", "otro_costo"].forEach((c) => fd.append(c, campo(nodo, c).value));
    if (nodo._abort) nodo._abort.abort();
    nodo._abort = new AbortController();
    try {
      const r = await fetch(URL_CALCULO, { method: "POST", body: fd, headers: { "X-CSRFToken": csrf }, credentials: "same-origin", signal: nodo._abort.signal });
      if (!r.ok) throw new Error("http " + r.status);
      pintarCalculo(nodo, await r.json());
    } catch (e) {
      if (e.name === "AbortError") return;
      pintarCalculo(nodo, { ok: false, mensaje: "no se pudo calcular ahora (revisa tu conexión), pero igual puedes escribir el precio" });
    }
  }
  const programarCalculo = (nodo) => { clearTimeout(nodo._t); nodo._t = setTimeout(() => calcular(nodo), 350); };

  // ---------------------------------------------------------------- Ganancia y total
  function actualizarGanancia(nodo) {
    const el = nodo.querySelector(".p-ganancia");
    const costo = parseInt(nodo.dataset.costo, 10), precio = entero(campo(nodo, "precio").value);
    if (!costo || !precio) { el.textContent = ""; el.className = "p-ganancia"; return; }
    const g = precio - costo;
    if (g < 0) { el.className = "p-ganancia mal"; el.textContent = `⚠ Estás cotizando por debajo del costo (pierdes ${bs(-g)} por unidad).`; }
    else { el.className = "p-ganancia bien"; el.textContent = `Ganancia por unidad: ${bs(g)} (${Math.round((g / costo) * 100)}% sobre el costo).`; }
  }

  function actualizarTotal() {
    let subtotal = 0, unidades = 0;
    piezas().forEach((n) => {
      const c = entero(campo(n, "cantidad").value), p = entero(campo(n, "precio").value);
      subtotal += c * p; unidades += c;
    });
    const extra = Math.min(entero(form.elements["descuento_extra"].value), subtotal);
    resumen.innerHTML =
      `<div class="rc-total"><small>Total</small><b>${bs(subtotal - extra)}</b></div>` +
      `<div class="rc-info"><span>${piezas().length} pieza${piezas().length === 1 ? "" : "s"} · ${unidades} unidad${unidades === 1 ? "" : "es"}</span>` +
      (extra ? `<span class="desc">Desc. adicional − ${bs(extra)}</span>` : "") + `</div>`;
  }

  // ---------------------------------------------------------------- Piezas
  function renumerar() {
    const todas = piezas();
    todas.forEach((n, k) => (n.querySelector(".p-pieza-n").textContent = k + 1));
    todas.forEach((n) => (n.querySelector(".p-quitar-pieza").hidden = todas.length === 1));
    btnAgregar.disabled = todas.length >= MAX_PIEZAS;
  }

  function crearPieza(previa) {
    const i = ++contador;
    const nodo = tpl.content.firstElementChild.cloneNode(true);
    nodo.querySelector("[name=pieza]").value = i;
    nodo.querySelectorAll("[data-f]").forEach((el) => (el.name = `p${i}_${el.dataset.f}`));
    nodo._fotos = { archivos: [], claves: new Set(), urls: [] };

    const sel = campo(nodo, "filamento");
    filamentos.forEach((f) => sel.add(new Option(`${f.nombre} · Bs. ${parseFloat(f.costo_kg)}/kg`, f.id)));
    sel.add(new Option("Otro (escribir el costo)…", "otro"));

    if (previa) Object.keys(previa).forEach((k) => { const el = campo(nodo, k); if (el && el.type !== "file") el.value = previa[k]; });
    cont.append(nodo);
    alternarOtro(nodo);
    pintarFotos(nodo);
    renumerar();
    if (previa) programarCalculo(nodo);
    return nodo;
  }

  function alternarOtro(nodo) {
    const otro = campo(nodo, "filamento").value === "otro";
    nodo.querySelector(".p-otro").hidden = !otro;
    campo(nodo, "otro_costo").required = otro;
  }

  // ---------------------------------------------------------------- Eventos (por delegación)
  form.addEventListener("input", (e) => {
    const nodo = e.target.closest(".p-pieza");
    if (nodo) {
      const f = e.target.dataset.f;
      if (f === "precio") { delete e.target.dataset.auto; actualizarGanancia(nodo); }
      if (["peso", "horas", "minutos", "otro_costo", "otro_nombre"].includes(f)) programarCalculo(nodo);
    }
    actualizarTotal();
  });

  form.addEventListener("change", (e) => {
    const nodo = e.target.closest(".p-pieza");
    if (!nodo) return;
    const f = e.target.dataset.f;
    if (f === "filamento") { alternarOtro(nodo); programarCalculo(nodo); }
    if (f === "fotos") { agregarFotos(nodo, e.target.files); }
  });

  form.addEventListener("click", (e) => {
    const nodo = e.target.closest(".p-pieza");
    if (!nodo) return;
    if (e.target.closest(".p-usar-sug")) {
      const precio = campo(nodo, "precio");
      precio.value = nodo.dataset.sugerido; precio.dataset.auto = "1";
      actualizarGanancia(nodo); actualizarTotal();
    } else if (e.target.closest(".p-foto-x")) {
      const k = parseInt(e.target.closest(".p-foto-x").dataset.k, 10);
      nodo._fotos.archivos.splice(k, 1);
      nodo._fotos.claves = new Set(); // permite volver a elegir una foto que se quitó
      pintarFotos(nodo);
    } else if (e.target.closest(".p-quitar-pieza")) {
      if (piezas().length > 1) {
        nodo._fotos.urls.forEach((u) => URL.revokeObjectURL(u));
        nodo.remove(); renumerar(); actualizarTotal();
      }
    }
  });

  btnAgregar.addEventListener("click", () => {
    if (piezas().length >= MAX_PIEZAS) return;
    const nodo = crearPieza();
    nodo.scrollIntoView({ behavior: "smooth", block: "start" });
    campo(nodo, "nombre").focus({ preventScroll: true });
  });

  // Evita crear la cotización dos veces si se toca el botón mientras suben las fotos.
  form.addEventListener("submit", () => {
    btnGuardar.disabled = true;
    btnGuardar.textContent = "Guardando…";
  });
  window.addEventListener("pageshow", () => { btnGuardar.disabled = false; btnGuardar.textContent = "Crear cotización"; });

  // ---------------------------------------------------------------- Inicio
  if (previas.length) previas.forEach((p) => crearPieza(p)); else crearPieza();
  actualizarTotal();
})();
