#!/usr/bin/env python3
"""Find open doctor appointments anywhere in Lithuania through the national
e-health booking system (IPR, ipr.esveikata.lt) and cross-check the doctors
against patient reviews.

Standard library only. Subcommands:

  config          what was discovered from the live sites (paths, filters)
  professions     professions and their codes (filter with --q)
  municipalities  municipalities (filter with --q)
  services        sub-services of a profession: age group, sub-specialty, care level
  filters         every search filter IPR's own search form offers, with options
  slots           open slots, one row per doctor x clinic x service x funding
  times           actual free times for one doctor at one clinic
  reviews         one doctor's profiles on pincetas.lt and manodaktaras.lt
  report          slots + free times + reviews for every doctor found (Markdown, JSON)

Leave out --municipality to search the whole country.

Nothing about the sites is assumed if it can be read from them: API paths and
filters (keys, codes, labels) come from IPR's web-app bundle, review-site
endpoints from their home pages, clinic addresses and generic clinic-name words
from IPR's institution list. Discovered values are cached in
~/.cache/lt-doctor-appointment (--refresh re-reads them); built-in values are
used only as a fallback, with a warning, when discovery fails.

Review analysis weighs each review by age (half-life, default 3 years) and by
how much it can be trusted (anonymous, one-off accounts, bursts, duplicates,
very short or shouting text). Reviewer IPs or identities are never output.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import http.client
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

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Vilnius")
except Exception:  # no tz database (e.g. Windows without the tzdata package)
    TZ = None  # use the machine's local time zone

UA = "lt-doctor-appointment-skill/0.3 (+https://github.com/aleksas/lt-doctor-appointment)"
IPR_ORIGIN = "https://ipr.esveikata.lt"
PINCETAS = "https://www.pincetas.lt"
MANODAKTARAS = "https://www.manodaktaras.lt"
DELAY = 0.35  # seconds between requests to the same host
CONNECT_TIMEOUT = 10
RETRIES = 5
PAGE = 50
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
            "publicDates": "/appointments/dates/public",
            "publicAppointmentsPath": "/appointments/public",
        },
        "filters": {
            "paymentType": {"label": "Apmokėjimas:", "options": [
                {"member": "Patient", "value": 2, "label": "Pacientas"},
                {"member": "VLK", "value": 1, "label": "Ligonių kasos"}]},
            "referralNeedType": {"label": "Siuntimas:", "options": [
                {"member": "REQUIRED", "value": 1, "label": "Su siuntimu"},
                {"member": "NOT_REQUIRED", "value": 2, "label": "Be siuntimo"}]},
            "appointmentMethodId": {"label": "Vizito būdas:", "options": [
                {"member": "INSTITUTION", "value": 1, "label": "Įstaigoje"},
                {"member": "REMOTE_APPOINTMENT_VIDEO", "value": 3, "label": "Nuotolinis: vaizdo skambutis"},
                {"member": "REMOTE_APPOINTMENT_PHONE", "value": 4, "label": "Nuotolinis: telefonas"}]},
        },
    },
    "pincetas": {"search_url": f"{PINCETAS}/search", "fields": ["criteria", "city", "skill", "rating"]},
    "manodaktaras": {"suggest_path": "/suggestions", "doctor_prefix": "/gydytojas"},
}
# Names we look up in IPR's bundle: semantic anchors that change far less often
# than the values they point to.
IPR_PATH_KEYS = list(FALLBACK["ipr"]["paths"])
IPR_REQUIRED_FILTERS = ["referralNeedType", "paymentType"]
IPR_PARAMS = ["municipalityId", "professionCode", "healthcareServiceId", "referralNeedType", "paymentType",
              "appointmentMethodId", "organizationId", "fundType", "leftBound", "rightBound",
              "practitionerId", "serviceId", "date", "page", "size"]
# CLI shortcuts for the filters IPR's search form offers (any other one: --filter key=value)
FILTER_FLAGS = {"referral": "referralNeedType", "payment": "paymentType", "method": "appointmentMethodId"}


FETCH_HINT = ("They may be stale: check them against the live site (see 'When the script can't discover "
              "something' in SKILL.md) before relying on the results.")


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP


class Http:
    """Polite HTTPS client: one kept-alive connection per host (a report makes
    hundreds of requests, and opening a new connection for each one gets
    throttled by IPR's firewall), a pause between requests to the same host,
    cookies, redirects, retries with backoff. Raises urllib.error types."""

    def __init__(self) -> None:
        self.conns: dict[str, http.client.HTTPSConnection] = {}
        self.cookies: dict[str, dict[str, str]] = {}
        self.last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        gap = time.monotonic() - self.last.get(host, 0)
        if gap < DELAY:
            time.sleep(DELAY - gap)
        self.last[host] = time.monotonic()

    def _conn(self, host: str, timeout: float) -> http.client.HTTPSConnection:
        c = self.conns.get(host)
        if c is None:
            c = self.conns[host] = http.client.HTTPSConnection(host, timeout=CONNECT_TIMEOUT)
        if c.sock is None:
            # IPR sometimes drops connection attempts; fail fast and retry
            # rather than waiting out a long timeout.
            c.timeout = CONNECT_TIMEOUT
            c.connect()
        c.sock.settimeout(timeout)
        return c

    def _drop(self, host: str) -> None:
        c = self.conns.pop(host, None)
        if c:
            c.close()

    def _once(self, method: str, url: str, body: bytes | None, headers: dict, timeout: float):
        u = urllib.parse.urlsplit(url)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        jar = self.cookies.setdefault(u.netloc, {})
        hdrs = {"Host": u.netloc, "Connection": "keep-alive", **headers}
        if jar:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        if body is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        self._wait(u.netloc)
        c = self._conn(u.netloc, timeout)
        try:
            c.request(method, path, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read()
        except (http.client.HTTPException, OSError):
            self._drop(u.netloc)  # stale keep-alive connection or network error
            raise
        for sc in r.headers.get_all("Set-Cookie") or []:
            k, _, rest = sc.partition("=")
            jar[k.strip()] = rest.split(";", 1)[0]
        if r.getheader("Connection", "").lower() == "close":
            self._drop(u.netloc)
        return r.status, r.headers, data

    def get(self, url: str, params: dict | None = None, data: dict | None = None,
            headers: dict | None = None, timeout: float = 30) -> str:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        hdrs = {"User-Agent": UA, "Accept-Language": "lt,en;q=0.8", **(headers or {})}
        method = "POST" if body is not None else "GET"
        for attempt in range(RETRIES):
            last = attempt == RETRIES - 1
            try:
                for _ in range(6):  # follow redirects
                    status, rh, payload = self._once(method, url, body, hdrs, timeout)
                    if status in (301, 302, 303, 307, 308) and rh.get("Location"):
                        url = urllib.parse.urljoin(url, rh["Location"])
                        if status in (301, 302, 303):
                            method, body = "GET", None
                        continue
                    break
                if status in (429, 500, 502, 503, 504) and not last:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, f"HTTP {status}", rh, None)
                return payload.decode("utf-8", errors="replace")
            except (http.client.HTTPException, OSError) as e:
                if isinstance(e, urllib.error.HTTPError):
                    raise
                if not last:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise urllib.error.URLError(e) from e
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
    t = re.sub(r"[ \t\r\f\v]+", " ", html.unescape(t))
    return re.sub(r"\s*\n\s*", "\n", t).strip()


def ms_to_local(ms: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime(fmt)


def day_bound_ms(day: str, end: bool = False) -> int:
    d = dt.date.fromisoformat(day)
    t = dt.datetime.combine(d, dt.time.max if end else dt.time.min, TZ)
    return int(t.timestamp() * 1000)


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


def _enum_values(js: str, var: str) -> dict[str, int]:
    """Members of a TypeScript enum as compiled into the bundle. Two shapes occur:
    var X=(()=>(function(n){n[n.A=0]="A",...}(X||(X={})),X))();
    var X=(()=>{return(n=X||(X={}))[n.A=-1]="A",n[n.B=1]="B",X;var n})();"""
    start = js.find(f"var {var}=(()=>")
    if start < 0:
        return {}
    end = js.find("();", start)
    return {m: int(v) for m, v in re.findall(r"n\[n\.(\w+)=(-?\d+)\]", js[start:end])}


def parse_ipr_bundle(js: str) -> dict:
    """Read API paths and every search-form filter (key, label, options with
    codes and labels) from IPR's Angular bundle. Raises ValueError when
    something essential is missing."""
    api = re.search(r'apiUrl:"([^"]*)"', js)
    if not api:
        raise ValueError("apiUrl not found")
    found: dict[str, str] = {}
    for k, v in re.findall(r'(\w+):"(/[^"]*)"', js):
        found.setdefault(k, v)
    missing = [k for k in IPR_PATH_KEYS if k not in found]
    if missing:
        raise ValueError(f"paths not found: {missing}")
    filters: dict[str, dict] = {}
    for label, key, opts in re.findall(r'\{label:"([^"]*)",key:"(\w+)",options:\[(.*?)\]', js):
        if key in filters:
            continue
        options = []
        for name, var, member in re.findall(r'\{name:"([^"]*)",value:(\w+)\.(\w+)\}', opts):
            values = _enum_values(js, var)
            if member in values:
                options.append({"member": member, "value": values[member], "label": js_string(name)})
        if options:
            filters[key] = {"label": js_string(label), "options": options}
    absent = [k for k in IPR_REQUIRED_FILTERS if k not in filters]
    if absent:
        raise ValueError(f"search-form filters not found: {absent}")
    gone = [p for p in IPR_PARAMS if f'"{p}"' not in js]
    if gone:
        warn(f"IPR bundle no longer mentions request params {gone}; results may be off")
    return {"apiUrl": api.group(1), "paths": {k: found[k] for k in IPR_PATH_KEYS}, "filters": filters}


def ipr_config(h: Http, refresh: bool = False) -> dict:
    try:
        index = h.get(IPR_ORIGIN + "/")
        bundle = re.search(r'src="(main\.[0-9a-f]+\.js)"', index)
        if not bundle:
            raise ValueError("main bundle not referenced from index.html")
        key = f"ipr-v3-{bundle.group(1)}.json"
        if not refresh and (cached := cache_read(key)):
            return cached
        cfg = parse_ipr_bundle(h.get(f"{IPR_ORIGIN}/{bundle.group(1)}", timeout=90))
        cfg["bundle"] = bundle.group(1)
        cache_write(key, cfg)
        return cfg
    except (ValueError, urllib.error.URLError, TimeoutError) as e:
        warn(f"IPR discovery failed ({e}); using last-known values. {FETCH_HINT}")
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
    except (ValueError, urllib.error.URLError, TimeoutError) as e:
        warn(f"pincetas.lt discovery failed ({e}); using last-known values. {FETCH_HINT}")
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
    except (ValueError, urllib.error.URLError, TimeoutError) as e:
        warn(f"manodaktaras.lt discovery failed ({e}); using last-known values. {FETCH_HINT}")
        return FALLBACK["manodaktaras"]


# ---------------------------------------------------------------- IPR


class Ipr:
    def __init__(self, h: Http, refresh: bool = False) -> None:
        self.h, self.refresh = h, refresh
        self.cfg = ipr_config(h, refresh)
        self._institutions: dict[int, dict] | None = None

    def url(self, key: str) -> str:
        return IPR_ORIGIN + self.cfg["apiUrl"] + self.cfg["paths"][key]

    def _data(self, key: str, **params) -> list[dict]:
        d = self.h.json(self.url(key), params=params or None)
        return d.get("data", d) if isinstance(d, dict) else d

    def _paged(self, key: str, params: dict) -> list[dict]:
        """IPR's meta.totalPages is always 0, and asking for the page after the
        last one can hang, so stop at the first short page."""
        rows, page = [], 0
        while page < 200:
            d = self.h.json(self.url(key), params={**params, "page": page, "size": PAGE})
            data = d.get("data") or []
            rows += data
            if len(data) < PAGE:
                break
            page += 1
        return rows

    # -- reference data

    def professions(self) -> list[dict]:
        return self._data("professionsSearchPath")

    def municipalities(self) -> list[dict]:
        return [{"id": m["admId"], "name": m["pavadinimas"]} for m in self._data("municipalitiesSearchPath")]

    def services(self, profession_id: int) -> list[dict]:
        return [{"id": s["id"], "name": s["name"]}
                for s in self._data("healthcareServicesSearchFilterPath", professionId=profession_id)]

    def institutions(self) -> dict[int, dict]:
        """istgId -> name, municipality id, city and street address (from nameExt)."""
        if self._institutions is not None:
            return self._institutions
        rows = None if self.refresh else cache_read("institutions.json", CACHE_TTL_DAYS)
        if rows is None:
            rows = []
            for i in self._data("institutionsSearchPath"):
                name = (i.get("istgPavadinimas") or "").strip()
                ext = (i.get("nameExt") or "").strip()
                rest = ext[len(name):].strip(" ,") if ext.startswith(name) else ""
                city, _, street = rest.partition(", ")
                rows.append({"id": i["istgId"], "name": name, "municipality_id": i.get("municipalityId"),
                             "city": city or None, "address": street or None})
            cache_write("institutions.json", rows)
        self._institutions = {r["id"]: r for r in rows}
        return self._institutions

    def generic_clinic_words(self) -> set[str]:
        """Words so common in IPR institution names (UAB, VšĮ, poliklinika, chain
        names, ...) that they say nothing about which clinic is meant."""
        names = [r["name"] for r in self.institutions().values()]
        df: dict[str, int] = {}
        for n in names:
            for t in name_tokens(n):
                df[t] = df.get(t, 0) + 1
        cut = max(5, len(names) // 100)  # in >1% of institution names
        return {t for t, c in df.items() if c >= cut}

    # -- filters

    def filters(self) -> dict[str, dict]:
        return self.cfg["filters"]

    def resolve_filter(self, param: str, q: str | None) -> dict | None:
        """Match user input against an option's member name, label or value."""
        if q is None:
            return None
        if param not in self.filters():
            raise SystemExit(f"IPR's search form has no filter '{param}'; see `filters`.")
        opts = self.filters()[param]["options"]
        fq = fold(q).replace("-", "_").replace(" ", "_")
        for o in opts:
            if fq in (fold(o["member"]), fold(o["label"]).replace(" ", "_"), str(o["value"])):
                return {**o, "param": param}
        hits = [o for o in opts if fold(q) in fold(o["label"])]
        if len(hits) == 1:
            return {**hits[0], "param": param}
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

    def resolve_municipalities(self, qs: list[str] | None) -> list[dict]:
        """[] means the whole country."""
        if not qs:
            return []
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

    # -- slots and times

    def _clinic(self, org_id: int, fallback_name: str | None) -> dict:
        inst = self.institutions().get(org_id) or {}
        return {"clinic": (fallback_name or inst.get("name") or "").strip(), "clinic_id": org_id,
                "city": inst.get("city"), "address": inst.get("address"),
                "municipality_id": inst.get("municipality_id")}

    def slots(self, profession_code: str, municipality_ids: list[int], filters: list[dict] | None = None,
              services: list[dict] | None = None, date_from: str | None = None, date_to: str | None = None,
              details: bool = True) -> list[dict]:
        """One row per doctor x clinic x service x funding, with the earliest free time.
        No municipality ids = the whole country."""
        base_q: dict = {"professionCode": profession_code}
        for f in filters or []:
            base_q[f["param"]] = f["value"]
        if date_from:
            base_q["leftBound"] = day_bound_ms(date_from)
        if date_to:
            base_q["rightBound"] = day_bound_ms(date_to, end=True)
        rows = []
        for mid in municipality_ids or [None]:
            for svc in services or [None]:
                q = dict(base_q)
                if mid:
                    q["municipalityId"] = mid
                if svc:
                    q["healthcareServiceId"] = svc["id"]
                for g in self._paged("appointmentTimeGroupsSearchPath", q):
                    base = {
                        **self._clinic(g["organizationId"], g.get("organizationName")),
                        "service": g["healthcareServiceName"], "service_id": g["healthcareServiceId"],
                        "funding": g["fundType"]["name"], "funding_code": g["fundType"]["type"],
                        "referral": g["referralNeed"]["name"], "referral_code": g["referralNeed"]["type"],
                        "method": g.get("appointmentMethod"), "method_id": g.get("appointmentMethodId"),
                    }
                    if not details:
                        rows.append({**base, "doctor": None, "earliest": ms_to_local(g["earliestTime"]),
                                     "free_times": g["periodTimesCount"]})
                        continue
                    dq = {"organizationId": g["organizationId"], "healthcareServiceId": g["healthcareServiceId"],
                          "fundType": g["fundType"]["type"], "referralNeedType": g["referralNeed"]["type"],
                          "professionCode": profession_code, "appointmentMethodId": g["appointmentMethodId"]}
                    for k in ("leftBound", "rightBound"):
                        if k in base_q:
                            dq[k] = base_q[k]
                    for s in self._paged("appointmentsGroupTimesSearchPath", dq):
                        first = (s.get("specialistFirstName") or "").strip().title()
                        last = (s.get("specialistLastName") or "").strip().title()
                        rows.append({**base, "doctor": f"{first} {last}".strip(),
                                     "specialist_id": s.get("specialistId"),
                                     "earliest": ms_to_local(s["earliestTime"]),
                                     "free_times": s["periodTimesCount"],
                                     "room": s.get("patalposPavadinimas") or s.get("patalposNumeris")})
        rows.sort(key=lambda r: r["earliest"])
        return rows

    def times(self, specialist_id: int, service_id: int, organization_id: int, days: int = 1,
              date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """Actual free times on the first `days` dates that have any, with
        everything IPR says about each slot."""
        left = day_bound_ms(date_from) if date_from else int(time.time() * 1000)
        right = day_bound_ms(date_to, end=True) if date_to else left + 366 * 86400 * 1000
        base = {"practitionerId": specialist_id, "serviceId": service_id, "organizationId": organization_id}
        dates = self.h.json(self.url("publicDates"), params={**base, "leftBound": left, "rightBound": right})
        out, seen = [], set()
        for d in sorted(x["appointmentDate"] for x in dates or [])[:days]:
            for a in self._paged("publicAppointmentsPath", {**base, "date": d}):
                key = (a.get("id"), a.get("date"))
                if key in seen:
                    continue
                seen.add(key)
                sp = a.get("specialist") or {}
                inst = sp.get("institution") or {}
                gc = (a.get("greenCorridor") or {}).get("name")
                out.append({
                    "time": ms_to_local(a["date"]),
                    "visit_type": a.get("type"),
                    "comment": a.get("comment"),
                    "funding": (a.get("fundType") or {}).get("name"),
                    "referral": (a.get("referralNeed") or {}).get("name"),
                    "green_corridor": gc,
                    "method": a.get("appointmentMethod"),
                    "services": [s.get("name") for s in sp.get("healthcareServices") or []],
                    "room": " ".join(x for x in (sp.get("roomNumber"), sp.get("roomName")) if x) or None,
                    "registration_address": sp.get("organizationBranchAddress"),
                    "phone": sp.get("phoneNo") or inst.get("phoneNo"),
                    "anonymous_allowed": a.get("allowedAnonymous"),
                })
        return out


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


def parse_pincetas_profile(s: str, url: str = "") -> dict:
    g = lambda p: (m.group(1).strip() if (m := re.search(p, s, re.S)) else None)  # noqa: E731
    recommend = g(r'itemprop="ratingValue">\s*([\d.,]+)\s*%')
    ratings = g(r'itemprop="ratingCount">\s*(\d+)')
    licences = []
    for name, info in re.findall(r'<p class="name">\s*(.*?)\s*</p>\s*<p class="info">(.*?)</p>', s, re.S):
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", info)
        status = re.search(r'class="status"[^>]*>(.*?)</span>', info, re.S)
        licences.append({"title": text_of(name), "issued": dates[0] if dates else None,
                         "status": text_of(status.group(1)) if status else None,
                         "data_updated": dates[-1] if len(dates) > 1 else None,
                         "text": text_of(info)})
    workplaces = []
    for block in re.findall(r'doctors-at-hospital">(.*?)</div>\s*</div>\s*</div>', s, re.S):
        name = re.search(r'itemprop="hospitalAffiliation"[^>]*>(.*?)</div>', block, re.S)
        stats = re.search(r'glyphicon-comment"[^>]*></span>\s*(\d+).*?glyphicon-heart"></span>\s*(\d+)%', block, re.S)
        if name:
            workplaces.append({"name": re.sub(r"^\d+\.\s*", "", text_of(name.group(1))),
                               "reviews": int(stats.group(1)) if stats else None,
                               "recommend_pct": int(stats.group(2)) if stats else None})
    detailed = []
    for block in s.split('class="rating-progress')[1:]:  # one block per question; unrated ones have no badge
        q = re.search(r"<p>(.*?)</p>", block, re.S)
        v = re.search(r'class="progress-badge"[^>]*>\s*([\d.,]+)\s*<', block)
        c = re.search(r'class="compare">(.*?)</(?:p|div)>', block, re.S)
        if q:
            detailed.append({"question": text_of(q.group(1)),
                             "score": float(v.group(1).replace(",", ".")) if v else None,
                             "best_answer" if v else "note": text_of(c.group(1)) if c else None})
    chart = re.search(r"new Chart\(.*?labels:\s*\[(.*?)\].*?data:\s*\[(.*?)\]", s, re.S)
    trend = {}
    if chart:
        trend = dict(zip(re.findall(r'"(\d{4})"', chart.group(1)), map(int, re.findall(r"(\d+)", chart.group(2)))))
    reviews = []
    for cls, info, body in re.findall(
            r'<div\s+class="comment clearfix([^"]*)">\s*<div\s+class="info">(.*?)</div>\s*'
            r'<div\s+class="message"[^>]*>(.*?)</div>', s, re.S):
        classes = cls.split()
        date = re.search(r"(\d{4}-\d{2}-\d{2})", info)
        count = re.search(r'glyphicon-comment"[^>]*></span>\s*(\d+)', info)
        reviews.append({
            "source": "pincetas.lt",
            "date": date.group(1) if date else None,
            "sentiment": "positive" if "positive" in classes else "negative" if "negative" in classes else "neutral",
            "author": "registered" if "loggedin" in classes else "anonymous",
            "author_review_count": int(count.group(1)) if count else None,
            "text": text_of(body),
        })  # deliberately no IP / author identity
    return {"source": "pincetas.lt", "url": url,
            "site_recommend_pct": float(recommend.replace(",", ".")) if recommend else None,
            "site_ratings": int(ratings) if ratings else None,
            "licences": licences,
            "workplaces": [w["name"] for w in workplaces],
            "workplace_stats": workplaces,
            "detailed_ratings": detailed,
            "cumulative_recommend_pct_by_year": trend,
            "reviews": reviews}


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
                out.append({"url": m.group(1), "name": html.unescape(m.group(2)).strip(), "specialties": labels})
        return out

    def query_for(self, name: str) -> str:
        return " ".join(reversed(name.split()))  # the site lists 'Surname Name'

    def profile(self, url: str) -> dict:
        return parse_pincetas_profile(self.h.get(url), url)


# ---------------------------------------------------------------- manodaktaras.lt


def parse_manodaktaras_profile(s: str, url: str = "") -> dict:
    m = re.search(r"<strong>(\d+)%</strong>\s*\((\d+)\s*\w+", s)
    reviews = []
    for block in s.split("data-comment-item")[1:]:
        meta = re.search(r'<div class="meta">(.*?)</div>', block, re.S)
        body = re.search(r"<p>(.*?)</p>", block, re.S)
        if not meta or not body:
            continue
        mt = fold(text_of(meta.group(1)))
        date = re.search(r"(\d{4}-\d{2}-\d{2})", mt)
        reviews.append({   # the site's own wording: 'Rekomenduoja' / 'Nerekomenduoja'
            "source": "manodaktaras.lt",
            "date": date.group(1) if date else None,
            "sentiment": "negative" if "nerekomend" in mt else "positive" if "rekomend" in mt else "neutral",
            "author": "verified visit", "author_review_count": None,
            "text": text_of(body.group(1)),
        })
    # structured data: clinics, prices, free times
    nodes = []
    for ld in re.findall(r'application/ld\+json">(.*?)</script>', s, re.S):
        try:
            d = json.loads(ld)
        except json.JSONDecodeError:
            continue
        nodes += d.get("@graph", [d]) if isinstance(d, dict) else []
    clinics = {n["@id"]: n.get("name") for n in nodes if n.get("@type") in ("MedicalClinic", "MedicalOrganization")}
    person = next((n for n in nodes if n.get("@type") == "Person"), {})
    prices, free = [], []
    for o in person.get("makesOffer") or []:
        clinic = clinics.get((o.get("availableAtOrFrom") or {}).get("@id"))
        if o.get("itemOffered"):
            prices.append({"service": o["itemOffered"].get("name"), "price": o.get("price"),
                           "currency": o.get("priceCurrency"), "clinic": clinic})
        elif o.get("availabilityStarts"):
            desc = o.get("description") or ""
            free.append({"date": o["availabilityStarts"][:10], "clinic": clinic,
                         "times": re.findall(r"\b\d{1,2}:\d{2}\b", desc.split("):")[-1].split(".")[0])})
    # "about" block: a heading, a list of what the doctor treats, then "• Label:" sections
    about = {"focus": [], "sections": {}}
    for heading, body in re.findall(r"<h4>([^<]*)</h4>(.*?)</div>", s, re.S):
        if "<strong>" not in body:
            continue
        head, *rest = re.split(r"<strong>\s*•?\s*", body)
        about["heading"] = text_of(heading)
        about["focus"] = [x for x in text_of(head).split("\n") if x]
        for part in rest:
            label, _, items = part.partition("</strong>")
            items = re.split(r"<(?:a|button|div|form)\b", items)[0]  # stop before buttons/links after the list
            about["sections"][text_of(label).rstrip(":")] = [x for x in text_of(items).split("\n") if x]
        break
    return {"source": "manodaktaras.lt", "url": url,
            "site_recommend_pct": float(m.group(1)) if m else None,
            "site_ratings": int(m.group(2)) if m else None,
            "workplaces": sorted({c for c in clinics.values() if c}),
            "about": about, "prices": prices, "free_times": free,
            "reviews": reviews}


class ManoDaktaras:
    source = "manodaktaras.lt"

    def __init__(self, h: Http, refresh: bool = False) -> None:
        self.h = h
        self.cfg = manodaktaras_config(h, refresh)

    def search(self, query: str) -> list[dict]:
        d = self.h.json(MANODAKTARAS + self.cfg["suggest_path"], params={"query": query},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        # the site redirects /<prefix>/<anything>/<anything>/<id> to the canonical page
        return [{"name": x["name"], "specialties": x.get("specialties", []), "cities": x.get("cities", []),
                 "url": f'{MANODAKTARAS}{self.cfg["doctor_prefix"]}/-/-/{x["id"]}'}
                for x in d.get("suggestions", []) if x.get("entity_type") == "doctor"]

    def query_for(self, name: str) -> str:
        return name

    def profile(self, url: str) -> dict:
        return parse_manodaktaras_profile(self.h.get(url), url)


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
    norm = [(r, re.sub(r"\W+", " ", fold(r["text"])).strip()) for r in reviews if not r["text_hidden_by_site"]]
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
                    ws = set().union(set(), *(name_tokens(w) for w in pair[1]["workplaces"]))
                    return stems_overlap(clinic_words, ws)
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
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
                out["sources"].append({"source": site.source, "found": False, "error": f"{type(e).__name__}: {e}"})
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


def _cell(s: object) -> str:
    return str(s if s not in (None, "") else "—").replace("|", "/").replace("\n", " ")


def _doctor_details(doc: str, rows: list[dict], rv: dict | None) -> list[str]:
    out = [f"### {doc}", ""]
    for e in rows:
        where = ", ".join(x for x in (e["clinic"], e.get("city"), e.get("address")) if x)
        out.append(f'- **IPR — {where}** ({e["funding"]}, {e["referral"]}, {e.get("method") or "—"})')
        if e.get("times"):
            by_day: dict[str, list[dict]] = {}
            for t in e["times"]:
                by_day.setdefault(t["time"][:10], []).append(t)
            for day, ts in by_day.items():
                kinds = sorted({" / ".join(x for x in (t["visit_type"], t["comment"]) if x) for t in ts} - {""})
                extra = [f"green corridor: {g}" for g in sorted({t["green_corridor"] for t in ts if t["green_corridor"]})]
                t0 = ts[0]
                extra += [x for x in (t0.get("room") and f'room {t0["room"]}',
                                      t0.get("registration_address"),
                                      t0.get("phone") and f'tel. {t0["phone"]}') if x]
                out.append(f'  - {day}: {", ".join(t["time"][11:] for t in ts)}'
                           + (f' — {"; ".join(kinds)}' if kinds else "")
                           + (f' ({"; ".join(extra)})' if extra else ""))
        elif e.get("times_error"):
            out.append(f'  - free times not available: {e["times_error"]}')
        elif "times" in e:
            out.append("  - IPR's public calendar returned no day-by-day list for this one (some clinics "
                       "publish times only inside the booking flow); check on ipr.esveikata.lt")
    srcs = {s["source"]: s for s in (rv or {}).get("sources", []) if s.get("found")}
    p = srcs.get("pincetas.lt")
    if p:
        lic = [f'{l["title"]}, issued {l["issued"]}' + (f' ({l["status"]})' if l.get("status") else "")
               for l in p.get("licences", []) if l.get("issued")]
        if lic:
            out.append(f'- **Licence** (pincetas.lt): {"; ".join(lic)}')
        rated = [d for d in p.get("detailed_ratings", []) if d.get("score") is not None]
        if rated:
            out.append("- **Detailed scores** (pincetas.lt, 1–5, all-time): " +
                       "; ".join(f'{d["question"]} {d["score"]:g}' for d in rated))
        ws = [f'{w["name"]} {w["recommend_pct"]}% of {w["reviews"]}' for w in p.get("workplace_stats", [])
              if w.get("recommend_pct") is not None]
        if ws:
            out.append(f'- **Per workplace** (pincetas.lt): {"; ".join(ws)}')
        out.append(f'- pincetas.lt: {p["url"]}')
    m = srcs.get("manodaktaras.lt")
    if m:
        ab = m.get("about") or {}
        if ab.get("focus"):
            out.append(f'- **Treats** (manodaktaras.lt): {"; ".join(ab["focus"])}')
        for label, items in (ab.get("sections") or {}).items():
            out.append(f'- **{label}** (manodaktaras.lt): {"; ".join(items)}')
        if m.get("prices"):
            out.append("- **Prices** (manodaktaras.lt): " + "; ".join(
                f'{x["service"]} {x["price"]} {x["currency"] or ""} @ {x["clinic"] or "—"}'.replace("  ", " ")
                for x in m["prices"]))
        if m.get("free_times"):
            out.append("- **Free times on manodaktaras.lt** (may include paid slots not in IPR): " + "; ".join(
                f'{x["date"]} {", ".join(x["times"])} @ {x["clinic"] or "—"}' for x in m["free_times"][:4]))
        out.append(f'- manodaktaras.lt: {m["url"]}')
    out.append("")
    return out


def build_report(ipr: Ipr, reviews: Reviews | None, profession: str, municipalities: list[str] | None,
                 filter_args: dict[str, str], services: list[str] | None, hint: str | None,
                 date_from: str | None = None, date_to: str | None = None, times_days: int = 1) -> tuple[str, dict]:
    prof = ipr.resolve_profession(profession)
    munis = ipr.resolve_municipalities(municipalities)
    filters = [ipr.resolve_filter(k, v) for k, v in filter_args.items()]
    svcs = ipr.resolve_services(prof, services)
    rows = ipr.slots(prof["code"], [m["id"] for m in munis], filters, svcs, date_from, date_to)

    agg: dict[tuple, dict] = {}  # doctor x clinic x funding x referral x method
    for r in rows:
        k = (r["doctor"], r["clinic_id"], r["funding"], r["referral"], r.get("method"))
        a = agg.setdefault(k, {**r, "services": {}, "free_times": 0})
        a["services"][r["service_id"]] = r["service"]
        a["free_times"] += r["free_times"]
        if r["earliest"] < a["earliest"]:
            a.update(earliest=r["earliest"], service_id=r["service_id"])
    entries = sorted(agg.values(), key=lambda a: a["earliest"])

    if times_days > 0:
        for i, e in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] free times: {e['doctor']} @ {e['clinic']}", file=sys.stderr)
            try:
                e["times"] = ipr.times(e["specialist_id"], e["service_id"], e["clinic_id"],
                                       times_days, date_from, date_to)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as ex:
                e["times"], e["times_error"] = [], f"{type(ex).__name__}"
    for e in entries:
        e["services"] = sorted(e["services"].values())

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
    where = ", ".join(m["name"] for m in munis) or "all of Lithuania"
    filt = [f'{ipr.filters()[f["param"]]["label"].rstrip(":")} {f["label"]}' for f in filters]
    if svcs:
        filt.append("services: " + "; ".join(s["name"] for s in svcs))
    if date_from or date_to:
        filt.append(f'dates {date_from or "…"} – {date_to or "…"}')
    lines = [f"# {prof['name']} ({prof['code']}) — {where}", "",
             f"IPR snapshot {now} ({'Europe/Vilnius' if TZ else 'local time'}). "
             f"Filters: {' | '.join(filt) or 'none'}. {len(entries)} doctor × clinic rows from {len(rows)} slot groups.", ""]
    if not entries:
        lines.append("No open slots matched these filters.\n")

    # With vs without a siuntimas. Labels come from IPR's own referral filter.
    ref_opts = {o["member"]: o["label"] for o in ipr.filters().get("referralNeedType", {}).get("options", [])}
    pay_order = [o["label"] for o in ipr.filters().get("paymentType", {}).get("options", [])]
    fundings = sorted({e["funding"] for e in entries}, key=lambda f: (f not in pay_order, -pay_order.index(f) if f in pay_order else 0))
    no_ref = ref_opts.get("NOT_REQUIRED")
    if entries and no_ref:
        def first(rs):
            return min((e["earliest"] for e in rs), default="—")
        groups = [(f'With a siuntimas ({ref_opts.get("REQUIRED", "referral")})', entries),
                  (f"Without a siuntimas ({no_ref})", [e for e in entries if e["referral"] == no_ref])]
        lines += ["## With or without a siuntimas (referral)", "",
                  "| | Rows | " + " | ".join(f"Earliest: {f}" for f in fundings) + " |",
                  "|---|---|" + "---|" * len(fundings)]
        for label, rs in groups:
            lines.append(f"| {label} | {len(rs)} | " +
                         " | ".join(first([e for e in rs if e["funding"] == f]) for f in fundings) + " |")
        lines += ["", "Referral flags are what each clinic set in IPR. A clinic can still ask for a siuntimas at a "
                  "state-funded visit, and higher care-level (III lygis) services usually expect one; confirm "
                  "before relying on a no-referral slot.", ""]

    for f in fundings:
        part = [e for e in entries if e["funding"] == f]
        lines += [f"## {f}", ""]
        hdr = "| Earliest | Doctor | Clinic | City | Visit | Referral | Services | Free times |"
        sep = "|---|---|---|---|---|---|---|---|"
        if reviews:
            hdr += " Profile tags | pincetas.lt | manodaktaras.lt |"
            sep += "---|---|---|"
        lines += [hdr, sep]
        for e in part:
            row = "| " + " | ".join(_cell(x) for x in (
                e["earliest"], e["doctor"], e["clinic"], e.get("city"), e.get("method"), e["referral"],
                "; ".join(_short_service(s) for s in e["services"]), e["free_times"])) + " |"
            if reviews:
                srcs = {s["source"]: s for s in review_data[e["doctor"]]["sources"]}
                tags = sorted({t for s in srcs.values() for t in s.get("specialties") or []})
                row += f' {_cell(", ".join(tags))} | {_cell(_fmt_src(srcs.get("pincetas.lt", {})))} | ' \
                       f'{_cell(_fmt_src(srcs.get("manodaktaras.lt", {})))} |'
            lines.append(row)
        lines.append("")
    if reviews:
        lines += [
            "Columns: Profile tags = specialties the review sites list; site % = the site's own all-time figure; "
            f"weighted = recency (half-life {reviews.half_life:g}y) × trust weighted share of positive reviews; "
            "eff. n = sum of weights (below ~5 is too little to judge); flagged = reviews posted in a burst, "
            "near-duplicates, very short, or shouting — read them before trusting the number; clinic not on profile "
            "= the review profile doesn't list this clinic (namesake or out of date). manodaktaras.lt only accepts "
            "reviews from its own bookings and is run by a booking company paid by clinics; its scores are almost "
            "always ~100% and don't separate good from bad doctors.", ""]
    if entries and (times_days > 0 or reviews):
        lines += ["## Doctor details", ""]
        by_doc_rows: dict[str, list[dict]] = {}
        for e in entries:
            by_doc_rows.setdefault(e["doctor"], []).append(e)
        for doc, rs in by_doc_rows.items():
            lines += _doctor_details(doc, rs, review_data.get(doc))
    data = {"generated": now, "profession": prof, "municipalities": munis or "all",
            "filters": filters, "services": svcs, "dates": [date_from, date_to],
            "rows": entries, "reviews": review_data}
    return "\n".join(lines), data


# ---------------------------------------------------------------- CLI


def _filter_args(a: argparse.Namespace) -> dict[str, str]:
    out = {param: getattr(a, flag) for flag, param in FILTER_FLAGS.items() if getattr(a, flag, None)}
    for kv in getattr(a, "filter", None) or []:
        k, _, v = kv.partition("=")
        if not v:
            raise SystemExit(f"--filter expects key=value, got '{kv}'")
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="ignore cached discovery results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config")
    p = sub.add_parser("professions"); p.add_argument("--q", help="substring, e.g. derma")
    p = sub.add_parser("municipalities"); p.add_argument("--q", help="substring, e.g. kaun")
    p = sub.add_parser("services"); p.add_argument("--profession", required=True)
    sub.add_parser("filters")

    def slot_args(p):
        p.add_argument("--profession", required=True, help="code (e.g. 221210) or unique name substring")
        p.add_argument("--municipality", action="append",
                       help="name or id from `municipalities`; repeatable; omit for all of Lithuania")
        p.add_argument("--referral", help="option of IPR's referral filter (see `filters`), e.g. NOT_REQUIRED")
        p.add_argument("--payment", help="option of IPR's payment filter, e.g. VLK (state-funded) or Patient")
        p.add_argument("--method", help="option of IPR's visit-method filter, e.g. INSTITUTION or a remote one")
        p.add_argument("--filter", action="append", metavar="KEY=VALUE",
                       help="any other filter listed by `filters`; repeatable")
        p.add_argument("--service", action="append", help="service id or name substring from `services`; repeatable")
        p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="earliest date")
        p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="latest date")

    p = sub.add_parser("slots"); slot_args(p)
    p.add_argument("--no-details", action="store_true", help="clinic level only, no doctor names")

    p = sub.add_parser("times")
    p.add_argument("--specialist-id", type=int, required=True)
    p.add_argument("--service-id", type=int, required=True)
    p.add_argument("--clinic-id", type=int, required=True)
    p.add_argument("--days", type=int, default=3, help="how many dates with free times to list")
    p.add_argument("--from", dest="date_from"); p.add_argument("--to", dest="date_to")

    def review_args(p):
        p.add_argument("--half-life", type=float, default=3.0, help="years; review weight halves every N years")
        p.add_argument("--recent-months", type=int, default=24)
        p.add_argument("--specialty-hint", help="prefer review profiles with this specialty "
                                                "(default: derived from the profession name)")

    p = sub.add_parser("reviews")
    p.add_argument("--name", required=True, help="doctor's full name, e.g. 'Vardenis Pavardenis'")
    p.add_argument("--clinic", action="append", help="clinic name(s), to tell namesakes apart")
    p.add_argument("--texts", action="store_true", help="full review texts instead of 160-char excerpts")
    review_args(p)

    p = sub.add_parser("report"); slot_args(p); review_args(p)
    p.add_argument("--no-reviews", action="store_true")
    p.add_argument("--times-days", type=int, default=1,
                   help="list actual free times for the first N dates per doctor (0 = skip)")
    p.add_argument("--json-out", help="write full data (incl. review texts and flags) to this file")

    a = ap.parse_args(argv)
    h = Http()
    if a.cmd == "config":
        cfg = {"ipr": ipr_config(h, a.refresh), "pincetas": pincetas_config(h, a.refresh),
               "manodaktaras": manodaktaras_config(h, a.refresh), "cache": str(CACHE)}
        print(json.dumps(cfg, ensure_ascii=False, indent=1))
        return
    ipr = Ipr(h, a.refresh)
    if a.cmd == "reviews":
        rv = Reviews(h, a.refresh, ipr.generic_clinic_words(), a.half_life, a.recent_months)
        r = rv.doctor(a.name, a.clinic, a.specialty_hint)
        if not a.texts:
            for s in r["sources"]:
                for x in s.get("reviews", []):
                    x["text"] = x["text"][:160] + ("…" if len(x["text"]) > 160 else "")
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif a.cmd == "professions":
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
        flags = {v: f"--{k}" for k, v in FILTER_FLAGS.items()}
        for key, f in ipr.filters().items():
            print(f'{flags.get(key, f"--filter {key}=…")}  ({f["label"].rstrip(":")})')
            for o in f["options"]:
                print(f'  {o["member"]}\t{o["value"]}\t{o["label"]}')
    elif a.cmd == "slots":
        prof = ipr.resolve_profession(a.profession)
        filters = [ipr.resolve_filter(k, v) for k, v in _filter_args(a).items()]
        rows = ipr.slots(prof["code"], [m["id"] for m in ipr.resolve_municipalities(a.municipality)],
                         filters, ipr.resolve_services(prof, a.service), a.date_from, a.date_to,
                         details=not a.no_details)
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif a.cmd == "times":
        print(json.dumps(ipr.times(a.specialist_id, a.service_id, a.clinic_id, a.days, a.date_from, a.date_to),
                         ensure_ascii=False, indent=1))
    elif a.cmd == "report":
        rv = None if a.no_reviews else Reviews(h, a.refresh, ipr.generic_clinic_words(), a.half_life, a.recent_months)
        md, data = build_report(ipr, rv, a.profession, a.municipality, _filter_args(a), a.service,
                                a.specialty_hint, a.date_from, a.date_to, a.times_days)
        print(md)
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(f"Full data: {a.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
