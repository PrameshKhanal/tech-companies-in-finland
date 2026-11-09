#!/usr/bin/env python3
"""Rebuild finland-employment-data.md and its SVG charts from open data.

Sources (public, no API key needed):
  * Statistics Finland - Labour Force Survey tables (StatFin PxWeb API)
  * Eurostat - monthly unemployment, dataset une_rt_m (dissemination API)

Usage:  python scripts/update_employment_data.py
Standard library only.
"""
import datetime
import html
import itertools
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_MD = os.path.join(ROOT, "finland-employment-data.md")
CHART_DIR = os.path.join(ROOT, "assets", "employment")

PX = "https://pxdata.stat.fi/PXWeb/api/v1/en/StatFin/tyti"
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m"
UA = {"User-Agent": "tech-companies-in-finland/employment-page (github.com/PrameshKhanal)"}

SINCE_YEAR = 2015
EU27 = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]
COMPARE = [("FI", "Finland"), ("EE", "Estonia"), ("SE", "Sweden"), ("DK", "Denmark"),
           ("NO", "Norway"), ("DE", "Germany"), ("EU27_2020", "EU (27 countries)"),
           ("EA21", "Euro area")]

COUNTRY = {"AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "CY": "Cyprus", "CZ": "Czechia",
           "DE": "Germany", "DK": "Denmark", "EE": "Estonia", "EL": "Greece", "ES": "Spain",
           "FI": "Finland", "FR": "France", "HR": "Croatia", "HU": "Hungary", "IE": "Ireland",
           "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MT": "Malta",
           "NL": "Netherlands", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SE": "Sweden",
           "SI": "Slovenia", "SK": "Slovakia"}
ISSN = "1798-7857"

BLUE, ORANGE, GREEN, PURPLE ="#2563eb", "#d97706", "#059669", "#7c3aed"


# --------------------------------------------------------------------------- data access
_cache = {}


def http(url, body=None, tries=5):
    """GET (or POST a JSON body) and return parsed JSON. Retries when the server says 'slow down'."""
    key = (url, json.dumps(body, sort_keys=True))
    if key in _cache:
        return _cache[key]
    data = None if body is None else json.dumps(body).encode()
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                _cache[key] = json.load(resp)
                return _cache[key]
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise
            wait = int(exc.headers.get("Retry-After") or 0) or 5 * attempt
            print(f"HTTP {exc.code} from {url.split('?')[0]}; waiting {wait}s (attempt {attempt}/{tries})")
            time.sleep(wait)


def jsonstat(js):
    """Flatten a JSON-stat 2 cube into ({ids}, {(cat, cat, ...): value})."""
    ids = js["id"]
    orders = []
    for dim in ids:
        index = js["dimension"][dim]["category"]["index"]
        if isinstance(index, dict):
            index = [k for k, _ in sorted(index.items(), key=lambda kv: kv[1])]
        orders.append(index)
    values = js["value"]
    getter = (lambda i: values.get(str(i))) if isinstance(values, dict) else (lambda i: values[i])
    return ids, {combo: getter(i) for i, combo in enumerate(itertools.product(*orders))}


def by_key(ids, data, x_dim, key_dim, fixed=None):
    """-> {key: {x: value}} for a cube, optionally pinning other dimensions."""
    fixed = fixed or {}
    ix, ik = ids.index(x_dim), ids.index(key_dim)
    out = {}
    for combo, value in data.items():
        if any(combo[ids.index(d)] != v for d, v in fixed.items()):
            continue
        out.setdefault(combo[ik], {})[combo[ix]] = value
    return out


def px_values(table, var):
    meta = http(f"{PX}/{table}.px")
    return next(v["values"] for v in meta["variables"] if v["code"] == var)


def px_get(table, selection):
    body = {"query": [{"code": c, "selection": {"filter": "item", "values": v}}
                      for c, v in selection.items()],
            "response": {"format": "json-stat2"}}
    return jsonstat(http(f"{PX}/{table}.px", body))


