/* Runtime injected into a chat file when the canvas opens it with ?view=deck|doc|print.
   deck: one slide at a time, scaled to fit, keys + postMessage control. doc: a page look. extract: data for PPTX / DOCX / Markdown export.
   The page is sandboxed without network, so this only talks to its parent through postMessage. */
(function () {
  var Q = new URLSearchParams(location.search), VIEW = Q.get("view") || "", W = 1280, H = 720;
  var slides = [].slice.call(document.querySelectorAll("section.slide")), cur = 0, grid = false, st = document.createElement("style");
  document.head.appendChild(st);
  var post = function (m) { try { parent.postMessage(Object.assign({ aihub: 1 }, m), "*"); } catch (e) {} };
  var title = function (s) { var h = s.querySelector("h1,h2,h3"); return (h ? h.textContent : s.textContent).trim().replace(/\s+/g, " ").slice(0, 70); };
  var info = function () { post({ t: "info", n: slides.length, i: cur, titles: slides.map(title), grid: grid }); };

  function layout() {
    if (VIEW !== "deck" || !slides.length) return;
    var vw = innerWidth, vh = innerHeight;
    if (grid) {
      var cols = Math.max(1, Math.floor(vw / 300)), sc = (vw - 24 - cols * 16) / cols / W;
      slides.forEach(function (s, i) { s.style.cssText += ";position:relative;display:block;opacity:1;transform:scale(" + sc + ");transform-origin:0 0;margin:0 " + (W * sc - W + 16) + "px " + (H * sc - H + 16) + "px 0;outline:" + (i === cur ? "3px solid #d97757" : "1px solid #8884") + ";cursor:pointer"; });
      document.body.style.cssText = "margin:0;padding:12px;display:flex;flex-wrap:wrap;align-content:flex-start;overflow:auto;height:auto;background:#1b1a18";
      document.documentElement.style.overflow = "auto";
    } else {
      var sc2 = Math.min(vw / W, vh / H);
      document.body.style.cssText = "margin:0;padding:0;overflow:hidden;height:100vh;background:#111";
      document.documentElement.style.overflow = "hidden";
      slides.forEach(function (s, i) {
        s.style.cssText += ";position:absolute;left:" + (vw - W * sc2) / 2 + "px;top:" + (vh - H * sc2) / 2 + "px;transform:scale(" + sc2 + ");transform-origin:0 0;margin:0;outline:0;cursor:default;" +
          "transition:opacity .35s ease;opacity:" + (i === cur ? 1 : 0) + ";pointer-events:" + (i === cur ? "auto" : "none");
      });
    }
  }
  function go(i) { cur = Math.max(0, Math.min(slides.length - 1, i)); layout(); info(); }

  if (VIEW === "deck") {
    st.textContent = "section.slide aside.notes{display:none!important}section.slide{overflow:hidden;flex:none}";
    slides.forEach(function (s, i) { s.addEventListener("click", function () { if (grid) { grid = false; go(i); } }); });
    addEventListener("resize", layout); addEventListener("load", layout);
    if (window.ResizeObserver) new ResizeObserver(layout).observe(document.documentElement);          // the frame may be 0 x 0 when this first runs
    addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " " || e.key === "ArrowDown") { e.preventDefault(); go(cur + 1); }
      else if (e.key === "ArrowLeft" || e.key === "PageUp" || e.key === "ArrowUp") { e.preventDefault(); go(cur - 1); }
      else if (e.key === "Home") go(0); else if (e.key === "End") go(slides.length - 1);
      else if (e.key === "g") { grid = !grid; layout(); info(); }
    });
    layout(); info();
  } else if (VIEW === "doc") {
    st.textContent = ":where(html){background:#e9e7e0}:where(article.doc){box-sizing:border-box;max-width:794px;margin:28px auto;padding:64px 72px;background:#fff;color:#141413;box-shadow:0 1px 3px #0003,0 12px 40px -18px #0004;min-height:1000px;line-height:1.6}:where(body){margin:0;background:transparent}";
    post({ t: "info", n: 1, i: 0, titles: [], doc: true });
  } else if (VIEW === "print") {
    var deck = slides.length > 0;
    st.textContent = deck ? "@page{size:1280px 720px;margin:0}html,body{margin:0}section.slide{page-break-after:always;break-after:page;overflow:hidden;position:relative}section.slide aside.notes{display:none!important}"
      : "@page{size:A4;margin:14mm}:where(article.doc){max-width:none}";
    setTimeout(function () { try { print(); } catch (e) {} }, 400);
  }

  /* ---------- extraction for export ---------- */
  var hex = function (c) {
    var m = /rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/.exec(c || ""); if (!m || (m[4] !== undefined && +m[4] === 0)) return null;
    return [m[1], m[2], m[3]].map(function (x) { return ("0" + (+x).toString(16)).slice(-2); }).join("");
  };
  var pt = function (px) { return Math.round(parseFloat(px) * 0.75 * 2) / 2; };
  var fam = function (cs) { return (cs.fontFamily.split(",")[0] || "").replace(/["']/g, "").trim(); };
  function runs(el) {
    var out = [];
    (function walk(n, f) {
      n.childNodes.forEach(function (c) {
        if (c.nodeType === 3) { var t = c.textContent.replace(/\s+/g, " "); if (t) out.push({ text: t, bold: f.b, italic: f.i, code: f.c, href: f.h, underline: f.u }); }
        else if (c.nodeType === 1) {
          var tag = c.tagName.toLowerCase(); if (tag === "br") { out.push({ text: "\n", br: true }); return; }
          if (["ul", "ol", "script", "style", "aside"].indexOf(tag) >= 0) return;
          walk(c, { b: f.b || /^(b|strong|h[1-6])$/.test(tag), i: f.i || /^(i|em)$/.test(tag), c: f.c || tag === "code", h: tag === "a" ? c.getAttribute("href") : f.h, u: f.u || tag === "u" });
        }
      });
    })(el, {});
    if (out.length) { out[0].text = out[0].text.replace(/^\s+/, ""); out[out.length - 1].text = out[out.length - 1].text.replace(/\s+$/, ""); }
    return out.filter(function (r) { return r.text; });
  }
  var plain = function (el) { return runs(el).map(function (r) { return r.text; }).join(""); };
  function blocksOf(root, rel, fast) {
    var out = [], base = rel ? root.getBoundingClientRect() : null;
    var sc = base ? base.width / W : 1;
    function box(el) { if (!base) return null; var r = el.getBoundingClientRect(); return { x: (r.left - base.left) / sc, y: (r.top - base.top) / sc, w: r.width / sc, h: r.height / sc }; }
    function visit(el) {
      [].forEach.call(el.children, function (c) {
        var tag = c.tagName.toLowerCase(), cs = getComputedStyle(c); if (cs.display === "none" || tag === "aside" || tag === "script" || tag === "style") return;
        var b = box(c), look = { color: hex(cs.color), font: fam(cs), size: pt(cs.fontSize), bold: +cs.fontWeight >= 600, italic: cs.fontStyle === "italic", align: cs.textAlign };
        if (/^h[1-6]$/.test(tag)) out.push({ t: "h", level: +tag[1], runs: runs(c), text: plain(c), box: b, look: look });
        else if (tag === "p" || tag === "figcaption") { if (plain(c)) out.push({ t: "p", runs: runs(c), text: plain(c), box: b, look: look }); }
        else if (tag === "ul" || tag === "ol") {
          var items = []; (function li(l, depth) { [].forEach.call(l.children, function (x) { if (x.tagName.toLowerCase() !== "li") return; items.push({ runs: runs(x), text: plain(x), depth: depth });
            [].forEach.call(x.children, function (y) { if (/^[uo]l$/i.test(y.tagName)) li(y, depth + 1); }); }); })(c, 0);
          out.push({ t: "list", ordered: tag === "ol", items: items, box: b, look: look });
        } else if (tag === "img") out.push({ t: "img", src: c.currentSrc || c.src, alt: c.alt || "", box: b, nw: c.naturalWidth, nh: c.naturalHeight });
        else if (tag === "table") {
          var rows = [].map.call(c.rows, function (r) { return [].map.call(r.cells, function (d) { return { text: plain(d), head: d.tagName === "TH", bg: hex(getComputedStyle(d).backgroundColor), color: hex(getComputedStyle(d).color) }; }); });
          out.push({ t: "table", rows: rows, box: b, look: look });
        } else if (tag === "pre") out.push({ t: "code", text: c.textContent.replace(/\n$/, ""), box: b, look: look });
        else if (tag === "blockquote") out.push({ t: "quote", runs: runs(c), text: plain(c), box: b, look: look });
        else if (tag === "hr") out.push({ t: "hr", box: b });
        else if (tag === "svg") out.push({ t: "svg", svg: new XMLSerializer().serializeToString(c), box: b });
        else visit(c);
      });
    }
    visit(root);
    return out;
  }
  function extract() {
    if (slides.length) {
      var wasGrid = grid, was = cur; grid = false;
      var data = slides.map(function (s, i) {
        cur = i; layout(); var cs = getComputedStyle(s);
        var bg = hex(cs.backgroundColor) || hex(getComputedStyle(document.body).backgroundColor) || "FFFFFF";
        var n = s.querySelector("aside.notes");
        return { bg: bg, fg: hex(cs.color), title: title(s), blocks: blocksOf(s, true), notes: n ? n.textContent.trim() : "" };
      });
      grid = wasGrid; cur = was; layout();
      return { kind: "slides", title: document.title, slides: data, theme: { head: fam(getComputedStyle(document.querySelector("h1,h2") || document.body)), body: fam(getComputedStyle(document.body)) } };
    }
    var art = document.querySelector("article.doc") || document.body;
    return { kind: "doc", title: document.title || (art.querySelector("h1") || {}).textContent || "Document", blocks: blocksOf(art, false) };
  }
  addEventListener("message", function (e) {
    var m = e.data; if (!m || !m.aihub) return;
    if (m.t === "go") go(m.i); else if (m.t === "next") go(cur + 1); else if (m.t === "prev") go(cur - 1);
    else if (m.t === "grid") { grid = !!m.on; layout(); info(); }
    else if (m.t === "extract") { try { post({ t: "extract", id: m.id, data: extract() }); } catch (err) { post({ t: "extract", id: m.id, error: String(err) }); } }
    else if (m.t === "print") { try { print(); } catch (err) {} }
  });
})();
