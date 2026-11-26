#!/usr/bin/env python3
"""Find open doctor appointments in Lithuania's e-health booking system (IPR,
ipr.esveikata.lt) and cross-check the doctors against patient reviews.

Standard library only. Subcommands:

  config          show what was discovered from the live sites (paths, filters)
  professions     professions and their codes (filter with --q)
  municipalities  municipalities (filter with --q)
  services        sub-services of a profession: age group, sub-specialty, care level
  filters         referral (siuntimas) and payment filter options, as IPR names them
  slots           open slots, one row per doctor x clinic x service x funding
  reviews         reviews for one doctor from pincetas.lt and manodaktaras.lt
  report          slots + reviews for every doctor found, as Markdown (+ JSON)

Nothing about the sites is assumed if it can be read from them: API paths and
filter codes/labels come from IPR's own web-app bundle, review-site endpoints
from their home pages, generic clinic-name words from IPR's institution list.
Discovered values are cached per bundle version in ~/.cache/lt-doctor-appointment
(--refresh to re-read). Built-in values are used only as a fallback, with a
warning, when discovery fails.

Review analysis weighs each review by age (half-life, default 3 years) and by
how much it can be trusted (anonymous, one-off accounts, bursts, duplicates,
very short or shouting text). It never outputs reviewer IPs or identities.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import http.cookiejar
import json
import math
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UA = "lt-doctor-appointment-skill/0.2 (+https://github.com/aleksas/lt-doctor-appointment)"
TZ = ZoneInfo("Europe/Vilnius")
IPR_ORIGIN = "https://ipr.esveikata.lt"
PINCETAS = "https://www.pincetas.lt"
MANODAKTARAS = "https://www.manodaktaras.lt"
DELAY = 0.35  # seconds between requests to the same host
CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "lt-doctor-appointment"
CACHE_TTL_DAYS = 7

# Last known values. Used only when live discovery fails (a warning is printed).
FALLBACK = {
    "ipr": {
        "apiUrl": "/api",
        "paths": {
            "municipalitiesSearchPath": "/searchesNew/municipalities",
            "professionsSearchPath": "/searchesNew/professions",
            "institutionsSearchPath": "/searchesNew/institutions",
            "healthcareServicesSearchFilterPath": "/searchesNew/healthcare-filter-services",
            "appointmentTimeGroupsSearchPath": "/searches/appointments/times",
            "appointmentsGroupTimesSearchPath": "/searches/appointments/times/details",
        },
        "filters": {
            "referralNeedType": [{"member": "All", "value": 0, "label": "Visi"},
                                 {"member": "REQUIRED", "value": 1, "label": "Su siuntimu"},
                                 {"member": "NOT_REQUIRED", "value": 2, "label": "Be siuntimo"}],
            "paymentType": [{"member": "All", "value": 0, "label": "Visi"},
                            {"member": "VLK", "value": 1, "label": "Ligonių kasos"},
                            {"member": "Patient", "value": 2, "label": "Pacientas"}],
        },
    },
    "pincetas": {"search_url": f"{PINCETAS}/search", "fields": ["criteria", "city", "skill", "rating"]},
    "manodaktaras": {"suggest_path": "/suggestions", "doctor_prefix": "/gydytojas"},
}
# Names of things in IPR's bundle we look up. These are semantic anchors that
# change far less often than the values they point to.
IPR_PATH_KEYS = list(FALLBACK["ipr"]["paths"])
IPR_PARAMS = ["municipalityId", "professionCode", "healthcareServiceId", "referralNeedType",
              "paymentType", "organizationId", "fundType", "appointmentMethodId", "page", "size"]


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP


class Http:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.last: dict[str, float] = {}

    def _wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        gap = time.monotonic() - self.last.get(host, 0)
        if gap < DELAY:
            time.sleep(DELAY - gap)
        self.last[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, data: dict | None = None,
            headers: dict | None = None) -> str:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        hdrs = {"User-Agent": UA, "Accept-Language": "lt,en;q=0.8", **(headers or {})}
        for attempt in range(4):
            self._wait(url)
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs)
                with self.opener.open(req, timeout=60) as r:
                    return r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("unreachable")

    def json(self, url: str, **kw) -> object:
        return json.loads(self.get(url, **kw))


# ---------------------------------------------------------------- helpers


def fold(s: str) -> str:
    """Lowercase, strip diacritics: 'Šalnienė' -> 'salniene'."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def name_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", fold(s)) if len(t) > 1}


def text_of(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(t)).strip()


def ms_to_local(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def js_string(s: str) -> str:
    """Decode a JS string literal body ('Ligoni\\u0173 kasos' -> 'Ligonių kasos')."""
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def cache_read(name: str, max_age_days: float | None = None) -> object | None:
    p = CACHE / name
    if not p.exists():
        return None
    if max_age_days is not None and time.time() - p.stat().st_mtime > max_age_days * 86400:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_write(name: str, value: object) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / name).write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        warn(f"cannot write cache {name}: {e}")


# ---------------------------------------------------------------- discovery


