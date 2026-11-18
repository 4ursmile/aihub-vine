// SQL code intelligence for Data Studio and notebook SQL cells: keywords, functions, the tables of the connection, and the columns of the tables the query
// uses (aliases resolved: `from orders o ... o.<columns>`). Schema comes from /api/sql/tables and /api/sql/columns, cached per connection.
// Same result shape as the kernel's completion, so the shared CodeBox shows it: { matches: [{ t, k }], start, end }.

const KEYWORDS = ("select from where group by order having limit offset fetch join left right inner outer full cross natural on using as and or not in is null like ilike between exists case when then else end " +
  "distinct all union intersect except insert into values update set delete create alter drop table view index with recursive over partition asc desc nulls first last true false cast interval " +
  "primary key foreign references default unique check explain analyze begin commit rollback truncate").split(" ");
const FUNCTIONS = ("count sum avg min max coalesce nullif cast concat length lower upper trim substr substring replace round floor ceil abs mod power sqrt now current_date current_timestamp date_trunc date_part extract " +
  "to_char to_date to_timestamp strftime datediff dateadd row_number rank dense_rank lag lead first_value last_value array_agg string_agg group_concat json_extract").split(" ");
export const SQL_WORDS = () => KEYWORDS;
const SIG = {
  count: ["count(expr | *)", "Number of rows (or non-null values of expr)."], sum: ["sum(expr)", "Sum of the values."], avg: ["avg(expr)", "Average of the values."], min: ["min(expr)", "Smallest value."], max: ["max(expr)", "Largest value."],
  coalesce: ["coalesce(value, value, ...)", "First argument that is not null."], nullif: ["nullif(a, b)", "Null when a equals b, otherwise a."], cast: ["cast(expr AS type)", "Convert to another type."],
  concat: ["concat(a, b, ...)", "Join strings."], length: ["length(text)", "Number of characters."], lower: ["lower(text)", "Lower case."], upper: ["upper(text)", "Upper case."], trim: ["trim(text)", "Remove surrounding spaces."],
  substr: ["substr(text, start, length)", "Part of a string; start counts from 1."], substring: ["substring(text, start, length)", "Part of a string; start counts from 1."], replace: ["replace(text, from, to)", "Replace every occurrence."],
  round: ["round(number, digits)", "Round to digits decimals."], floor: ["floor(number)", "Round down."], ceil: ["ceil(number)", "Round up."], abs: ["abs(number)", "Absolute value."], mod: ["mod(a, b)", "Remainder of a / b."], power: ["power(base, exponent)", "base to the power exponent."], sqrt: ["sqrt(number)", "Square root."],
  date_trunc: ["date_trunc(unit, timestamp)", "Cut a timestamp to 'day', 'month', 'year' ..."], date_part: ["date_part(unit, timestamp)", "A part of a date: 'year', 'month' ..."], extract: ["extract(unit FROM timestamp)", "A part of a date."],
  to_char: ["to_char(value, format)", "Format a number or date as text."], to_date: ["to_date(text, format)", "Parse text into a date."], to_timestamp: ["to_timestamp(text, format)", "Parse text into a timestamp."], strftime: ["strftime(format, time)", "SQLite: format a date/time."],
  datediff: ["datediff(unit, start, end)", "Difference between two dates."], dateadd: ["dateadd(unit, amount, date)", "Add an interval to a date."], row_number: ["row_number() OVER (PARTITION BY ... ORDER BY ...)", "Position of the row in its group."],
  rank: ["rank() OVER (PARTITION BY ... ORDER BY ...)", "Rank with gaps for ties."], dense_rank: ["dense_rank() OVER (PARTITION BY ... ORDER BY ...)", "Rank without gaps."], lag: ["lag(expr, offset, default) OVER (...)", "Value from a previous row."],
  lead: ["lead(expr, offset, default) OVER (...)", "Value from a following row."], array_agg: ["array_agg(expr)", "Collect values into an array."], string_agg: ["string_agg(expr, separator)", "Join values into one string."], group_concat: ["group_concat(expr, separator)", "Join values into one string."],
  json_extract: ["json_extract(json, path)", "Value at a JSON path."], now: ["now()", "Current timestamp."], current_date: ["current_date", "Today's date."], current_timestamp: ["current_timestamp", "Current timestamp."], first_value: ["first_value(expr) OVER (...)", "First value in the window."], last_value: ["last_value(expr) OVER (...)", "Last value in the window."],
};
// signature help for the function whose "(" follows NAMEEND: { sig, doc } or null
export async function sqlSignature(nameEnd, pos) {                  // NAMEEND = the text up to the "(" of the call
  const name = (/([A-Za-z_]\w*)\s*$/.exec(nameEnd.slice(0, pos)) || [])[1], hit = name && SIG[name.toLowerCase()];
  return hit ? { sig: hit[0], doc: hit[1] } : null;
}

