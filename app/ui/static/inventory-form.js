// Inventory-formulier: toon alleen de velden van de gekozen bron.
(function () {
  "use strict";
  const form = document.querySelector("[data-inventory-form]");
  if (!form) return;
  const update = () => {
    const source = form.querySelector('input[name="source_type"]:checked').value;
    form.querySelectorAll("[data-source]").forEach((el) => {
      el.hidden = el.dataset.source !== source;
    });
  };
  form.querySelectorAll('input[name="source_type"]').forEach((radio) => {
    radio.addEventListener("change", update);
  });
})();