def parse_ipr_bundle(js: str) -> dict:
    """Read API paths, filter enums (codes + labels) and check param names in
    IPR's Angular bundle. Raises ValueError when something essential is missing."""
    api = re.search(r'apiUrl:"([^"]*)"', js)
    if not api:
        raise ValueError("apiUrl not found")
    found = dict(re.findall(r'(\w+Path):"(/[^"]*)"', js))
    paths = {k: found[k] for k in IPR_PATH_KEYS if k in found}
    missing = [k for k in IPR_PATH_KEYS if k not in found]
    if missing:
        raise ValueError(f"paths not found: {missing}")
    filters = {}
    for param in ("referralNeedType", "paymentType"):
        var = re.search(r"e\.%s===(\w+)\.\w+" % param, js)
        if not var:
            raise ValueError(f"enum for {param} not found")
        v = re.escape(var.group(1))
        block = re.search(r"function\(n\)\{((?:n\[n\.\w+=\d+\]=\"\w+\",?)+)\}\(%s\|\|" % v, js)
        if not block:
            raise ValueError(f"enum body for {param} not found")
        members = re.findall(r"n\[n\.(\w+)=(\d+)\]", block.group(1))
        labels = {m: js_string(l) for m, l in re.findall(r'new \w+\(%s\.(\w+),"([^"]+)"\)' % v, js)}
        filters[param] = [{"member": m, "value": int(n), "label": labels.get(m, m)} for m, n in members]
    absent = [p for p in IPR_PARAMS if f'"{p}"' not in js]
    if absent:
        warn(f"IPR bundle no longer mentions request params {absent}; results may be off")
    return {"apiUrl": api.group(1), "paths": paths, "filters": filters}


def ipr_config(h: Http, refresh: bool = False) -> dict:
    try:
        index = h.get(IPR_ORIGIN + "/")
        bundle = re.search(r'src="(main\.[0-9a-f]+\.js)"', index)
        if not bundle:
            raise ValueError("main bundle not referenced from index.html")
        key = f"ipr-{bundle.group(1)}.json"
        if not refresh and (cached := cache_read(key)):
            return cached
        cfg = parse_ipr_bundle(h.get(f"{IPR_ORIGIN}/{bundle.group(1)}"))
        cfg["bundle"] = bundle.group(1)
        cache_write(key, cfg)
        return cfg
    except (ValueError, urllib.error.URLError) as e:
        warn(f"IPR discovery failed ({e}); using built-in fallback values")
        return {**FALLBACK["ipr"], "bundle": None}


def pincetas_config(h: Http, refresh: bool = False) -> dict:
    if not refresh and (c := cache_read("pincetas.json", CACHE_TTL_DAYS)):
        h.get(PINCETAS + "/")  # session cookie for the search endpoint
        return c
    try:
        s = h.get(PINCETAS + "/")
        url = re.search(r'url:\s*"([^"]+)\?page="\s*\+\s*page', s)
        q = re.search(r"function generateQuery\(\)\s*\{\s*params\s*=\s*\{(.*?)\};", s, re.S)
        if not url or not q:
            raise ValueError("search endpoint not found on home page")
        c = {"search_url": url.group(1), "fields": re.findall(r"(\w+)\s*:", q.group(1))}
        cache_write("pincetas.json", c)
        return c
    except (ValueError, urllib.error.URLError) as e:
        warn(f"pincetas.lt discovery failed ({e}); using fallback")
        return FALLBACK["pincetas"]


def manodaktaras_config(h: Http, refresh: bool = False) -> dict:
    if not refresh and (c := cache_read("manodaktaras.json", CACHE_TTL_DAYS)):
        return c
    try:
        s = h.get(MANODAKTARAS + "/")
        sug = re.search(r"data-autocomplete='\{\"serviceUrl\":\"([^\"]+)\"", s)
        # doctor pages look like /<prefix>/<specialty>/<slug>/<id>; clinics have one segment less
        doc = re.findall(r'href="(/[a-z-]+)/[a-z0-9-]+/[a-z0-9-]+/\d+"', s)
        if not sug or not doc:
            raise ValueError("autocomplete or doctor links not found on home page")
        c = {"suggest_path": sug.group(1), "doctor_prefix": max(set(doc), key=doc.count)}
        cache_write("manodaktaras.json", c)
        return c
    except (ValueError, urllib.error.URLError) as e:
        warn(f"manodaktaras.lt discovery failed ({e}); using fallback")
        return FALLBACK["manodaktaras"]


# ---------------------------------------------------------------- IPR