def eurostat_rates(geos, age):
    url = (f"{EUROSTAT}?format=JSON&lang=EN&unit=PC_ACT&sex=T&age={age}&s_adj=SA"
           f"&sinceTimePeriod={SINCE_YEAR}-01" + "".join(f"&geo={g}" for g in geos))
    js = http(url)
    ids, data = jsonstat(js)
    return by_key(ids, data, "time", "geo"), js.get("updated", "")[:10]


# --------------------------------------------------------------------------- helpers
def month_label(ym):  # "2026-09" -> "September 2026"
    y, m = ym.split("-")
    return datetime.date(int(y), int(m), 1).strftime("%B %Y")


def quarter_label(q):  # "2026Q3" -> "Q3 2026"
    return f"Q{q[-1]} {q[:4]}"


def num(v, d=1):
    return "–" if v is None else f"{v:,.{d}f}"


def delta(a, b, d=1, unit=""):
    if a is None or b is None:
        return "–"
    return f"{a - b:+,.{d}f}{unit}"


def last_with(series, *keys):
    for x in sorted(series[keys[0]], reverse=True):
        if all(series[k].get(x) is not None for k in keys):
            return x
    raise RuntimeError("no common period")


def ordinal_highest(rank):
    if rank == 1:
        return "the highest"
    suffix = "th" if 10 <= rank % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return f"the {rank}{suffix}-highest"


def find_publication(month_text):
    """Find the Labour Force Survey release for e.g. 'September 2026' on stat.fi."""
    fallback = "https://stat.fi/en/statistics/tyti"
    try:
        req = urllib.request.Request(fallback, headers=UA)
        page = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        for href, title in re.findall(r'<a href="(/en/publication/[a-z0-9]+)">([^<]+)</a>', page):
            if month_text in title:
                return "https://stat.fi" + href
    except Exception as exc:  # network or layout change: fall back to the statistics page
        print("Could not locate the release page:", exc)
    print("Release page for", month_text, "not found; linking the statistics page instead.")
    return fallback


def official_key_selections(url, month_text):
    """The 'Key selections' bullets of the release page, or None if they can't be read for this month."""
    try:
        page = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode("utf-8", "replace")
        block = re.search(r"Key selections</h2>\s*<div><ul>(.*?)</ul>", page, re.S)
        items = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                 for t in re.findall(r"<li>(.*?)</li>", block.group(1), re.S)] if block else []
        return items if items and month_text in items[0] else None
    except Exception as exc:
        print("Could not read the release's key selections:", exc)
        return None


def prev_year(period):  # "2026-09" / "2026M09" / "2026Q3" / "2025" -> one year earlier
    return f"{int(period[:4]) - 1}{period[4:]}"


# --------------------------------------------------------------------------- SVG charts
W, H = 760, 340
ML, MR, MT, MB = 54, 24, 66, 40


def esc(s):
    return html.escape(str(s), quote=True)


def nice_ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    t, ticks = math.floor(lo / step) * step, []
    while t <= math.ceil(hi / step) * step + step / 2:
        ticks.append(round(t, 10))
        t += step
    return ticks


def frame(title, legend):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" '
        f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="8" fill="#ffffff" stroke="#d0d7de"/>',
        f'<text x="{ML}" y="28" font-size="15" font-weight="600" fill="#1f2328">{esc(title)}</text>',
    ]
    x = ML
    for name, color in legend:
        parts.append(f'<rect x="{x}" y="42" width="14" height="4" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{x + 20}" y="47" font-size="12" fill="#1f2328">{esc(name)}</text>')
        x += 20 + len(name) * 6.8 + 20
    return parts


def axes(parts, y0, y1, yticks, fmt, xticks, X):
    pw, ph = W - ML - MR, H - MT - MB
    for t in yticks:
        y = MT + ph * (1 - (t - y0) / (y1 - y0))
        parts.append(f'<line x1="{ML}" x2="{W - MR}" y1="{y:.1f}" y2="{y:.1f}" stroke="#d0d7de" stroke-width="1"/>')
        parts.append(f'<text x="{ML - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="#57606a">{fmt(t)}</text>')
    for i, label in xticks:
        parts.append(f'<text x="{X(i):.1f}" y="{H - MB + 20}" font-size="11" text-anchor="middle" fill="#57606a">{esc(label)}</text>')
    return pw, ph