const tablesCache = new Map(), colsCache = new Map(), colsWait = new Map();
const NOT_ALIAS = new Set(["on", "where", "left", "right", "inner", "outer", "full", "cross", "join", "group", "order", "having", "limit", "union", "natural", "using", "offset", "set", "values", "select", "fetch", "except", "intersect", "window", "when"]);
const NONE = { catalog: "", catalogs: [], schema: "", schemas: [], tables: [] };

// tables of one place: CATALOG / SCHEMA undefined = the connection's default. -> { catalog, catalogs, schema, schemas, tables }
function tablesOf(post, conn, catalog, schema) {
  const k = conn + "\u0001" + (catalog == null ? "\u0002" : catalog) + "\u0001" + (schema == null ? "\u0002" : schema);
  if (!tablesCache.has(k)) {
    const p = post("sql/tables", { id: conn, catalog: catalog || undefined, schema: schema == null ? undefined : schema })
      .then((r) => (r.ok ? { catalog: r.catalog || "", catalogs: r.catalogs || [], schema: r.schema || "", schemas: r.schemas || [], tables: (r.tables || []).map((t) => t.name) } : NONE))
      .catch(() => { tablesCache.delete(k); return NONE; });
    tablesCache.set(k, p);
  }
  return tablesCache.get(k);
}
const getTables = (post, conn) => tablesOf(post, conn);
function getColumns(post, conn, catalog, schema, table) {
  const k = conn + "\u0001" + (catalog || "") + "\u0001" + (schema || "") + "\u0001" + table.toLowerCase();
  if (colsCache.has(k)) return Promise.resolve(colsCache.get(k));
  if (colsWait.has(k)) return colsWait.get(k);
  const p = post("sql/columns", { id: conn, catalog: catalog || undefined, schema, table }).then((r) => { const c = r.ok ? (r.columns || []).map((x) => ({ name: x.name, type: x.type || "" })) : []; colsCache.set(k, c); return c; }).catch(() => []).finally(() => colsWait.delete(k));
  colsWait.set(k, p);
  return p;
}
export const forgetSchema = (conn) => { for (const m of [tablesCache, colsCache]) for (const k of [...m.keys()]) if (k.startsWith(conn + "\u0001")) m.delete(k); };