class Ipr:
    def __init__(self, h: Http, refresh: bool = False) -> None:
        self.h, self.refresh = h, refresh
        self.cfg = ipr_config(h, refresh)

    def url(self, key: str) -> str:
        return IPR_ORIGIN + self.cfg["apiUrl"] + self.cfg["paths"][key]

    def _data(self, key: str, **params) -> list[dict]:
        d = self.h.json(self.url(key), params=params or None)
        return d.get("data", d) if isinstance(d, dict) else d

    def professions(self) -> list[dict]:
        return self._data("professionsSearchPath")

    def municipalities(self) -> list[dict]:
        return [{"id": m["admId"], "name": m["pavadinimas"]} for m in self._data("municipalitiesSearchPath")]

    def services(self, profession_id: int) -> list[dict]:
        return [{"id": s["id"], "name": s["name"]}
                for s in self._data("healthcareServicesSearchFilterPath", professionId=profession_id)]

    def generic_clinic_words(self) -> set[str]:
        """Words so common in IPR institution names (UAB, VšĮ, poliklinika, ...)
        that they say nothing about which clinic is meant."""
        if not self.refresh and (c := cache_read("generic-clinic-words.json", CACHE_TTL_DAYS)):
            return set(c)
        names = [i.get("istgPavadinimas") or "" for i in self._data("institutionsSearchPath")]
        df: dict[str, int] = {}
        for n in names:
            for t in name_tokens(n):
                df[t] = df.get(t, 0) + 1
        cut = max(5, len(names) // 100)  # in >1% of institution names
        words = sorted(t for t, c in df.items() if c >= cut)
        cache_write("generic-clinic-words.json", words)
        return set(words)

    def filter_options(self, param: str) -> list[dict]:
        return self.cfg["filters"][param]

    def resolve_filter(self, param: str, q: str | None) -> dict | None:
        """Match user input against the enum member, its label or its value."""
        if q is None:
            return None
        opts = self.filter_options(param)
        fq = fold(q).replace("-", "_").replace(" ", "_")
        for o in opts:
            if fq in (fold(o["member"]), fold(o["label"]).replace(" ", "_"), str(o["value"])):
                return o
        hits = [o for o in opts if fold(q) in fold(o["label"])]
        if len(hits) == 1:
            return hits[0]
        raise SystemExit(f"{param}: '{q}' is not one of " +
                         ", ".join(f'{o["member"]} ("{o["label"]}")' for o in opts))

    def resolve_profession(self, q: str) -> dict:
        profs = self.professions()
        hits = [p for p in profs if p["code"] == q] if q.isdigit() else \
               [p for p in profs if fold(q) in fold(p["name"])]
        if len(hits) != 1:
            names = "; ".join(f'{p["code"]} {p["name"]}' for p in hits[:15])
            raise SystemExit(f"profession '{q}' matched {len(hits)}: {names or 'none'}. "
                             "Use the exact code from `professions`.")
        return hits[0]

    def resolve_municipalities(self, qs: list[str]) -> list[dict]:
        munis, out = self.municipalities(), []
        for q in qs:
            hits = [m for m in munis if str(m["id"]) == q or fold(q) in fold(m["name"])]
            exact = [m for m in hits if fold(m["name"]) == fold(q) or str(m["id"]) == q]
            if not hits:
                raise SystemExit(f"municipality '{q}' not found; see `municipalities`.")
            if len(hits) > 1 and not exact:
                raise SystemExit(f"municipality '{q}' is ambiguous: " +
                                 "; ".join(f'{m["id"]} {m["name"]}' for m in hits))
            out += exact or hits
        return out

    def resolve_services(self, profession: dict, qs: list[str] | None) -> list[dict] | None:
        if not qs:
            return None
        svcs, out = self.services(profession["id"]), []
        for q in qs:
            hits = [s for s in svcs if str(s["id"]) == q or fold(q) in fold(s["name"])]
            if not hits:
                raise SystemExit(f"service '{q}' not found; see `services --profession {profession['code']}`.")
            out += [s for s in hits if s not in out]
        return out

    def _paged(self, key: str, params: dict) -> list[dict]:
        rows, page = [], 0
        while page < 200:
            d = self.h.json(self.url(key), params={**params, "page": page, "size": 50})
            if not d.get("data"):
                break
            rows += d["data"]
            page += 1
        return rows

    def slots(self, profession_code: str, municipality_ids: list[int], referral: dict | None = None,
              payment: dict | None = None, services: list[dict] | None = None,
              details: bool = True) -> list[dict]:
        """One row per doctor x clinic x service x funding, with the earliest free time."""
        base_q = {"professionCode": profession_code}
        if referral:
            base_q["referralNeedType"] = referral["value"]
        if payment:
            base_q["paymentType"] = payment["value"]
        rows = []
        for mid in municipality_ids:
            for svc in services or [None]:
                q = {**base_q, "municipalityId": mid}
                if svc:
                    q["healthcareServiceId"] = svc["id"]
                for g in self._paged("appointmentTimeGroupsSearchPath", q):
                    base = {
                        "municipality_id": mid,
                        "clinic": (g.get("organizationName") or "").strip(),
                        "clinic_id": g["organizationId"],
                        "service": g["healthcareServiceName"],
                        "service_id": g["healthcareServiceId"],
                        "funding": g["fundType"]["name"], "funding_code": g["fundType"]["type"],
                        "referral": g["referralNeed"]["name"], "referral_code": g["referralNeed"]["type"],
                        "method": g.get("appointmentMethod"),
                    }
                    if not details:
                        rows.append({**base, "doctor": None, "earliest": ms_to_local(g["earliestTime"]),
                                     "free_times": g["periodTimesCount"]})
                        continue
                    for s in self._paged("appointmentsGroupTimesSearchPath", {
                            "organizationId": g["organizationId"],
                            "healthcareServiceId": g["healthcareServiceId"],
                            "fundType": g["fundType"]["type"],
                            "referralNeedType": g["referralNeed"]["type"],
                            "professionCode": profession_code,
                            "appointmentMethodId": g["appointmentMethodId"]}):
                        first = (s.get("specialistFirstName") or "").strip().title()
                        last = (s.get("specialistLastName") or "").strip().title()
                        rows.append({**base, "doctor": f"{first} {last}".strip(),
                                     "specialist_id": s.get("specialistId"),
                                     "earliest": ms_to_local(s["earliestTime"]),
                                     "free_times": s["periodTimesCount"],
                                     "room": s.get("patalposPavadinimas")})
        rows.sort(key=lambda r: r["earliest"])
        return rows


# ---------------------------------------------------------------- name matching


def match_score(wanted: str, candidate: str) -> int:
    """3 = same name tokens, 2 = one name contains the other,
    1 = surname and first given name both present, 0 = no match."""
    w, c = name_tokens(wanted), name_tokens(candidate)
    if not w or not c:
        return 0
    if w == c:
        return 3
    if w <= c or c <= w:
        return 2
    parts = fold(wanted).split()
    if parts[0] in c and set(parts[-1].split("-")) & c:
        return 1
    return 0


CONFIDENCE = {3: "exact", 2: "partial name", 1: "weak"}


def stems_overlap(a: set[str], b: set[str]) -> bool:
    """Token overlap tolerant of Lithuanian endings: 'narmeda' ~ 'narmedos'."""
    def stem(t: str) -> str:
        return t[:5] if len(t) >= 5 else t
    return bool({stem(t) for t in a} & {stem(t) for t in b})


def specialty_hint(profession_name: str) -> str:
    """Stem of the most specific word of the profession name:
    'Gydytojas dermatovenerologas' -> 'derma'."""
    return max(name_tokens(profession_name), key=len)[:5]


# ---------------------------------------------------------------- pincetas.lt


class Pincetas:
    source = "pincetas.lt"

    def __init__(self, h: Http, refresh: bool = False) -> None:
        self.h = h
        self.cfg = pincetas_config(h, refresh)

    def search(self, query: str) -> list[dict]:
        fields = {f: "" for f in self.cfg["fields"]}
        fields[self.cfg["fields"][0]] = query  # the free-text field comes first
        d = json.loads(self.h.get(f'{self.cfg["search_url"]}?page=1', data=fields,
                                  headers={"X-Requested-With": "XMLHttpRequest"}))
        out = []
        for li in re.findall(r'<li class="doctor">(.*?)</li>', d["doctor"]["html"], re.S):
            m = re.search(r'href="([^"]+)">([^<]+)</a>', li)
            if m:
                labels = [text_of(x) for x in re.findall(r'<span class="label[^"]*">(.*?)</span>', li)]
                out.append({"url": m.group(1), "name": html.unescape(m.group(2)).strip(),
                            "specialties": labels})
        return out

    def query_for(self, name: str) -> str:
        return " ".join(reversed(name.split()))  # the site lists 'Surname Name'

    def profile(self, url: str) -> dict:
        s = self.h.get(url)
        g = lambda p: (m.group(1).strip() if (m := re.search(p, s, re.S)) else None)  # noqa: E731
        recommend = g(r'itemprop="ratingValue">\s*([\d.,]+)\s*%')
        ratings = g(r'itemprop="ratingCount">\s*(\d+)')
        licence = g(r'panel-license.*?<p class="name">\s*(.*?)\s*</p>')
        workplaces = [re.sub(r"^\d+\.\s*", "", text_of(x)) for x in re.findall(
            r'itemprop="hospitalAffiliation"[^>]*>(.*?)</div>', s, re.S)]
        chart = re.search(r"new Chart\(.*?labels:\s*\[(.*?)\].*?data:\s*\[(.*?)\]", s, re.S)
        trend = {}
        if chart:
            trend = dict(zip(re.findall(r'"(\d{4})"', chart.group(1)),
                             map(int, re.findall(r"(\d+)", chart.group(2)))))
        reviews = []
        for cls, info, body in re.findall(
                r'<div\s+class="comment clearfix([^"]*)">\s*<div\s+class="info">(.*?)</div>\s*'
                r'<div\s+class="message"[^>]*>(.*?)</div>', s, re.S):
            classes = cls.split()
            date = re.search(r"(\d{4}-\d{2}-\d{2})", info)
            count = re.search(r'glyphicon-comment"[^>]*></span>\s*(\d+)', info)
            reviews.append({
                "source": self.source,
                "date": date.group(1) if date else None,
                "sentiment": "positive" if "positive" in classes else
                             "negative" if "negative" in classes else "neutral",
                "author": "registered" if "loggedin" in classes else "anonymous",
                "author_review_count": int(count.group(1)) if count else None,
                "text": text_of(body),
            })  # deliberately no IP / author identity
        return {"source": self.source, "url": url,
                "site_recommend_pct": float(recommend.replace(",", ".")) if recommend else None,
                "site_ratings": int(ratings) if ratings else None,
                "licence": text_of(licence) if licence else None,
                "workplaces": workplaces,
                "cumulative_recommend_pct_by_year": trend,
                "reviews": reviews}


# ---------------------------------------------------------------- manodaktaras.lt


class ManoDaktaras:
    source = "manodaktaras.lt"

    def __init__(self, h: Http, refresh: bool = False) -> None:
        self.h = h
        self.cfg = manodaktaras_config(h, refresh)

    def search(self, query: str) -> list[dict]:
        d = self.h.json(MANODAKTARAS + self.cfg["suggest_path"], params={"query": query},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        # the site redirects /<prefix>/<anything>/<anything>/<id> to the canonical page
        return [{"name": x["name"], "specialties": x.get("specialties", []),
                 "url": f'{MANODAKTARAS}{self.cfg["doctor_prefix"]}/-/-/{x["id"]}'}
                for x in d.get("suggestions", []) if x.get("entity_type") == "doctor"]

    def query_for(self, name: str) -> str:
        return name

    def profile(self, url: str) -> dict:
        s = self.h.get(url)
        m = re.search(r"<strong>(\d+)%</strong>\s*\((\d+)\s*\w+", s)
        reviews = []
        for block in s.split("data-comment-item")[1:]:
            meta = re.search(r'<div class="meta">(.*?)</div>', block, re.S)
            body = re.search(r"<p>(.*?)</p>", block, re.S)
            if not meta or not body:
                continue
            mt = fold(text_of(meta.group(1)))
            date = re.search(r"(\d{4}-\d{2}-\d{2})", mt)
            # the site's own wording: 'Rekomenduoja' / 'Nerekomenduoja'
            reviews.append({
                "source": self.source,
                "date": date.group(1) if date else None,
                "sentiment": "negative" if "nerekomend" in mt else
                             "positive" if "rekomend" in mt else "neutral",
                "author": "verified visit",
                "author_review_count": None,
                "text": text_of(body.group(1)),
            })
        clinics = sorted(set(re.findall(r'"availableAtOrFrom":\{"@id":"[^"]*/klinika/([^/"]+)/', s)))
        return {"source": self.source, "url": url,
                "site_recommend_pct": float(m.group(1)) if m else None,
                "site_ratings": int(m.group(2)) if m else None,
                "workplaces": clinics,
                "reviews": reviews}


# ---------------------------------------------------------------- review analysis

# pincetas.lt replaces the text of negative reviews older than ~3 years with a
# placeholder ("... todėl nebeaktualus ir nerodomas"); the vote still counts.
HIDDEN_TEXT = re.compile(r"nebeaktual\w* ir nerodom\w*", re.I)
# Flags that should make a reader look twice. Milder signals (anonymous,
# single-review account, old, hidden text) lower the weight but aren't counted.
SUSPICIOUS = {"burst", "near-duplicate", "very short", "shouting", "many exclamations"}


def _age_years(date: str, today: dt.date) -> float:
    return max(0.0, (today - dt.date.fromisoformat(date)).days / 365.25)


def _wilson_lower(pos: float, n: float, z: float = 1.64) -> float | None:
    if n <= 0:
        return None
    p = pos / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z * z / n))


