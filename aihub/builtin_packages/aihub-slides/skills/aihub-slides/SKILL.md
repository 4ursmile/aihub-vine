---
name: aihub-slides
description: Use when the user asks for a slide deck, presentation, slideshow, pitch, report or document that should be presented or exported (PowerPoint, Word, PDF). Writes one HTML file in the format AI Hub UI presents and exports.
---

# Slides and documents as HTML

Write ONE self-contained `.html` file in the working folder. AI Hub UI shows it as a slideshow or a paged document and exports it (decks: .pptx, PDF, HTML; documents: .docx, PDF, Markdown, HTML). No scripts, no network fonts or images: use system fonts, inline SVG and local files (`assets/x.png`).

## Slide deck

```html
<!doctype html><html><head><meta charset="utf-8">
<meta name="aihub-kind" content="slides">
<title>Deck title</title>
<style>
  :root{--bg:#faf9f5;--fg:#141413;--muted:#73726c;--accent:#d97757;--accent2:#c6613f;--surface:#f0eee6;
        --font-head:Georgia,serif;--font-body:system-ui,sans-serif}
  .slide{width:1280px;height:720px;box-sizing:border-box;padding:72px 88px;background:var(--bg);color:var(--fg);font:28px/1.4 var(--font-body);position:relative}
  .slide h1,.slide h2{font-family:var(--font-head);margin:0 0 24px}
  .slide h1{font-size:64px}.slide h2{font-size:44px;color:var(--accent2)}
</style></head><body>
<section class="slide"><h1>Title</h1><p>Subtitle</p><aside class="notes">Speaker notes.</aside></section>
<section class="slide"><h2>One idea</h2><ul><li>Point</li><li>Point</li></ul></section>
</body></html>
```

Rules:
- Every slide is `<section class="slide">`, exactly 1280x720 CSS pixels, content inside the padding; never let text overflow the slide.
- One idea per slide: a title (`h1` on the first, `h2` after), then at most 5 short bullets, one image, one table or one chart.
- Colors and fonts come ONLY from the CSS variables on `:root` (`--bg --fg --muted --accent --accent2 --surface --font-head --font-body`), so a theme can replace them.
- Use `h1/h2/h3`, `p`, `ul/ol/li`, `img`, `table`, `pre`, `blockquote` for content: these export to editable PowerPoint objects. Decorative shapes may be inline SVG (they export as pictures only if given as `<img src="x.svg">`).
- Speaker notes go in `<aside class="notes">` inside the slide (hidden when presenting, exported as PowerPoint notes).
- Place things with normal flow or `position:absolute` inside the slide; the exporter reads the real position of every block.

## Document

`<meta name="aihub-kind" content="doc">` and one `<article class="doc">` with `h1`..`h3`, `p`, `ul/ol`, `table`, `img`, `blockquote`, `pre`; inline `strong`, `em`, `code`, `a`. Page width about 794px (A4). Use the same CSS variables.

## Themes

If the user's message contains a block starting `[Theme "Name"` use exactly those variable values (and fonts). With no theme, pick a calm palette with one accent and good contrast (text 4.5:1 or better).

## After writing

Tell the user the file name; the canvas opens it as a slideshow (arrow keys, grid view, full screen) with an Export button. Do not paste the HTML into the reply.
