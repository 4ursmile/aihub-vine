"""The sample SQLite database Data Studio ships with: a small, made-up shop whose tables are shaped for every chart kind (so each chart
type has data worth drawing on day one). Built with the standard library only, deterministic (fixed seed), a few hundred KB.
`sample_guide` lists every table with what it is good for; the AI reads it too."""
import datetime
import json
import math
import os
import random
import sqlite3

VERSION = 3

GUIDE = (
    ("regions", "6 sales regions with coordinates", "map markers, bar"),
    ("customers", "1,200 customers: segment, plan, region, age, signup date", "pie / donut (segment, plan), histogram (age), line (signups per month)"),
    ("products", "37 products in 6 categories with price, cost, rating", "scatter / bubble (price vs rating, bubble = units), treemap (category > product)"),
    ("orders", "~10,000 orders over 2 years: channel, status, discount, quantity", "stacked bar (revenue by month and channel), line, heatmap (weekday x channel), pie (status)"),
    ("daily_metrics", "730 days of visitors, signups, revenue and a noisy trend with weekly + yearly seasonality", "line / area with zoom, scatter with trend line"),
    ("web_traffic", "sessions for every weekday x hour (168 cells)", "heatmap"),
    ("exam_scores", "600 students: class, subject, score, hours studied, sleep", "histogram (score), box plot (score by class), scatter with trend (hours vs score)"),
    ("employees", "54 employees in a company tree: department > team, salary, years", "treemap (department > team), horizontal bar (salary), box plot (salary by department)"),
    ("survey_responses", "5 survey questions x ratings 1-5 with counts", "stacked / horizontal bar, heatmap (question x rating)"),
    ("events", "2,000 app events with a JSON payload column", "SQLite JSON functions: json_extract(payload, '$.plan'); bar of events per kind"),
    ("v_revenue_by_month", "view: revenue and orders per month", "line / bar over time"),
    ("v_category_share", "view: revenue per product category", "pie / donut, treemap"),
)

FIRST = "Ava Noah Mia Liam Zoe Eli Ivy Leo Eva Max Amy Sam Lea Kai Uma Ben Ola Dev Ria Tom Joy Ray Sky Ned".split()
LAST = "Stone Rivera Chen Okafor Novak Silva Khan Berg Mori Costa Ito Haas Vega Lund Park Diaz Rossi Hale Wu Singh".split()
REGIONS = [("North America", "Americas", 40.7, -74.0), ("Latin America", "Americas", -23.5, -46.6), ("Europe", "EMEA", 51.5, -0.1), ("Middle East & Africa", "EMEA", 25.2, 55.3),
           ("South Asia", "APAC", 19.1, 72.9), ("East Asia & Pacific", "APAC", 35.7, 139.7)]
CATEGORIES = {"Laptops": (900, 2400), "Phones": (300, 1300), "Audio": (30, 400), "Wearables": (60, 500), "Home": (20, 350), "Gaming": (40, 700)}
DEPTS = {"Engineering": ["Platform", "Mobile", "Data", "Security"], "Sales": ["Enterprise", "SMB", "Partners"], "Marketing": ["Brand", "Growth"],
         "Support": ["Tier 1", "Tier 2"], "Finance": ["Accounting", "Planning"], "People": ["Recruiting"]}
QUESTIONS = ["Easy to use", "Good value", "Fast delivery", "Helpful support", "Would recommend"]
CHANNELS = ["web", "mobile app", "store", "partner"]


def _name(r):
    return "%s %s" % (r.choice(FIRST), r.choice(LAST))