def flag_reviews(reviews: list[dict], today: dt.date) -> None:
    """Annotate each review with 'flags' and a 'trust' weight in (0, 1]."""
    for r in reviews:
        r["flags"] = []
        r["text_hidden_by_site"] = bool(HIDDEN_TEXT.search(r["text"]))
    dated = sorted((r for r in reviews if r.get("date")), key=lambda r: r["date"])
    for i, r in enumerate(dated):  # 3+ same-sentiment reviews within 7 days
        d0 = dt.date.fromisoformat(r["date"])
        window = [x for x in dated[i:] if x["sentiment"] == r["sentiment"]
                  and (dt.date.fromisoformat(x["date"]) - d0).days <= 7]
        if len(window) >= 3:
            for x in window:
                if "burst" not in x["flags"]:
                    x["flags"].append("burst")
    norm = [(r, re.sub(r"\W+", " ", fold(r["text"])).strip()) for r in reviews
            if not r["text_hidden_by_site"]]
    for i, (a, ta) in enumerate(norm):
        for b, tb in norm[i + 1:]:
            if len(ta) > 25 and len(tb) > 25 and difflib.SequenceMatcher(None, ta, tb).ratio() > 0.85:
                for x in (a, b):
                    if "near-duplicate" not in x["flags"]:
                        x["flags"].append("near-duplicate")
    for r in reviews:
        t = r["text"]
        letters = [c for c in t if c.isalpha()]
        if r["text_hidden_by_site"]:
            r["flags"].append("text hidden by site")
        else:
            if len(t) < 25:
                r["flags"].append("very short")
            if len(letters) > 15 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
                r["flags"].append("shouting")
            if t.count("!") >= 4:
                r["flags"].append("many exclamations")
        if r["author"] == "anonymous":
            r["flags"].append("anonymous")
        if r["author"] == "registered" and r.get("author_review_count") == 1:
            r["flags"].append("single-review account")
        if r.get("date") and _age_years(r["date"], today) > 5:
            r["flags"].append("older than 5 years")
        f = set(r["flags"])
        trust = 1.0
        trust *= 0.6 if "anonymous" in f else 1
        trust *= 0.8 if "single-review account" in f else 1
        trust *= 0.5 if "burst" in f else 1
        trust *= 0.5 if "near-duplicate" in f else 1
        trust *= 0.7 if "very short" in f else 1
        trust *= 0.7 if f & {"shouting", "many exclamations"} else 1
        r["trust"] = round(trust, 3)


