// OpenUI island: React + @openuidev components, loaded only when a chat produces an OpenUI block (the shell stays Preact).
// Everything comes from jsDelivr at pinned versions; if it cannot load, the caller shows the source instead.
const CDN = "https://cdn.jsdelivr.net/npm/";
const V = { react: "18.3.1", dom: "18.3.1", lang: "1.0.2", ui: "0.18.1" };
let pending;

export function load() {
  pending ||= (async () => {
    const [React, client, lang, lib] = await Promise.all([
      import(CDN + `react@${V.react}/+esm`),
      import(CDN + `react-dom@${V.dom}/client/+esm`),
      import(CDN + `@openuidev/react-lang@${V.lang}/+esm`),
      import(CDN + `@openuidev/react-ui@${V.ui}/genui-lib/+esm`),
    ]);
    for (const css of ["dist/styles/openui-defaults.css", "dist/components/index.css"]) {
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = CDN + `@openuidev/react-ui@${V.ui}/${css}`;
      document.head.appendChild(l);
    }
    return { React: React.default || React, client, lang, lib };
  })();
  pending.catch(() => (pending = null));
  return pending;
}

/** The OpenUI Lang rules, as text, for the system prompt of a new chat. '' when the library cannot load. */
export async function languagePrompt() {
  try {
    const { lang, lib } = await load();
    const L = lib.openuiLibrary, o = lib.openuiPromptOptions;
    return "# OpenUI Lang (canvas components)\n" + (typeof L.prompt === "function" ? L.prompt(o) : lang.generatePrompt(L, o));
  } catch (e) {
    return "";
  }
}

/** Render OpenUI Lang `source` into `el`. Returns a function that unmounts. */
export async function mount(el, source, streaming = false) {
  const { React, client, lang, lib } = await load();
  const root = client.createRoot(el);
  root.render(React.createElement(lang.Renderer, { response: source, library: lib.openuiLibrary, isStreaming: streaming }));
  return () => root.unmount();
}
