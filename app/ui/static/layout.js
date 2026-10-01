// Sidebar: collapse to icons on wide screens (remembered per browser), slide-in menu on
// narrow screens. Also closes the user menu on an outside click or Escape.
(function () {
  const root = document.documentElement;
  const narrow = window.matchMedia("(max-width: 900px)");

  function remember(collapsed) {
    try {
      localStorage.setItem("lamplighter.sidebar", collapsed ? "collapsed" : "expanded");
    } catch (e) { /* storage blocked: not remembered */ }
  }

  function toggle() {
    if (narrow.matches) {
      root.classList.toggle("sidebar-open");
    } else {
      remember(root.classList.toggle("sidebar-collapsed"));
    }
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-sidebar-toggle]")) {
      toggle();
    } else if (event.target.closest("[data-sidebar-collapse]")) {
      remember(root.classList.toggle("sidebar-collapsed"));
    } else if (event.target.closest("[data-sidebar-close]")) {
      root.classList.remove("sidebar-open");
    }
    const menu = document.querySelector("details.usermenu[open]");
    if (menu && !menu.contains(event.target)) {
      menu.removeAttribute("open");
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    root.classList.remove("sidebar-open");
    const menu = document.querySelector("details.usermenu[open]");
    if (menu) menu.removeAttribute("open");
  });

  narrow.addEventListener("change", function () { root.classList.remove("sidebar-open"); });
})();