def build(path):
    """Create (or replace) the database file at PATH."""
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    r = random.Random(20240607)
    db = sqlite3.connect(tmp)
    c = db.cursor()
    c.executescript("""
    CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT, continent TEXT, lat REAL, lng REAL);
    CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, segment TEXT, plan TEXT, region_id INTEGER REFERENCES regions(id), age INTEGER, signup_date TEXT);
    CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL, cost REAL, rating REAL, launched TEXT);
    CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), product_id INTEGER REFERENCES products(id), order_date TEXT,
                         quantity INTEGER, discount REAL, channel TEXT, status TEXT, total REAL);
    CREATE TABLE daily_metrics (day TEXT PRIMARY KEY, visitors INTEGER, signups INTEGER, revenue REAL, conversion REAL);
    CREATE TABLE web_traffic (weekday TEXT, weekday_no INTEGER, hour INTEGER, sessions INTEGER);
    CREATE TABLE exam_scores (student_id INTEGER PRIMARY KEY, class TEXT, subject TEXT, score REAL, hours_studied REAL, sleep_hours REAL);
    CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, department TEXT, team TEXT, title TEXT, salary REAL, years INTEGER, manager_id INTEGER);
    CREATE TABLE survey_responses (question TEXT, rating INTEGER, responses INTEGER);
    CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, user_id INTEGER, payload TEXT);
    CREATE TABLE sample_guide (table_name TEXT, about TEXT, good_for TEXT);
    """)
    c.executemany("INSERT INTO regions VALUES (?,?,?,?,?)", [(i + 1,) + x for i, x in enumerate(REGIONS)])
    start = datetime.date(2023, 1, 1)
    days = 730
    # customers
    rows = []
    for i in range(1, 1201):
        seg = r.choices(["Consumer", "Small business", "Enterprise", "Education"], [55, 25, 12, 8])[0]
        plan = r.choices(["free", "plus", "pro", "team"], [45, 30, 17, 8])[0]
        age = max(18, min(80, int(r.gauss(37, 12))))
        rows.append((i, _name(r), seg, plan, r.choices(range(1, 7), [30, 12, 26, 8, 14, 10])[0], age, (start + datetime.timedelta(days=int(r.random() ** 0.8 * days))).isoformat()))
    c.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?)", rows)
    # products
    prods, pid = [], 1
    for cat, (lo, hi) in CATEGORIES.items():
        for k in range(1, 7 if cat != "Gaming" else 8):
            price = round(r.uniform(lo, hi), 2)
            prods.append((pid, "%s %s%d" % (cat[:-1] if cat.endswith("s") else cat, r.choice(["Air", "Pro", "Lite", "Max", "One", "Go"]), k), cat, price,
                          round(price * r.uniform(0.45, 0.8), 2), round(min(5, max(2.5, r.gauss(4.0 - price / 4000, 0.5))), 1), (start + datetime.timedelta(days=r.randrange(0, 500))).isoformat()))
            pid += 1
    c.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", prods)
    # orders: seasonal, growing, with a channel mix that shifts to mobile
    orders, oid = [], 1
    for d in range(days):
        day = start + datetime.timedelta(days=d)
        season = 1 + 0.25 * math.sin(2 * math.pi * (day.timetuple().tm_yday - 80) / 365) + (0.35 if day.month in (11, 12) else 0)
        n = max(0, int(r.gauss(9 * season * (1 + d / 900) * (1.25 if day.weekday() < 5 else 0.8), 2.5)))
        for _ in range(n):
            p = r.choices(prods, [1 / (1 + p[3] / 400) for p in prods])[0]
            q = r.choices([1, 2, 3, 4, 5], [62, 22, 9, 5, 2])[0]
            disc = r.choices([0, 0.05, 0.1, 0.2], [60, 20, 14, 6])[0]
            mobile = 0.25 + 0.3 * d / days
            ch = r.choices(CHANNELS, [0.5 - mobile / 2, mobile, 0.2, 0.1])[0]
            st = r.choices(["delivered", "shipped", "cancelled", "returned"], [84, 6, 6, 4])[0]
            orders.append((oid, r.randint(1, 1200), p[0], day.isoformat(), q, disc, ch, st, round(p[3] * q * (1 - disc), 2)))
            oid += 1
    c.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    # daily metrics
    dm = []
    for d in range(days):
        day = start + datetime.timedelta(days=d)
        base = 1200 + d * 2.2 + 220 * math.sin(2 * math.pi * d / 365) + (-260 if day.weekday() >= 5 else 0)
        vis = max(100, int(r.gauss(base, 80)))
        conv = max(0.004, r.gauss(0.021 + d / 60000, 0.003))
        sign = int(vis * conv)
        dm.append((day.isoformat(), vis, sign, round(sign * r.uniform(45, 70), 2), round(conv, 4)))
    c.executemany("INSERT INTO daily_metrics VALUES (?,?,?,?,?)", dm)
    # web traffic heatmap: office hours on weekdays, evenings on weekends
    wt = []
    for wd, nm in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        for h in range(24):
            peak = (math.exp(-((h - 11) ** 2) / 12) + 0.8 * math.exp(-((h - 15) ** 2) / 10)) if wd < 5 else (0.9 * math.exp(-((h - 20) ** 2) / 14) + 0.3 * math.exp(-((h - 12) ** 2) / 30))
            wt.append((nm, wd + 1, h, max(3, int(r.gauss(40 + 420 * peak, 18)))))
    c.executemany("INSERT INTO web_traffic VALUES (?,?,?,?)", wt)
    # exam scores: hours studied drives score, classes differ in spread
    ex = []
    spread = {"Class A": (68, 9), "Class B": (74, 12), "Class C": (62, 7), "Class D": (79, 10)}
    for i in range(1, 601):
        cl = r.choice(list(spread))
        hrs = max(0.5, r.gauss(6, 2.4))
        mean, sd = spread[cl]
        ex.append((i, cl, r.choice(["Math", "Physics", "Biology", "History"]), round(max(5, min(100, mean + (hrs - 6) * 3.2 + r.gauss(0, sd * 0.6))), 1), round(hrs, 1), round(min(10, max(3.5, r.gauss(7, 1.1))), 1)))
    c.executemany("INSERT INTO exam_scores VALUES (?,?,?,?,?,?)", ex)
    # employees (a company tree)
    emp, eid = [(1, "Dana Whitfield", "Executive", "Leadership", "CEO", 310000.0, 14, None)], 2
    for dept, teams in DEPTS.items():
        head = eid
        emp.append((eid, _name(r), dept, teams[0], "VP " + dept, round(r.uniform(190, 250)) * 1000.0, r.randint(6, 12), 1))
        eid += 1
        for tm in teams:
            for _ in range(r.randint(3, 7) if dept == "Engineering" else r.randint(2, 4)):
                if eid > 61:
                    break
                lvl = r.choices(["Associate", "Specialist", "Senior", "Lead"], [30, 35, 25, 10])[0]
                emp.append((eid, _name(r), dept, tm, "%s %s" % (lvl, dept[:-3] if dept.endswith("ing") else dept), round(r.gauss({"Associate": 62, "Specialist": 82, "Senior": 112, "Lead": 140}[lvl] * (1.25 if dept == "Engineering" else 1), 8)) * 1000.0, r.randint(0, 11), head))
                eid += 1
    c.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?)", emp)
    # survey
    sv = []
    for qn, q in enumerate(QUESTIONS):
        w = [(3 + qn) ** 0.5, 2.5, 5 + qn, 9 + 2 * (qn % 2), 8 - qn % 3]
        sv += [(q, rt + 1, int(r.gauss(w[rt] * 60, 12))) for rt in range(5)]
    c.executemany("INSERT INTO survey_responses VALUES (?,?,?)", sv)
    # events with JSON payloads
    ev = []
    for i in range(1, 2001):
        k = r.choices(["page_view", "signup", "purchase", "error", "share"], [55, 10, 12, 8, 15])[0]
        pl = {"plan": r.choice(["free", "plus", "pro"]), "device": r.choice(["ios", "android", "web"])}
        if k == "purchase":
            pl["amount"] = round(r.uniform(9, 900), 2)
        if k == "error":
            pl["code"] = r.choice([400, 404, 500, 503])
        ev.append((i, (datetime.datetime(2024, 5, 1) + datetime.timedelta(minutes=r.randrange(0, 60 * 24 * 30))).isoformat(sep=" "), k, r.randint(1, 1200), json.dumps(pl)))
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?)", ev)
    c.executemany("INSERT INTO sample_guide VALUES (?,?,?)", GUIDE)
    c.executescript("""
    CREATE VIEW v_revenue_by_month AS SELECT substr(order_date,1,7) AS month, ROUND(SUM(total),2) AS revenue, COUNT(*) AS orders
        FROM orders WHERE status NOT IN ('cancelled','returned') GROUP BY 1 ORDER BY 1;
    CREATE VIEW v_category_share AS SELECT p.category, ROUND(SUM(o.total),2) AS revenue, SUM(o.quantity) AS units
        FROM orders o JOIN products p ON p.id=o.product_id WHERE o.status NOT IN ('cancelled','returned') GROUP BY 1 ORDER BY 2 DESC;
    CREATE INDEX ix_orders_date ON orders(order_date);
    CREATE INDEX ix_orders_customer ON orders(customer_id);
    PRAGMA user_version = %d;""" % VERSION)
    db.commit()
    db.close()
    os.replace(tmp, path)
    return path


def ensure(path):
    """Make sure the file exists and is the current version. -> path."""
    try:
        if os.path.isfile(path):
            db = sqlite3.connect(path)
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] == VERSION:
                    return path
            finally:
                db.close()
    except sqlite3.Error:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return build(path)