def analyze(reviews: list[dict], today: dt.date | None = None,
            half_life_years: float = 3.0, recent_months: int = 24) -> dict:
    today = today or dt.date.today()
    flag_reviews(reviews, today)
    rated = [r for r in reviews if r.get("date") and r["sentiment"] in ("positive", "negative")]
    wpos = wsum = 0.0
    for r in rated:
        w = 0.5 ** (_age_years(r["date"], today) / half_life_years) * r["trust"]
        r["weight"] = round(w, 3)
        wsum += w
        wpos += w if r["sentiment"] == "positive" else 0
    cutoff = (today - dt.timedelta(days=round(recent_months * 30.44))).isoformat()
    recent = [r for r in rated if r["date"] >= cutoff]
    older = [r for r in rated if r["date"] < cutoff]

    def pct(rs):
        return round(100 * sum(r["sentiment"] == "positive" for r in rs) / len(rs)) if rs else None
    by_year: dict[str, dict] = {}
    for r in rated:
        by_year.setdefault(r["date"][:4], {"positive": 0, "negative": 0})[r["sentiment"]] += 1
    wl = _wilson_lower(wpos, wsum)
    return {
        "reviews_total": len(reviews),
        "rated": len(rated),
        "neutral": sum(r["sentiment"] == "neutral" for r in reviews),
        "positive_pct_all_time": pct(rated),
        "recent": {"months": recent_months, "n": len(recent), "positive_pct": pct(recent)},
        "older": {"n": len(older), "positive_pct": pct(older)},
        "weighted_positive_pct": round(100 * wpos / wsum) if wsum else None,
        "effective_n": round(wsum, 1),
        "weighted_lower_bound_pct": round(100 * wl) if wl is not None else None,
        "latest_review": max((r["date"] for r in rated), default=None),
        "by_year": dict(sorted(by_year.items())),
        "flagged_count": sum(bool(set(r["flags"]) & SUSPICIOUS) for r in reviews),
        "negative_text_hidden_by_site": sum(r["text_hidden_by_site"] for r in reviews),
        "half_life_years": half_life_years,
    }


