"""Maintainer tool: put the lazily loaded browser libraries into static/vendor/lazy/ so the UI needs no CDN (map tiles still come from OSM).

    python ui/tools/vendor_libs.py [build-dir]

Needs node + npm on THIS machine only (users never run it). Installs the pinned versions with --ignore-scripts into a scratch
folder, bundles the ES-module ones into one file each with esbuild (one React for OpenUI), copies the single-file builds and
writes VERSIONS.json (version, size, sha256 per file). Bump a version here, run it, commit the result."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "static", "vendor", "lazy")
PKGS = {"react": "18.3.1", "react-dom": "18.3.1", "@openuidev/react-lang": "1.0.2", "@openuidev/react-ui": "0.18.1",
        "@xterm/xterm": "5.5.0", "@xterm/addon-fit": "0.10.0", "@viz-js/viz": "3.11.0", "mermaid": "11.4.1",
        "echarts": "5.5.1", "leaflet": "1.9.4", "jszip": "3.10.1", "pptxgenjs": "3.12.0", "docx": "8.5.0", "esbuild": "0.24.0"}
BUNDLES = {                                                     # out file -> ES module entry (bundled, minified)
    "openui.mjs": 'export * as React from "react"; export * as client from "react-dom/client"; export * as lang from "@openuidev/react-lang"; export * as lib from "@openuidev/react-ui/genui-lib";',
    "xterm.mjs": 'import * as X from "@xterm/xterm"; export const Terminal = X.Terminal || X.default.Terminal;',      # UMD builds: name the exports
    "addon-fit.mjs": 'import * as F from "@xterm/addon-fit"; export const FitAddon = F.FitAddon || F.default.FitAddon;',
    "viz.mjs": 'export * from "@viz-js/viz";',
    "mermaid.mjs": 'export { default } from "mermaid";',
}
COPIES = {                                                      # out path -> node_modules path
    "echarts.min.js": "echarts/dist/echarts.min.js",
    "leaflet/leaflet.js": "leaflet/dist/leaflet.js", "leaflet/leaflet.css": "leaflet/dist/leaflet.css", "leaflet/images": "leaflet/dist/images",
    "jszip.min.js": "jszip/dist/jszip.min.js",
    "pptxgen.bundle.js": "pptxgenjs/dist/pptxgen.bundle.js",
    "docx.umd.js": "docx/build/index.umd.js",
    "xterm.css": "@xterm/xterm/css/xterm.css",
    "openui/openui-defaults.css": "@openuidev/react-ui/dist/styles/openui-defaults.css",
    "openui/components.css": "@openuidev/react-ui/dist/components/index.css",
}
SOURCE = {"openui.mjs": ["react", "react-dom", "@openuidev/react-lang", "@openuidev/react-ui"], "xterm.mjs": ["@xterm/xterm"],
          "addon-fit.mjs": ["@xterm/addon-fit"], "viz.mjs": ["@viz-js/viz"], "mermaid.mjs": ["mermaid"]}


def sh(cmd, cwd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    build = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("TMPDIR", "/tmp"), "aihub-vendor-build"))
    os.makedirs(build, exist_ok=True)
    with open(os.path.join(build, "package.json"), "w") as f:
        json.dump({"name": "aihub-ui-vendor", "private": True, "dependencies": PKGS}, f, indent=1)
    sh(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"], build)
    nm = os.path.join(build, "node_modules")
    esb = os.path.join(nm, ".bin", "esbuild")
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    for out, src in BUNDLES.items():
        entry = os.path.join(build, "entry-" + out.replace(".mjs", ".js"))
        with open(entry, "w") as f:
            f.write(src + "\n")
        sh([esb, entry, "--bundle", "--format=esm", "--minify", "--target=es2020", "--legal-comments=none",
            '--define:process.env.NODE_ENV="production"', "--loader:.css=empty", "--outfile=" + os.path.join(OUT, out)], build)
    for out, src in COPIES.items():
        s, d = os.path.join(nm, src), os.path.join(OUT, out)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        (shutil.copytree if os.path.isdir(s) else shutil.copyfile)(s, d)
    files = {}
    for root, _, names in os.walk(OUT):
        for n in sorted(names):
            p = os.path.join(root, n)
            rel = os.path.relpath(p, OUT).replace(os.sep, "/")
            top = rel.split("/")[0]
            pk = SOURCE.get(rel) or [next((k for k, v in COPIES.items() if rel == k or rel.startswith(k + "/")), top)]
            pk = [COPIES[pk[0]].split("/dist")[0].split("/build")[0].split("/css")[0]] if pk[0] in COPIES else pk
            files[rel] = {"from": {k: PKGS.get(k, "") for k in pk}, "size": os.path.getsize(p), "sha256": sha(p)}
    with open(os.path.join(OUT, "VERSIONS.json"), "w") as f:
        json.dump({"tool": "ui/tools/vendor_libs.py", "files": files}, f, indent=1, sort_keys=True)
    print("total %.1f MB in %s" % (sum(v["size"] for v in files.values()) / 1e6, os.path.normpath(OUT)))


if __name__ == "__main__":
    main()