def line_chart(path, title, xs, series, xticks, fmt=lambda v: f"{v:g}", label_last=False, decimals=1):
    vals = [v for _, _, vs in series for v in vs if v is not None]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1
    yticks = nice_ticks(lo - pad, hi + pad)
    y0, y1 = yticks[0], yticks[-1]
    pw, ph = W - ML - MR, H - MT - MB
    X = lambda i: ML + pw * i / (len(xs) - 1)
    Y = lambda v: MT + ph * (1 - (v - y0) / (y1 - y0))
    parts = frame(title, [(n, c) for n, c, _ in series] if len(series) > 1 else [])
    axes(parts, y0, y1, yticks, fmt, xticks, X)
    for name, color, vs in series:
        seg = []
        for i, v in enumerate(vs + [None]):
            if v is None:
                if len(seg) > 1:
                    pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in seg)
                    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2" '
                                 f'stroke-linejoin="round" stroke-linecap="round"/>')
                seg = []
            else:
                seg.append((i, v))
        last = max((i for i, v in enumerate(vs) if v is not None), default=None)
        if last is not None:
            parts.append(f'<circle cx="{X(last):.1f}" cy="{Y(vs[last]):.1f}" r="3.5" fill="{color}"/>')
            if label_last:
                parts.append(f'<text x="{X(last) - 6:.1f}" y="{Y(vs[last]) - 9:.1f}" font-size="12" font-weight="600" '
                             f'text-anchor="end" fill="{color}">{vs[last]:.{decimals}f}</text>')
    parts.append("</svg>")
    write(path, "\n".join(parts))


def bar_chart(path, title, labels, values, color=BLUE, decimals=0):
    hi = max(values)
    yticks = nice_ticks(0, hi * 1.1)
    y0, y1 = 0, yticks[-1]
    pw, ph = W - ML - MR, H - MT - MB
    slot = pw / len(labels)
    parts = frame(title, [])
    axes(parts, y0, y1, yticks, lambda v: f"{v:g}", [], lambda i: ML)
    for i, (label, v) in enumerate(zip(labels, values)):
        x = ML + slot * i + slot * 0.15
        bw = slot * 0.7
        top = MT + ph * (1 - v / y1)
        parts.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{MT + ph - top:.1f}" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{top - 6:.1f}" font-size="11" text-anchor="middle" fill="#1f2328">{v:.{decimals}f}</text>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{H - MB + 20}" font-size="11" text-anchor="middle" fill="#57606a">{esc(label)}</text>')
    parts.append("</svg>")
    write(path, "\n".join(parts))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def year_ticks(xs, marker):
    """Tick at the first period of each year (every second year when there are many)."""
    ticks = [(i, x[:4]) for i, x in enumerate(xs) if x.endswith(marker)]
    return ticks[::2] if len(ticks) > 8 else ticks