class Reviews:
    def __init__(self, h: Http, refresh: bool = False, generic_words: set[str] | None = None,
                 half_life_years: float = 3.0, recent_months: int = 24) -> None:
        self.sites = [Pincetas(h, refresh), ManoDaktaras(h, refresh)]
        self.generic = generic_words or set()
        self.half_life, self.recent_months = half_life_years, recent_months

    def doctor(self, name: str, clinics: list[str] | None = None, hint: str | None = None) -> dict:
        hint = fold(hint) if hint else None
        clinic_words = set().union(set(), *(name_tokens(c) for c in clinics or [])) - self.generic
        out: dict = {"doctor": name, "sources": []}

        def pick(cands):
            scored = sorted(((match_score(name, c["name"]), c) for c in cands), key=lambda sc: -sc[0])
            scored = [(s, c) for s, c in scored if s]
            if hint:
                scored = [(s, c) for s, c in scored if any(hint in fold(x) for x in c["specialties"])] or scored
            return scored

        surname = name.split()[-1].split("-")[0]
        for site in self.sites:
            try:
                cands = pick(site.search(site.query_for(name))) or pick(site.search(surname))
                if not cands:
                    out["sources"].append({"source": site.source, "found": False})
                    continue
                best = cands[0][0]
                pairs = [(c, site.profile(c["url"])) for s, c in cands if s == best][:3]

                def overlap(pair):
                    return stems_overlap(clinic_words, set().union(set(), *(name_tokens(w) for w in pair[1]["workplaces"])))
                if len(pairs) > 1 and clinic_words:
                    pairs.sort(key=overlap, reverse=True)
                cand, p = pairs[0]
                p.update(found=True, matched_name=cand["name"], specialties=cand["specialties"],
                         match=CONFIDENCE[best] + (" (several profiles with this name; picked by workplace)"
                                                   if len(pairs) > 1 else ""))
                if hint and not any(hint in fold(x) for x in cand["specialties"]):
                    p["specialty_mismatch"] = ", ".join(cand["specialties"]) or "none listed"
                if clinic_words:
                    p["workplace_matches_clinic"] = overlap(pairs[0])
                p["analysis"] = analyze(p["reviews"], None, self.half_life, self.recent_months)
                out["sources"].append(p)
            except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
                out["sources"].append({"source": site.source, "found": False,
                                       "error": f"{type(e).__name__}: {e}"})
        return out


# ---------------------------------------------------------------- report


def _fmt_src(s: dict) -> str:
    if not s.get("found"):
        return "error" if s.get("error") else "not found"
    a, rec = s["analysis"], s["analysis"]["recent"]
    parts = [f'{s["site_recommend_pct"]:.0f}% of {s["site_ratings"]}' if s.get("site_recommend_pct") is not None
             else f'{s.get("site_ratings") or 0} ratings']
    if a["weighted_positive_pct"] is not None:
        parts.append(f'weighted {a["weighted_positive_pct"]}% (eff. n {a["effective_n"]})')
    if rec["n"]:
        parts.append(f'last {rec["months"]}m {rec["positive_pct"]}% of {rec["n"]}')
    else:
        parts.append(f'none in last {rec["months"]}m (latest {a["latest_review"] or "—"})')
    if a["flagged_count"]:
        parts.append(f'{a["flagged_count"]} flagged')
    if not s.get("match", "").startswith("exact") or "several" in s.get("match", ""):
        parts.append(f'match: {s["match"]}')
    if s.get("specialty_mismatch"):
        parts.append(f'listed as: {s["specialty_mismatch"]}')
    if s.get("workplace_matches_clinic") is False:
        parts.append("clinic not on profile")
    return "; ".join(parts)


