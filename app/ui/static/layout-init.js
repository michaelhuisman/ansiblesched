// Runs before the first paint: restore a collapsed sidebar, so the page does not jump.
try {
  if (localStorage.getItem("lamplighter.sidebar") === "collapsed") {
    document.documentElement.classList.add("sidebar-collapsed");
  }
} catch (e) { /* storage blocked: default (expanded) */ }
