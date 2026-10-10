// Vista de productos del panel: tarjetas / lista (se recuerda) + búsqueda al escribir.
(function () {
  const KEY = "panel-vista";
  const contenedores = document.querySelectorAll("[data-vista-contenedor]");
  const botones = document.querySelectorAll("[data-vista]");

  function leer() { try { return localStorage.getItem(KEY) || "tarjetas"; } catch (e) { return "tarjetas"; } }
  function guardar(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function aplicar(v) {
    contenedores.forEach((c) => {
      c.classList.toggle("vista-tarjetas", v === "tarjetas");
      c.classList.toggle("vista-lista", v === "lista");
    });
    botones.forEach((b) => b.classList.toggle("on", b.dataset.vista === v));
  }
  botones.forEach((b) => b.addEventListener("click", () => { guardar(b.dataset.vista); aplicar(b.dataset.vista); }));
  aplicar(leer());

  // Búsqueda instantánea (sin tildes ni mayúsculas)
  const norm = (t) => (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  document.querySelectorAll("input[data-filtro]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const q = norm(inp.value.trim());
      document.querySelectorAll("[data-vista-contenedor] .p-prod").forEach((t) => {
        t.hidden = q !== "" && !norm(t.dataset.nombre).includes(q);
      });
    });
    if (inp.value) inp.dispatchEvent(new Event("input"));
  });
})();