def _short_service(service: str) -> str:
    """'Dermatovenerologija (Vaikams) II lygis' -> 'Vaikams II lygis'."""
    m = re.search(r"\((.*?)\)\s*(.*)", service)
    return f"{m.group(1)} {m.group(2)}".strip() if m else service


def build_report(ipr: Ipr, reviews: Reviews | None, profession: str, municipalities: list[str],
                 referral: str | None, payment: str | None, services: list[str] | None,
                 hint: str | None) -> tuple[str, dict]:
    prof = ipr.resolve_profession(profession)
    munis = ipr.resolve_municipalities(municipalities)
    ref = ipr.resolve_filter("referralNeedType", referral)
    pay = ipr.resolve_filter("paymentType", payment)
    svcs = ipr.resolve_services(prof, services)
    rows = ipr.slots(prof["code"], [m["id"] for m in munis], ref, pay, svcs)

    agg: dict[tuple, dict] = {}  # doctor x clinic x funding x referral
    for r in rows:
        k = (r["doctor"], r["clinic"], r["funding"], r["referral"])
        a = agg.setdefault(k, {**r, "services": set(), "free_times": 0})
        a["services"].add(r["service"])
        a["free_times"] += r["free_times"]
        a["earliest"] = min(a["earliest"], r["earliest"])
    entries = sorted(agg.values(), key=lambda a: a["earliest"])
    for e in entries:
        e["services"] = sorted(e["services"])

    review_data: dict[str, dict] = {}
    if reviews:
        hint = hint or specialty_hint(prof["name"])
        by_doc: dict[str, list[str]] = {}
        for e in entries:
            by_doc.setdefault(e["doctor"], []).append(e["clinic"])
        for i, (doc, clinics) in enumerate(by_doc.items(), 1):
            print(f"[{i}/{len(by_doc)}] reviews: {doc}", file=sys.stderr)
            review_data[doc] = reviews.doctor(doc, clinics, hint)

    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    filt = [f'referral: {ref["label"] if ref else "any"}', f'payment: {pay["label"] if pay else "any"}']
    if svcs:
        filt.append("services: " + "; ".join(s["name"] for s in svcs))
    lines = [f"# {prof['name']} ({prof['code']}) — {', '.join(m['name'] for m in munis)}", "",
             f"IPR snapshot {now} (Europe/Vilnius). Filters — {' | '.join(filt)}. "
             f"{len(entries)} doctor × clinic rows from {len(rows)} slot groups.", ""]
    if not entries:
        lines.append("No open slots matched these filters.\n")

    # With vs without a siuntimas. Labels come from IPR itself: a slot whose
    # referral label is the NOT_REQUIRED option can be booked without one.
    ref_opts = {o["member"]: o["label"] for o in ipr.filter_options("referralNeedType")}
    pay_order = [o["label"] for o in ipr.filter_options("paymentType") if o["value"]]
    fundings = sorted({e["funding"] for e in entries},
                      key=lambda f: pay_order.index(f) if f in pay_order else len(pay_order))
    no_ref_label = ref_opts.get("NOT_REQUIRED")
    if entries and no_ref_label:
        def first(rs):
            return min((e["earliest"] for e in rs), default="—")
        groups = [(f'With a siuntimas ({ref_opts.get("REQUIRED", "referral")})', entries),
                  (f"Without a siuntimas ({no_ref_label})", [e for e in entries if e["referral"] == no_ref_label])]
        lines += ["## With or without a siuntimas (referral)", "",
                  "| | Rows | " + " | ".join(f"Earliest: {f}" for f in fundings) + " |",
                  "|---|---|" + "---|" * len(fundings)]
        for label, rs in groups:
            lines.append(f"| {label} | {len(rs)} | " +
                         " | ".join(first([e for e in rs if e["funding"] == f]) for f in fundings) + " |")
        lines += ["", "Referral flags are what each clinic set in IPR. A clinic can still ask for a "
                  "siuntimas at a state-funded visit, and higher care-level (III lygis) services usually "
                  "expect one; confirm before relying on a no-referral slot.", ""]

    for f in fundings:
        part = [e for e in entries if e["funding"] == f]
        lines += [f"## {f}", ""]
        hdr, sep = "| Earliest | Doctor | Clinic | Referral | Services | Free times |", "|---|---|---|---|---|---|"
        if reviews:
            hdr += " Profile tags | pincetas.lt | manodaktaras.lt |"
            sep += "---|---|---|"
        lines += [hdr, sep]
        for e in part:
            row = (f'| {e["earliest"]} | {e["doctor"]} | {e["clinic"]} | {e["referral"]} | '
                   f'{"; ".join(_short_service(s) for s in e["services"])} | {e["free_times"]} |')
            if reviews:
                srcs = {s["source"]: s for s in review_data[e["doctor"]]["sources"]}
                tags = sorted({t for s in srcs.values() for t in s.get("specialties") or []})
                row += (f' {", ".join(tags) or "—"} | {_fmt_src(srcs.get("pincetas.lt", {}))} | '
                        f'{_fmt_src(srcs.get("manodaktaras.lt", {}))} |')
            lines.append(row)
        lines.append("")
    if reviews:
        lines += [
            "Columns: Profile tags = specialties the review sites list (helps narrow to e.g. moles / skin "
            f"cancer, aesthetics, children); site % = the site's own all-time figure; weighted = recency "
            f"(half-life {reviews.half_life:g}y) × trust weighted share of positive reviews; eff. n = sum of "
            "weights (below ~5 is too little to judge); flagged = reviews posted in a burst, near-duplicates, "
            "very short, or shouting — read them before trusting the number; clinic not on profile = the "
            "review profile doesn't list this clinic (namesake or out of date). manodaktaras.lt only accepts "
            "reviews from its own bookings and is run by a booking company paid by clinics; its scores are "
            "almost always ~100% and don't separate good from bad doctors.", ""]
    data = {"generated": now, "profession": prof, "municipalities": munis,
            "filters": {"referral": ref, "payment": pay, "services": svcs},
            "rows": entries, "reviews": review_data}
    return "\n".join(lines), data


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="ignore cached discovery results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config")
    p = sub.add_parser("professions"); p.add_argument("--q", help="substring, e.g. derma")
    p = sub.add_parser("municipalities"); p.add_argument("--q", help="substring, e.g. viln")
    p = sub.add_parser("services"); p.add_argument("--profession", required=True)
    sub.add_parser("filters")

    def slot_args(p):
        p.add_argument("--profession", required=True, help="code (e.g. 221210) or unique name substring")
        p.add_argument("--municipality", action="append", required=True,
                       help="name or id from `municipalities`; repeatable")
        p.add_argument("--referral", help="option from `filters` (member, label or value), "
                                          "e.g. REQUIRED / NOT_REQUIRED; default: all")
        p.add_argument("--payment", help="option from `filters`, e.g. VLK (state-funded) / Patient; default: all")
        p.add_argument("--service", action="append",
                       help="service id or name substring from `services`; repeatable")

    p = sub.add_parser("slots"); slot_args(p)
    p.add_argument("--no-details", action="store_true", help="clinic level only, no doctor names")

    def review_args(p):
        p.add_argument("--half-life", type=float, default=3.0, help="years; review weight halves every N years")
        p.add_argument("--recent-months", type=int, default=24)
        p.add_argument("--specialty-hint", help="prefer review profiles with this specialty (default: "
                                                "derived from the profession name)")

    p = sub.add_parser("reviews")
    p.add_argument("--name", required=True, help="doctor's full name, e.g. 'Jūratė Garbaravičienė'")
    p.add_argument("--clinic", action="append", help="clinic name(s), to tell namesakes apart")
    p.add_argument("--texts", action="store_true", help="full review texts instead of 160-char excerpts")
    review_args(p)

    p = sub.add_parser("report"); slot_args(p); review_args(p)
    p.add_argument("--no-reviews", action="store_true")
    p.add_argument("--json-out", help="write full data (incl. review texts and flags) to this file")

    a = ap.parse_args(argv)
    h = Http()
    if a.cmd == "config":
        cfg = {"ipr": ipr_config(h, a.refresh), "pincetas": pincetas_config(h, a.refresh),
               "manodaktaras": manodaktaras_config(h, a.refresh), "cache": str(CACHE)}
        print(json.dumps(cfg, ensure_ascii=False, indent=1))
        return
    if a.cmd == "reviews":
        ipr = Ipr(h, a.refresh)
        rv = Reviews(h, a.refresh, ipr.generic_clinic_words(), a.half_life, a.recent_months)
        r = rv.doctor(a.name, a.clinic, a.specialty_hint)
        if not a.texts:
            for s in r["sources"]:
                for x in s.get("reviews", []):
                    x["text"] = x["text"][:160] + ("…" if len(x["text"]) > 160 else "")
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    ipr = Ipr(h, a.refresh)
    if a.cmd == "professions":
        for p in ipr.professions():
            if not a.q or fold(a.q) in fold(p["name"]):
                print(f'{p["code"]}\t{p["name"]}')
    elif a.cmd == "municipalities":
        for m in ipr.municipalities():
            if not a.q or fold(a.q) in fold(m["name"]):
                print(f'{m["id"]}\t{m["name"]}')
    elif a.cmd == "services":
        for s in ipr.services(ipr.resolve_profession(a.profession)["id"]):
            print(f'{s["id"]}\t{s["name"]}')
    elif a.cmd == "filters":
        for param, flag in (("referralNeedType", "--referral"), ("paymentType", "--payment")):
            print(f"{flag}:")
            for o in ipr.filter_options(param):
                print(f'  {o["member"]}\t{o["value"]}\t{o["label"]}')
    elif a.cmd == "slots":
        prof = ipr.resolve_profession(a.profession)
        rows = ipr.slots(prof["code"], [m["id"] for m in ipr.resolve_municipalities(a.municipality)],
                         ipr.resolve_filter("referralNeedType", a.referral),
                         ipr.resolve_filter("paymentType", a.payment),
                         ipr.resolve_services(prof, a.service), details=not a.no_details)
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif a.cmd == "report":
        rv = None if a.no_reviews else Reviews(h, a.refresh, ipr.generic_clinic_words(),
                                               a.half_life, a.recent_months)
        md, data = build_report(ipr, rv, a.profession, a.municipality, a.referral, a.payment,
                                a.service, a.specialty_hint)
        print(md)
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(f"Full data: {a.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
