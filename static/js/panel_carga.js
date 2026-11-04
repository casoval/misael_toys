// Carga de stock en lote: buscar/filtrar, rellenar una columna, Enter baja a la celda de abajo y contador.
(function () {
  const form = document.querySelector("[data-carga]");
  if (!form) return;
  const filas = [...form.querySelectorAll("tr[data-fila]")];
  const q = form.querySelector("[data-c-q]");
  const cat = form.querySelector("[data-c-cat]");
  const soloVacios = form.querySelector("[data-c-vacios]");
  const soloEscritos = form.querySelector("[data-c-escritos]");
  const contador = form.querySelector("[data-c-contador]");
  const visiblesEl = form.querySelector("[data-c-visibles]");
  const norm = (t) => (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const celdasDe = (f) => [...f.querySelectorAll("[data-celda]")];
  const escrita = (f) => celdasDe(f).some((i) => i.value.trim() !== "");

  function filtrar() {
    const texto = norm(q.value.trim());
    let n = 0;
    filas.forEach((f) => {
      const d = f.dataset;
      let ok = true;
      if (texto && !(norm(d.nombre).includes(texto) || norm(d.codigo).includes(texto))) ok = false;
      if (ok && cat.value && d.categoria !== cat.value) ok = false;
      if (ok && soloVacios.checked && parseInt(d.total, 10) > 0) ok = false;
      if (ok && soloEscritos.checked && !escrita(f)) ok = false;
      f.hidden = !ok;
      if (ok) n++;
    });
    visiblesEl.textContent = "(" + n + " de " + filas.length + ")";
  }

  function contar() {
    const cambios = filas.reduce((n, f) => n + celdasDe(f).filter((i) => i.value.trim() !== "").length, 0);
    const prods = filas.filter(escrita).length;
    contador.textContent = cambios
      ? cambios + " cantidad" + (cambios === 1 ? "" : "es") + " en " + prods + " producto" + (prods === 1 ? "" : "s") + " por guardar"
      : "Aún no escribiste nada.";
    form.querySelector("[data-c-guardar]").disabled = cambios === 0;
    filas.forEach((f) => f.classList.toggle("editada", escrita(f)));
  }

  [q, cat, soloVacios, soloEscritos].forEach((e) => e.addEventListener("input", filtrar));
  form.addEventListener("input", (e) => { if (e.target.matches("[data-celda]")) { contar(); if (soloEscritos.checked) filtrar(); } });

  // Rellenar una columna entera (solo los productos que se ven ahora)
  form.querySelectorAll("th [data-r-btn]").forEach((b) => b.addEventListener("click", () => {
    const th = b.closest("th");
    const v = th.querySelector("[data-r-valor]").value.trim();
    const col = th.dataset.col;
    filas.filter((f) => !f.hidden).forEach((f) => {
      const c = f.querySelector('[data-celda][data-col="' + col + '"]');
      if (c) c.value = v;
    });
    contar();
  }));

  // Enter = bajar a la misma columna de la siguiente fila visible (no envía el formulario)
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.target.matches("[data-celda]")) {
      e.preventDefault();
      const fila = e.target.closest("tr");
      let sig = fila.nextElementSibling;
      while (sig && sig.hidden) sig = sig.nextElementSibling;
      const destino = sig && sig.querySelector('[data-celda][data-col="' + e.target.dataset.col + '"]');
      if (destino) { destino.focus(); destino.select(); }
    } else if (e.target.matches("[data-c-q], [data-r-valor], input[name=nota]")) {
      e.preventDefault();
    }
  });
  // Al enfocar una celda, seleccionar su contenido para sobrescribir rápido
  form.addEventListener("focusin", (e) => { if (e.target.matches("[data-celda]")) e.target.select(); });
  // Evitar que la rueda del mouse cambie los números por accidente
  form.addEventListener("wheel", (e) => { if (document.activeElement && document.activeElement.type === "number") document.activeElement.blur(); }, { passive: true });

  window.addEventListener("beforeunload", (e) => {
    if (filas.some(escrita) && !form.dataset.enviando) { e.preventDefault(); e.returnValue = ""; }
  });
  form.addEventListener("submit", () => { form.dataset.enviando = "1"; });

  filtrar();
  contar();
})();
