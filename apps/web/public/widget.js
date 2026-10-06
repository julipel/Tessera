/*
 * Виджет AI-консультанта для сайта тенанта (ADR-0022):
 *   <script src="https://<чат>/widget.js" data-key="wk_…" async></script>
 * Кнопка открывает iframe с чатом `/widget?key=…`; «Свернуть» внутри чата присылает
 * postMessage `{type: "tessera:collapse"}`. Без зависимостей и без <style>: стили — через
 * element.style и без innerHTML, чтобы виджет работал при строгом style-src и Trusted Types.
 */
(function () {
  "use strict";
  if (window.__tesseraWidget) return;

  var script =
    document.currentScript || document.querySelector('script[src*="widget.js"][data-key]');
  var key = script && script.getAttribute("data-key");
  if (!key) {
    console.error("[tessera] widget.js: не задан data-key");
    return;
  }
  window.__tesseraWidget = true;

  var chatOrigin = new URL(script.src, location.href).origin;
  var frameUrl = chatOrigin + "/widget?key=" + encodeURIComponent(key);
  var mobile = window.matchMedia("(max-width: 639px)");
  var Z = "2147483000";
  // Подписи кнопки и iframe — по языку браузера (ru/en/sv, иначе ru): конфиг тенанта виджет
  // не загружает, язык диалога выбирает чат во фрейме (ADR-0025).
  var TEXT = {
    ru: { open: "Открыть чат", close: "Закрыть чат", frame: "Чат с консультантом" },
    en: { open: "Open chat", close: "Close chat", frame: "Chat with an assistant" },
    sv: { open: "Öppna chatten", close: "Stäng chatten", frame: "Chatt med en assistent" },
  };
  var text = TEXT[(navigator.language || "").toLowerCase().split(/[-_]/)[0]] || TEXT.ru;

  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.setAttribute("aria-label", text.open);
  launcher.setAttribute("aria-expanded", "false");
  launcher.setAttribute("data-tessera", "launcher");
  launcher.appendChild(chatIcon());
  assign(launcher.style, {
    position: "fixed",
    right: "20px",
    bottom: "20px",
    width: "56px",
    height: "56px",
    borderRadius: "50%",
    border: "none",
    padding: "0",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "#1f2328",
    color: "#ffffff",
    boxShadow: "0 4px 16px rgba(0,0,0,.25)",
    cursor: "pointer",
    zIndex: Z,
  });

  var panel = document.createElement("div");
  panel.setAttribute("data-tessera", "panel");
  var iframe = null;
  var open = false;

  function layout() {
    var full = mobile.matches;
    assign(panel.style, {
      position: "fixed",
      display: open ? "block" : "none",
      zIndex: Z,
      overflow: "hidden",
      background: "#ffffff",
      inset: full ? "0" : "auto",
      right: full ? "0" : "20px",
      bottom: full ? "0" : "88px",
      width: full ? "100%" : "400px",
      height: full ? "100%" : "min(640px, calc(100vh - 108px))",
      borderRadius: full ? "0" : "12px",
      boxShadow: full ? "none" : "0 8px 32px rgba(0,0,0,.2)",
    });
    // На телефоне чат на весь экран — кнопка закрыла бы поле ввода, сворачивает шапка чата.
    launcher.style.display = open && full ? "none" : "flex";
  }

  function setOpen(value) {
    open = value;
    if (open && !iframe) {
      // iframe создаётся при первом открытии и дальше только скрывается — диалог не теряется.
      iframe = document.createElement("iframe");
      iframe.src = frameUrl;
      iframe.title = text.frame;
      iframe.setAttribute("data-tessera", "frame");
      assign(iframe.style, { width: "100%", height: "100%", border: "0", display: "block" });
      panel.appendChild(iframe);
    }
    launcher.setAttribute("aria-expanded", String(open));
    launcher.setAttribute("aria-label", open ? text.close : text.open);
    layout();
    if (open) iframe.focus();
    else launcher.focus();
  }

  launcher.addEventListener("click", function () {
    setOpen(!open);
  });
  window.addEventListener("message", function (event) {
    if (!iframe || event.source !== iframe.contentWindow || event.origin !== chatOrigin) return;
    if (event.data && event.data.type === "tessera:collapse") setOpen(false);
  });
  if (mobile.addEventListener) mobile.addEventListener("change", layout);

  function chatIcon() {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    var attrs = {
      viewBox: "0 0 24 24",
      width: "28",
      height: "28",
      "aria-hidden": "true",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linejoin": "round",
    };
    for (var name in attrs) svg.setAttribute(name, attrs[name]);
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z");
    svg.appendChild(path);
    return svg;
  }

  function assign(style, values) {
    for (var name in values) style[name] = values[name];
  }

  function mount() {
    layout();
    document.body.appendChild(panel);
    document.body.appendChild(launcher);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