const unq = (x) => x.replace(/^[`"\[]|[`"\]]$/g, "");
const same = (list, name) => list.find((x) => x.toLowerCase() === name.toLowerCase());
const IDENT = String.raw`(?:[A-Za-z_][\w$]*|` + "`[^`]+`" + String.raw`|"[^"]+"|\[[^\]]+\])`;

// a dotted name -> { catalog, schema, table } (1 part: default place, 2: schema.table, 3: catalog.schema.table)
function place(parts, sch) {
  if (parts.length >= 3) return { catalog: parts[parts.length - 3], schema: parts[parts.length - 2], table: parts[parts.length - 1] };
  if (parts.length === 2) return { catalog: sch.catalog, schema: parts[0], table: parts[1] };
  return { catalog: sch.catalog, schema: sch.schema, table: parts[0] };
}

// tables the query reads and the names they go by: [{ catalog, schema, table, alias }]
function usedTables(text, sch) {
  const out = [], re = new RegExp(String.raw`\b(?:from|join|update|into)\s+(${IDENT}(?:\s*\.\s*${IDENT})*)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?`, "gi");
  let m;
  while ((m = re.exec(text))) {
    const parts = m[1].split(/\s*\.\s*/).map(unq), p = place(parts, sch);
    if (parts.length === 1) { const t = same(sch.tables, p.table); if (!t) continue; p.table = t; }
    const a = m[2] && !NOT_ALIAS.has(m[2].toLowerCase()) ? m[2] : "";
    out.push({ ...p, alias: a });
  }
  return out;
}

// what comes after `a.b.` : columns of a table / alias, tables of a schema, or schemas of a catalog
async function afterDot(post, conn, sch, parts, used) {
  const cols = async (c, s, t) => (await getColumns(post, conn, c, s, t)).map((x) => ({ t: x.name, k: "property", d: x.type }));
  const schemasOf = async (c) => (await tablesOf(post, conn, c)).schemas.map((x) => ({ t: x, k: "module", d: "schema" }));
  const tablesIn = async (c, s) => (await tablesOf(post, conn, c, s)).tables.map((x) => ({ t: x, k: "class", d: (c ? c + "." : "") + s }));
  if (parts.length === 1) {
    const q = parts[0].toLowerCase(), u = used.find((x) => x.alias.toLowerCase() === q) || used.find((x) => !x.alias && x.table.toLowerCase() === q);
    if (u) return cols(u.catalog, u.schema, u.table);
    const t = same(sch.tables, parts[0]);
    if (t) return cols(sch.catalog, sch.schema, t);
    const c = same(sch.catalogs, parts[0]);
    if (c) return schemasOf(c);
    const s = same(sch.schemas, parts[0]);
    if (s) return tablesIn(sch.catalog, s);
    return [];
  }
  if (parts.length === 2) {
    const c = same(sch.catalogs, parts[0]);
    if (c) { const s = same((await tablesOf(post, conn, c)).schemas, parts[1]); if (s) return tablesIn(c, s); }
    return cols(sch.catalog, parts[0], parts[1]);                       // schema.table.
  }
  return cols(parts[parts.length - 3], parts[parts.length - 2], parts[parts.length - 1]);
}

const rank = (name, w) => { const n = name.toLowerCase(); return n.startsWith(w) ? 0 : n.includes("_" + w) ? 1 : n.includes(w) ? 2 : 9; };
const pick = (list, w, max) => (w ? list.map((x) => [rank(x.t, w), x]).filter(([r]) => r < 9).sort((a, b) => a[0] - b[0]).map(([, x]) => x) : list).slice(0, max);

// -> async (code, pos) => completion for this connection
export function sqlComplete(post, conn) {
  return async (code, pos) => {
    const before = code.slice(0, pos), m = /([A-Za-z_][\w$]*)?$/.exec(before), word = m[1] || "", start = pos - word.length, w = word.toLowerCase();
    const upper = word.length > 1 && word === word.toUpperCase(), cs = (t) => (upper ? t.toUpperCase() : t);
    const kw = (list, k) => list.map((t) => ({ t: cs(t), k, d: k === "function" && SIG[t] ? SIG[t][0] : "" }));
    const empty = { matches: [], start, end: pos };
    if (/--[^\n]*$/.test(before) || (before.split("'").length - 1) % 2 === 1) return empty;       // inside a comment or a string
    if (!conn) return { matches: pick(kw(KEYWORDS, "keyword").concat(kw(FUNCTIONS, "function")), w, 30), start, end: pos };
    const sch = await getTables(post, conn), used = usedTables(code, sch);
    // `a.` / `a.b.` / `a.b.c.` : schemas of a catalog, tables of a schema, columns of a table or alias
    const path = new RegExp(String.raw`((?:${IDENT}\s*\.\s*)+)$`).exec(before.slice(0, start));
    if (path) {
      const parts = path[1].split(/\s*\.\s*/).filter(Boolean).map(unq);
      return { matches: pick(await afterDot(post, conn, sch, parts, used), w, 80), start, end: pos };
    }
    const prev = (/([A-Za-z_]+|[,(=<>+\-*/])\s*$/.exec(before.slice(0, start)) || [])[1] || "";
    if (/^(from|join|into|update|table|truncate)$/i.test(prev)) {              // a table is expected here
      const places = sch.tables.map((t) => ({ t, k: "class", d: sch.schema ? (sch.catalog ? sch.catalog + "." : "") + sch.schema : "" }))
        .concat(sch.schemas.length > 1 ? sch.schemas.map((s) => ({ t: s, k: "module", d: "schema" })) : [], sch.catalogs.map((c) => ({ t: c, k: "module", d: "catalog" })));
      return { matches: pick(places, w, 60), start, end: pos };
    }
    let cols = [];
    const tabs = [...new Map(used.map((u) => [[u.catalog, u.schema, u.table].join("."), u])).values()];
    for (const u of tabs.slice(0, 6)) for (const c of await getColumns(post, conn, u.catalog, u.schema, u.table)) cols.push({ t: c.name, k: "property", d: c.type });
    const seen = new Set(); cols = cols.filter((c) => !seen.has(c.t) && seen.add(c.t));
    if (!w && !cols.length) return empty;
    const tables = sch.tables.map((t) => ({ t, k: "class" })).concat(sch.catalogs.map((c) => ({ t: c, k: "module", d: "catalog" })), sch.schemas.length > 1 ? sch.schemas.map((x) => ({ t: x, k: "module", d: "schema" })) : []);
    // after SELECT/WHERE/AND/ON ... columns first; elsewhere keywords first
    const colFirst = /^(select|where|and|or|on|by|set|having|when|then|else|,|\(|=|<|>|\+|-|\*|\/)$/i.test(prev);
    const lists = colFirst ? [cols, kw(FUNCTIONS, "function"), kw(KEYWORDS, "keyword"), tables] : [kw(KEYWORDS, "keyword"), cols, tables, kw(FUNCTIONS, "function")];
    const out = []; for (const l of lists) for (const x of pick(l, w, 40)) if (x.t.toLowerCase() !== w || out.length === 0) out.push(x);
    return { matches: out.slice(0, 50), start, end: pos };
  };
}