# --------------------------------------------------------------------------- main
def main():
    today = datetime.date.today().isoformat()

    # ---- Statistics Finland: monthly key indicators (table 135z)
    codes = {
        "employed": "tyti-Tyolliset", "employed_t": "tyolliset_trendi",
        "unemployed": "tyti-Tyottomat", "unemployed_t": "tyottomat_trendi",
        "emp_rate": "Tyollisyysaste_20_64", "emp_rate_t": "Tyollaste_20_64_trendi",
        "unemp_rate": "tyti-Tyottomyysaste", "unemp_rate_t": "tyottaste_trendi",
        "unemp_rate_sa": "Tyottaste_kausi",
        "youth": "Tyottomyysaste_15_24", "youth_t": "Tyottaste_15_24_trendi",
        "youth_sa": "Tyottaste_15_24_kausi",
    }
    months = [m for m in px_values("135z", "timeperiod_m") if int(m[:4]) >= SINCE_YEAR]
    ids, data = px_get("135z", {"timeperiod_m": months, "contentscode": list(codes.values())})
    raw = by_key(ids, data, "timeperiod_m", "contentscode")
    sf = {name: raw[code] for name, code in codes.items()}
    sf_latest = max(m for m, v in sf["employed"].items() if v is not None)
    sf_prev = prev_year(sf_latest)
    sf_label = month_label(sf_latest.replace("M", "-"))
    sf_prev_label = month_label(sf_prev.replace("M", "-"))

    # ---- Statistics Finland: men and women (table 135y), latest month vs a year earlier
    meta_y = {v["code"]: v for v in http(f"{PX}/135y.px")["variables"]}

    def code_of(var, text):
        return meta_y[var]["values"][meta_y[var]["valueTexts"].index(text)]

    sex_var, age_var = "sukupuoli_9_20180101", "ikaryhma_19_20190101"
    sex_codes = {"men": code_of(sex_var, "Males"), "women": code_of(sex_var, "Females")}
    age_code = code_of(age_var, "15 - 74")
    cont_codes = {"employed": code_of("contentscode", "Employed, 1000 persons"),
                  "unemployed": code_of("contentscode", "Unemployed, 1000 persons")}
    ids_y, data_y = px_get("135y", {"timeperiod_m": [sf_prev, sf_latest], sex_var: list(sex_codes.values()),
                                    age_var: [age_code], "contentscode": list(cont_codes.values())})

    def gender_change(sex, status):
        def at(m):
            want = {"timeperiod_m": m, sex_var: sex_codes[sex], age_var: age_code, "contentscode": cont_codes[status]}
            return data_y[tuple(want[d] for d in ids_y)]
        return at(sf_latest) - at(sf_prev)

    # ---- Statistics Finland: tech view
    quarters = [q for q in px_values("137l", "timeperiod_q") if int(q[:4]) >= SINCE_YEAR]
    ind_codes = px_values("137l", "toimiala_79_20180101")
    ind_names = dict(zip(ind_codes, next(v["valueTexts"] for v in http(f"{PX}/137l.px")["variables"]
                                         if v["code"] == "toimiala_79_20180101")))
    ict_ind = next(c for c, n in ind_names.items() if n.startswith("J Information and communication"))
    total_ind = next(c for c, n in ind_names.items() if n == "Total")
    emp_content = px_values("137l", "contentscode")[0]
    ids, data = px_get("137l", {"toimiala_79_20180101": [total_ind, ict_ind],
                                "timeperiod_q": quarters, "contentscode": [emp_content]})
    q = by_key(ids, data, "timeperiod_q", "toimiala_79_20180101", {"contentscode": emp_content})
    q_total, q_ict = q[total_ind], q[ict_ind]
    q_latest = max(x for x, v in q_ict.items() if v is not None)

    occ_meta = {v["code"]: v for v in http(f"{PX}/13au.px")["variables"]}
    occ = occ_meta["ammatti_19_20180101"]
    occ_names = dict(zip(occ["values"], occ["valueTexts"]))
    occ_total = next(c for c, n in occ_names.items() if n.startswith("SSS"))
    occ_prof = next(c for c, n in occ_names.items() if n.startswith("25 "))
    occ_tech = next(c for c, n in occ_names.items() if n.startswith("35 "))
    sex_total = occ_meta["sukupuoli_9_20180101"]["values"][0]
    years = occ_meta["timeperiod_y"]["values"]
    ids, data = px_get("13au", {"timeperiod_y": years, "sukupuoli_9_20180101": [sex_total],
                                "ammatti_19_20180101": [occ_total, occ_prof, occ_tech],
                                "contentscode": [occ_meta["contentscode"]["values"][0]]})
    o = by_key(ids, data, "timeperiod_y", "ammatti_19_20180101")
    o_total, o_prof, o_tech = o[occ_total], o[occ_prof], o[occ_tech]
    o_latest = max(y for y, v in o_prof.items() if v is not None)

    # ---- Eurostat
    geos = sorted(set(EU27) | {g for g, _ in COMPARE})
    es, es_updated = eurostat_rates(geos, "TOTAL")
    es_youth, _ = eurostat_rates(EU27 + ["EU27_2020", "EA21"], "Y_LT25")
    # Finland's own figures come from Statistics Finland (seasonally adjusted series);
    # every other country comes from Eurostat. Eurostat's own Finland estimate is kept for the note.
    es_fi_own = dict(es["FI"])
    es["FI"] = {m.replace("M", "-"): v for m, v in sf["unemp_rate_sa"].items() if v is not None}
    es_youth["FI"] = {m.replace("M", "-"): v for m, v in sf["youth_sa"].items() if v is not None}
    es_latest = last_with(es, "FI", "EU27_2020")
    es_prev = prev_year(es_latest)
    ranked = sorted(((es[g].get(es_latest), g) for g in EU27 if es[g].get(es_latest) is not None), reverse=True)
    fi_rank = [g for _, g in ranked].index("FI") + 1
    youth_latest = last_with(es_youth, "FI", "EU27_2020")
    y_ranked = sorted(((es_youth[g].get(youth_latest), g) for g in EU27
                       if es_youth.get(g, {}).get(youth_latest) is not None), reverse=True)
    fi_youth_rank = [g for _, g in y_ranked].index("FI") + 1
    pub_url = find_publication(sf_label)
    ref_date = datetime.date.today()
    ref_text = f"{ref_date.day}.{ref_date.month}.{ref_date.year}"
    ref_period = f"{sf_latest[:4]}, {sf_label.split()[0]}"

    # ---- charts
    sf_months = [m for m in months if sf["unemp_rate_t"].get(m) is not None]
    xs = [m.replace("M", "-") for m in sf_months]
    line_chart(os.path.join(CHART_DIR, "fi-unemployment-rate.svg"),
               "Finland: unemployment rate, ages 15–74, trend (%)", xs,
               [("Finland", BLUE, [sf["unemp_rate_t"][m] for m in sf_months])],
               year_ticks(xs, "-01"), label_last=True)
    line_chart(os.path.join(CHART_DIR, "fi-employment-rate.svg"),
               "Finland: employment rate, ages 20–64, trend (%)", xs,
               [("Finland", GREEN, [sf["emp_rate_t"][m] for m in sf_months])],
               year_ticks(xs, "-01"), label_last=True)
    es_months = sorted(t for t in es["FI"] if t >= f"{SINCE_YEAR}-01")
    line_chart(os.path.join(CHART_DIR, "europe-unemployment-rate.svg"),
               "Unemployment rate, seasonally adjusted (%): Finland vs Europe", es_months,
               [("Finland", BLUE, [es["FI"].get(t) for t in es_months]),
                ("Sweden", ORANGE, [es["SE"].get(t) for t in es_months]),
                ("EU", GREEN, [es["EU27_2020"].get(t) for t in es_months]),
                ("Euro area", PURPLE, [es["EA21"].get(t) for t in es_months])],
               year_ticks(es_months, "-01"))
    qs = sorted(x for x in q_ict if q_ict[x] is not None)
    line_chart(os.path.join(CHART_DIR, "fi-information-communication.svg"),
               "Finland: employed in information & communication (thousand people)", qs,
               [("Information & communication", BLUE, [q_ict[x] for x in qs])],
               year_ticks(qs, "Q1"), fmt=lambda v: f"{v:g}", label_last=True, decimals=0)
    ys = sorted(y for y in o_prof if o_prof[y] is not None)
    bar_chart(os.path.join(CHART_DIR, "fi-ict-professionals.svg"),
              "Finland: employed ICT professionals (thousand people)", ys, [o_prof[y] for y in ys])

    # ---- markdown
    L = []
    a = L.append
    a("# 🇫🇮 Finland Employment Data")
    a("")
    a("[⬅ Back to the list of tech companies](README.md)")
    a("")
    a(f"> 📅 **Last updated:** {today} · Statistics Finland data up to **{sf_label}** · "
      f"Eurostat data up to **{month_label(es_latest)}**")
    a("")
    a("A quick look at how the Finnish labour market is doing, how it compares with the rest of Europe, "
      "and what the numbers say about the information and communication (ICT) sector. "
      "All figures come from official open data and this page is generated by "
      "[`scripts/update_employment_data.py`](scripts/update_employment_data.py).")
    a("")
    a("---")
    a("")
    a(f"## 📊 Latest figures: {sf_label}")
    a("")
    def persons(thousands):
        return f"{int(round(thousands * 1000)):,}"

    a("#### Number of unemployed people")
    a("")
    a(f"## ~{persons(sf['unemployed'][sf_latest])}")
    a("")
    a(f"*{sf_label} · estimate from a sample survey*")
    a("")
    a("#### Europe Ranking")
    a("")
    a(f"## {fi_rank} of {len(ranked)}")
    a("")
    a(f"*{ordinal_highest(fi_rank).capitalize()} unemployment rate in the EU · {num(es['FI'][es_latest])}% in {month_label(es_latest)}*")
    a("")

    def compare(change, noun):  # change in thousand persons
        n = persons(abs(change))
        if round(change * 1000) == 0:
            return f"**no change** in {noun}"
        return f"**{n}** {'more' if change > 0 else 'fewer'} {noun}"

    month_name = sf_label.split()[0]
    official = official_key_selections(pub_url, sf_label)
    if official:  # the release's own "Key selections", with the numbers in bold
        bullets = [re.sub(r"(\d{1,3}(?:,\d{3})+|\d+\.\d+\s+per\s+cent)", r"**\1**", t) for t in official]
        print("Key figures: official wording from the release page")
    else:  # fallback: calculated from the (rounded) database tables
        print("Key figures: calculated from the database tables (release text not available)")
        bullets = [
            f"In {sf_label}, the number of employed persons was **{persons(sf['employed'][sf_latest])}** "
            f"and that of unemployed persons **{persons(sf['unemployed'][sf_latest])}**.",
            f"There were {compare(gender_change('men', 'employed'), 'employed men')} and "
            f"{compare(gender_change('women', 'employed'), 'employed women')} than in {sf_prev_label}.",
            f"There were {compare(gender_change('men', 'unemployed'), 'unemployed men')} and "
            f"{compare(gender_change('women', 'unemployed'), 'unemployed women')} than in {sf_prev_label}.",
            f"The trend of the employment rate among persons aged 20 to 64 was **{num(sf['emp_rate_t'][sf_latest])} per cent** in {month_name}.",
            f"The trend of the unemployment rate among persons aged 15 to 74 was **{num(sf['unemp_rate_t'][sf_latest])} per cent** in {month_name}.",
        ]
    a("**Key figures**")
    a("")
    for b in bullets:
        a(f"- {b}")
    a("")
    a("| Indicator | Latest (not adjusted) | Trend | Year ago | Change vs. year ago |")
    a("| --- | ---: | ---: | ---: | ---: |")
    rows = [("Employed (thousand people)", "employed", "employed_t", 0, ""),
            ("Unemployed (thousand people)", "unemployed", "unemployed_t", 0, ""),
            ("Employment rate, ages 20–64 (%)", "emp_rate", "emp_rate_t", 1, " pp"),
            ("Unemployment rate (%)", "unemp_rate", "unemp_rate_t", 1, " pp"),
            ("Youth unemployment rate, ages 15–24 (%)", "youth", "youth_t", 1, " pp")]
    for name, k, kt, d, unit in rows:
        a(f"| {name} | {num(sf[k][sf_latest], d)} | {num(sf[kt].get(sf_latest), d)} | "
          f"{num(sf[k].get(sf_prev), d)} | {delta(sf[k][sf_latest], sf[k].get(sf_prev), d, unit)} |")
    a("")
    a("*Trend* is adjusted for seasonal and random variation, so it is the best series for judging the "
      "direction of the labour market. The latest trend values are preliminary and may be revised. Ages 15–74 unless stated. "
      "The monthly figures come from a sample survey and carry a margin of error of tens of thousands of people for "
      "the totals, so small changes between months are not meaningful.")
    a("")
    a("![Finland unemployment rate (trend)](assets/employment/fi-unemployment-rate.svg)")
    a("")
    a("![Finland employment rate (trend)](assets/employment/fi-employment-rate.svg)")
    a("")
    a("---")
    a("")
    a(f"## 🇪🇺 How Finland compares with Europe: {month_label(es_latest)}")
    a("")
    a("Seasonally adjusted unemployment rate, ages 15–74. **Finland:** Official Statistics of Finland (Statistics Finland). "
      "**All other countries and regions:** Eurostat (harmonised ILO definition).")
    a("")
    eu_now, ea_now = es["EU27_2020"].get(es_latest), es["EA21"].get(es_latest)
    a(f"> 🏆 **Finland's ranking in Europe: {fi_rank} of {len(ranked)}.** "
      f"With {num(es['FI'][es_latest])}% in {month_label(es_latest)}, Finland has {ordinal_highest(fi_rank)} unemployment rate "
      f"of the {len(ranked)} EU countries with data (EU average {num(eu_now)}%).  ")
    a(f"> **Youth unemployment (under 25): {fi_youth_rank} of {len(y_ranked)}**, "
      f"{num(es_youth['FI'][youth_latest])}% in {month_label(youth_latest)}, so {ordinal_highest(fi_youth_rank)} among "
      f"the EU countries with data for that month (EU average {num(es_youth['EU27_2020'].get(youth_latest))}%).")
    a("")
    ordered = sorted(COMPARE, key=lambda gn: -(es[gn[0]].get(es_latest) or -1))
    a(f"| Region | {month_label(es_latest)} | {month_label(es_prev)} | Change (pp) |")
    a("| --- | ---: | ---: | ---: |")
    for g, name in ordered:
        now, before = es[g].get(es_latest), es[g].get(es_prev)
        label = f"**{name}**" if g == "FI" else name
        cell = f"**{num(now)}**" if g == "FI" else num(now)
        a(f"| {label} | {cell} | {num(before)} | {delta(now, before)} |")
    a("")
    a("<details>")
    a(f"<summary>Show the full EU ranking for {month_label(es_latest)} (1 = highest unemployment)</summary>")
    a("")
    a("| Rank | Country | Unemployment rate (%) |")
    a("| ---: | --- | ---: |")
    for i, (rate, g) in enumerate(ranked, 1):
        name = COUNTRY[g]
        a(f"| {i} | {'**' + name + '**' if g == 'FI' else name} | {'**' + num(rate) + '**' if g == 'FI' else num(rate)} |")
    a("")
    a("</details>")
    a("")
    a("![Unemployment rate: Finland vs Europe](assets/employment/europe-unemployment-rate.svg)")
    a("")
    fy, ey = es_youth["FI"], es_youth["EU27_2020"]
    a(f"**Youth unemployment (under 25), {month_label(youth_latest)}:** Finland {num(fy.get(youth_latest))}% "
      f"vs. EU {num(ey.get(youth_latest))}% "
      f"(a year earlier: {num(fy.get(prev_year(youth_latest)))}% vs. {num(ey.get(prev_year(youth_latest)))}%).")
    a("")
    own = es_fi_own.get(es_latest)
    own_text = f" Eurostat's own estimate for Finland in {month_label(es_latest)} is {num(own)}%." if own is not None else ""
    a("> **Why is Finland's rate here different from the table above?** The first table shows the unadjusted figure and the "
      f"trend. The comparison needs a seasonally adjusted figure ({num(es['FI'][es_latest])}% for {month_label(es_latest)}, "
      f"also from Statistics Finland), because that is what Eurostat publishes for the other countries.{own_text} "
      "Eurostat's figures are revised from release to release. Use the first table to follow Finland's own trend, "
      "and the Europe table to compare countries.")
    a("")
    a("---")
    a("")
    a("## 💻 The tech sector in Finland")
    a("")
    a("The Labour Force Survey tables used here do not break unemployment out for ICT workers, but they do show how many "
      "people are employed in the information and communication industry and in ICT occupations.")
    a("")
    a("### Employed in the information & communication industry")
    a("")
    a("Industry class J of the national industry classification (publishing, telecommunications, "
      "computer programming and consultancy, information services). Quarterly, thousand people.")
    a("")
    a("| Quarter | Information & communication | All industries | Share of all employed |")
    a("| --- | ---: | ---: | ---: |")
    for x in sorted(qs)[-9:][::-1]:
        share = q_ict[x] / q_total[x] * 100 if q_total.get(x) else None
        a(f"| {quarter_label(x)} | {num(q_ict[x], 0)} | {num(q_total.get(x), 0)} | {num(share)}% |")
    a("")
    qp = prev_year(q_latest)
    a(f"Change from {quarter_label(qp)} to {quarter_label(q_latest)}: {delta(q_ict[q_latest], q_ict.get(qp), 0)} thousand people. "
      f"Quarters differ seasonally, so compare the same quarter across years.")
    a("")
    a("![Employed in information and communication](assets/employment/fi-information-communication.svg)")
    a("")
    a("### Employed in ICT occupations")
    a("")
    a("People working in ICT jobs in any sector (for example developers working in a bank or a factory). Annual, thousand people.")
    a("")
    a("| Year | ICT professionals | ICT technicians | ICT professionals' share of all employed |")
    a("| --- | ---: | ---: | ---: |")
    for y in sorted(ys)[-8:][::-1]:
        share = o_prof[y] / o_total[y] * 100 if o_total.get(y) else None
        a(f"| {y} | {num(o_prof[y], 0)} | {num(o_tech.get(y), 0)} | {num(share)}% |")
    a("")
    a("![Employed ICT professionals](assets/employment/fi-ict-professionals.svg)")
    a("")
    a("*ICT professionals* are occupation group 25 and *ICT technicians* group 35 of the Finnish Classification of Occupations 2010. "
      "These are survey estimates rounded to thousands, so differences of a few thousand are within the margin of error.")
    a("")
    a("---")
    a("")
    a("## 🔎 Sources and notes")
    a("")
    a("- **Statistics Finland** (all Finnish figures), [Labour Force Survey](https://stat.fi/en/statistics/tyti). Tables: "
      "[135z key indicators](https://pxdata.stat.fi/PXWeb/pxweb/en/StatFin/StatFin__tyti/135z.px), "
      "[137l employed by industry](https://pxdata.stat.fi/PXWeb/pxweb/en/StatFin/StatFin__tyti/137l.px), "
      "[13au employed by occupation](https://pxdata.stat.fi/PXWeb/pxweb/en/StatFin/StatFin__tyti/13au.px).")
    a("- **Eurostat** (all other countries, EU and euro area), [monthly unemployment (une_rt_m)](https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table?lang=en) "
      "and its [methodology notes](https://ec.europa.eu/eurostat/cache/metadata/en/une_rt_m_esms.htm).")
    a("- The numbers come from labour force surveys and are estimates. Totals are rounded and may differ slightly between tables.")
    a("")
    a("### Finnish data citation")
    a("")
    a(f"> Official Statistics of Finland (OSF): Labour force survey [online publication]. Reference period: {ref_period}. "
      f"ISSN={ISSN}. Helsinki: Statistics Finland [Referenced: {ref_text}]. Access method: {pub_url}")
    a("")
    a("[Back to top ⬆️](#-finland-employment-data) · [⬅ Back to the list of tech companies](README.md)")
    write(OUT_MD, "\n".join(L))
    print("Wrote", OUT_MD)
    print("Statistics Finland latest:", sf_latest, "| Eurostat latest:", es_latest, "| Finland rank:", fi_rank, "of", len(ranked))


if __name__ == "__main__":
    main()
