// Export of a presented deck / document: PowerPoint (PptxGenJS), Word (docx), Markdown, PDF (browser print). Libraries load on demand from static/vendor/lazy (vendored, no CDN).
const libs = {};
const LAZY = new URL("./vendor/lazy/", import.meta.url).href;
const lib = (file, ok) => libs[file] || (libs[file] = new Promise((res, rej) => {
  if (ok()) return res();
  const s = document.createElement("script"); s.src = LAZY + file; s.onload = res; s.onerror = () => { s.remove(); delete libs[file]; rej(new Error("Could not load " + file)); }; document.head.appendChild(s);
}));
export const loadJSZip = () => lib("jszip.min.js", () => window.JSZip).then(() => window.JSZip);

export const safeName = (t) => (String(t || "export").replace(/[^\w.\- ]+/g, "").trim().replace(/\s+/g, "-").slice(0, 60) || "export");
function save(blob, name) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000); }

async function dataUrl(src) {
  if (!src) return null;
  if (src.startsWith("data:")) return src;
  try { const r = await fetch(src); if (!r.ok) return null; const b = await r.blob(); return await new Promise((ok) => { const f = new FileReader(); f.onload = () => ok(f.result); f.readAsDataURL(b); }); } catch (e) { return null; }
}
const svgUrl = (svg) => "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));

// ---------- PowerPoint
export async function toPptx(d, name) {
  await lib("pptxgen.bundle.js", () => window.PptxGenJS);
  const p = new window.PptxGenJS(); p.layout = "LAYOUT_WIDE"; p.title = d.title || name;
  const k = 13.333 / 1280, inch = (v) => Math.max(0, Math.round(v * k * 1000) / 1000);
  const runsOf = (b, fg) => (b.runs && b.runs.length ? b.runs : [{ text: b.text }]).map((r) => ({ text: r.text, options: { bold: r.bold || b.look?.bold, italic: r.italic || b.look?.italic, color: b.look?.color || fg, underline: r.underline ? { style: "sng" } : undefined,
    fontFace: r.code ? "Courier New" : b.look?.font || undefined, hyperlink: r.href ? { url: r.href } : undefined, breakLine: !!r.br } }));
  for (const s of d.slides) {
    const sl = p.addSlide(); sl.background = { color: s.bg || "FFFFFF" }; const fg = s.fg || "222222";
    for (const b of s.blocks) {
      const bx = b.box || { x: 80, y: 80, w: 1100, h: 100 }, pos = { x: inch(bx.x), y: inch(bx.y), w: inch(bx.w), h: inch(bx.h) };
      const base = { ...pos, margin: 0, valign: "top", fontSize: Math.max(8, Math.min(96, b.look?.size || 18)), fontFace: b.look?.font || undefined, color: b.look?.color || fg, align: ["center", "right", "justify"].includes(b.look?.align) ? b.look.align : "left" };
      if (b.t === "h" || b.t === "p" || b.t === "quote") sl.addText(runsOf(b, fg), { ...base, bold: b.t === "h" || b.look?.bold, italic: b.t === "quote" || b.look?.italic, fit: "shrink" });
      else if (b.t === "list") sl.addText(b.items.map((it) => ({ text: it.text, options: { bullet: b.ordered ? { type: "number" } : true, indentLevel: it.depth, breakLine: true } })), { ...base, fit: "shrink" });
      else if (b.t === "code") sl.addText(b.text, { ...base, fontFace: "Courier New", fill: { color: "F2F2F2" }, color: "222222" });
      else if (b.t === "table") sl.addTable(b.rows.map((r) => r.map((c) => ({ text: c.text, options: { bold: c.head, color: c.color || fg, fill: c.bg ? { color: c.bg } : undefined } }))),
        { x: pos.x, y: pos.y, w: pos.w, fontSize: Math.max(9, b.look?.size || 14), fontFace: b.look?.font || undefined, border: { type: "solid", color: "BBBBBB", pt: 0.5 } });
      else if (b.t === "img") { const u = await dataUrl(b.src); if (u) sl.addImage({ data: u, ...pos }); }
      else if (b.t === "svg") sl.addImage({ data: svgUrl(b.svg), ...pos });
    }
    if (s.notes) sl.addNotes(s.notes);
  }
  await p.writeFile({ fileName: safeName(name) + ".pptx" });
}

