// Starts Swagger UI on /docs (external file: the CSP allows script-src 'self' only, no inline script).
(function () {
  var el = document.getElementById("ui");
  window.ui = SwaggerUIBundle({
    url: el.getAttribute("data-spec"),
    dom_id: "#ui",
    deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset || SwaggerUIBundle.presets.apis],
  });
})();
