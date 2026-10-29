/* ConcreTOPrint — the only script on the site.
   It stores nothing (no cookies, no localStorage) and contacts no server. */
(function () {
  var root = document.documentElement;
  root.classList.remove("no-js");
  root.classList.add("js");

  document.addEventListener("DOMContentLoaded", function () {
    // Mobile menu
    var toggle = document.querySelector(".nav-toggle");
    var nav = document.getElementById("site-nav");
    if (toggle && nav) {
      var close = function () {
        nav.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      };
      toggle.addEventListener("click", function () {
        var open = nav.classList.toggle("open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
      nav.addEventListener("click", function (e) {
        if (e.target.closest("a")) close();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && nav.classList.contains("open")) {
          close();
          toggle.focus();
        }
      });
    }

    // Current year in the footer
    var years = document.querySelectorAll("[data-year]");
    for (var i = 0; i < years.length; i++) {
      years[i].textContent = String(new Date().getFullYear());
    }
  });
})();