// ---------- Word
export async function toDocx(d, name) {
  await lib("docx.umd.js", () => window.docx);
  const X = window.docx, H = [null, X.HeadingLevel.HEADING_1, X.HeadingLevel.HEADING_2, X.HeadingLevel.HEADING_3, X.HeadingLevel.HEADING_4, X.HeadingLevel.HEADING_5, X.HeadingLevel.HEADING_6];
  const tr = (r) => { const o = { text: r.text, bold: r.bold || undefined, italics: r.italic || undefined, font: r.code ? "Consolas" : undefined };
    return r.href ? new X.ExternalHyperlink({ link: r.href, children: [new X.TextRun({ ...o, style: "Hyperlink" })] }) : new X.TextRun(o); };
  const kids = (b) => (b.runs && b.runs.length ? b.runs : [{ text: b.text || "" }]).map(tr);
  const out = [];
  for (const b of d.blocks) {
    if (b.t === "h") out.push(new X.Paragraph({ heading: H[b.level] || H[3], children: kids(b) }));
    else if (b.t === "p") out.push(new X.Paragraph({ children: kids(b), spacing: { after: 120 } }));
    else if (b.t === "quote") out.push(new X.Paragraph({ children: (b.runs && b.runs.length ? b.runs : [{ text: b.text }]).map((r) => tr({ ...r, italic: true })), indent: { left: 540 }, spacing: { after: 120 } }));
    else if (b.t === "code") b.text.split("\n").forEach((ln) => out.push(new X.Paragraph({ children: [new X.TextRun({ text: ln || " ", font: "Consolas", size: 20 })], shading: { type: X.ShadingType.CLEAR, fill: "F2F2F2" } })));
    else if (b.t === "list") b.items.forEach((it) => out.push(new X.Paragraph({ children: (it.runs && it.runs.length ? it.runs : [{ text: it.text }]).map(tr), ...(b.ordered ? { numbering: { reference: "num", level: Math.min(it.depth, 3) } } : { bullet: { level: Math.min(it.depth, 3) } }) })));
    else if (b.t === "table") out.push(new X.Table({ width: { size: 100, type: X.WidthType.PERCENTAGE }, rows: b.rows.map((r) => new X.TableRow({ children: r.map((c) => new X.TableCell({ shading: c.head ? { type: X.ShadingType.CLEAR, fill: "EEEEEE" } : undefined, children: [new X.Paragraph({ children: [new X.TextRun({ text: c.text, bold: c.head })] })] })) })) }));
    else if (b.t === "img") {
      const u = await dataUrl(b.src); if (!u || u.startsWith("data:image/svg")) continue;
      const w = Math.min(b.nw || 500, 560), h = Math.round(w * ((b.nh || 300) / (b.nw || 500))), type = /png/.test(u.slice(0, 20)) ? "png" : /gif/.test(u.slice(0, 20)) ? "gif" : "jpg";
      out.push(new X.Paragraph({ children: [new X.ImageRun({ data: Uint8Array.from(atob(u.split(",")[1]), (c) => c.charCodeAt(0)), transformation: { width: w, height: h }, type })] }));
    } else if (b.t === "hr") out.push(new X.Paragraph({ border: { bottom: { style: X.BorderStyle.SINGLE, size: 6, color: "BBBBBB", space: 1 } } }));
  }
  const levels = [0, 1, 2, 3].map((l) => ({ level: l, format: "decimal", text: "%" + (l + 1) + ".", alignment: X.AlignmentType.LEFT, style: { paragraph: { indent: { left: 720 * (l + 1), hanging: 360 } } } }));
  const doc = new X.Document({ title: d.title || name, numbering: { config: [{ reference: "num", levels }] }, sections: [{ children: out }] });
  save(await X.Packer.toBlob(doc), safeName(name) + ".docx");
}

// ---------- Markdown
export function toMarkdown(d, name) {
  const inl = (b) => (b.runs && b.runs.length ? b.runs : [{ text: b.text || "" }]).map((r) => { let t = r.text; if (r.code) t = "`" + t + "`"; if (r.bold) t = "**" + t.trim() + "** "; if (r.italic) t = "*" + t.trim() + "* "; if (r.href) t = "[" + t.trim() + "](" + r.href + ")"; return t; }).join("").replace(/\s+/g, " ").trim();
  const md = d.blocks.map((b) => b.t === "h" ? "#".repeat(b.level) + " " + inl(b) : b.t === "p" ? inl(b) : b.t === "quote" ? "> " + inl(b)
    : b.t === "list" ? b.items.map((it, i) => "  ".repeat(it.depth) + (b.ordered ? i + 1 + ". " : "- ") + it.text).join("\n") : b.t === "code" ? "```\n" + b.text + "\n```"
    : b.t === "table" ? b.rows.map((r, i) => "| " + r.map((c) => c.text.replace(/\|/g, "\\|")).join(" | ") + " |" + (i === 0 ? "\n|" + r.map(() => " --- |").join("") : "")).join("\n")
    : b.t === "img" ? "![" + (b.alt || "") + "](" + (b.src || "").split("/").slice(-2).join("/") + ")" : b.t === "hr" ? "---" : "").filter(Boolean).join("\n\n");
  save(new Blob([md + "\n"], { type: "text/markdown" }), safeName(name) + ".md");
}
