// Monatrain : rafraichit les listes de trains toutes les 60 s quand la page est visible,
// applique les prereglages de jours et envoie le choix de gare sans bouton.
(function () {
  "use strict";

  var REFRESH_MS = 60000;

  function refresh(box) {
    if (document.visibilityState !== "visible") return;
    if (box.querySelector("details[open]")) return;
    fetch(box.dataset.refresh, { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) {
        return res.ok ? res.text() : null;
      })
      .then(function (html) {
        if (html !== null) box.innerHTML = html;
      })
      .catch(function () {
        /* reseau coupe : on garde l'affichage actuel */
      });
  }

  document.querySelectorAll("[data-refresh]").forEach(function (box) {
    setInterval(function () {
      refresh(box);
    }, REFRESH_MS);
  });

  document.querySelectorAll("[data-days]").forEach(function (button) {
    button.addEventListener("click", function () {
      var wanted = button.dataset.days.split(",");
      document.querySelectorAll('input[name="jours"]').forEach(function (box) {
        box.checked = wanted.indexOf(box.value) !== -1;
      });
    });
  });

  document.querySelectorAll("form[data-autosubmit] select").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.value) select.form.submit();
    });
  });
})();
