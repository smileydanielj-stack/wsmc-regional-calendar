#!/usr/bin/env python3
"""WSMC Regional Calendar aggregator.

Pulls upcoming events from:
  1. WA Dept. of Ecology NACES (Northwest Area Committee Exercise Schedule) - drills & exercises
  2. Marine Exchange of Puget Sound "WA Maritime" public Google Calendar (iCal feed)
  3. Marine Exchange committee pages (Harbor Safety Committee, Marine Firefighting Commission) - meeting dates
  4. wsmc_events.json - hand-maintained WSMC events

Writes events.json (for the website), calendar.ics (subscribable feed) and, if the
page file is present, refreshes the snapshot embedded in wsmc-regional-calendar.html.

Standard library only:  python3 build_calendar.py
"""
import re, html, json, sys, os, hashlib, difflib, shutil, subprocess, tempfile, datetime as dt
import urllib.request, urllib.parse, http.cookiejar
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
UA = "WSMC-Regional-Calendar/1.0 (+https://www.wsmcoop.org)"
UA_FALLBACK = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

NACES_URL = "https://apps.ecology.wa.gov/naces/"
MAREX_CAL_ID = "0gs007g192c4fjifogqngpgdio@group.calendar.google.com"
MAREX_ICS = "https://calendar.google.com/calendar/ical/%s/public/basic.ics" % urllib.parse.quote(MAREX_CAL_ID)
MAREX_PAGE = "https://marexps.com/wa-maritime-calendar/"
PSHSC_PAGE = "https://marexps.com/puget-sound-harbor-safety-committee/"
PSMFC_PAGE = "https://marexps.com/puget-sound-marine-firefighting-commission/"

PAST_DAYS, FUTURE_DAYS = 7, 500
BS = chr(92)   # a single backslash
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")

CATEGORIES = {
    "wsmc":       "WSMC",
    "drill":      "Drills & Exercises",
    "regulatory": "Committees & Agencies",
    "port":       "Port Meetings",
    "industry":   "Industry & Community",
}

def log(*a): print(*a, file=sys.stderr)
def clean(s): return re.sub(r"\s+", " ", s or "").strip()
def scrub(s): return clean(PHONE.sub("", s or "")).strip(" ,;-")
def iso(d): return d.isoformat() if isinstance(d, dt.date) and not isinstance(d, dt.datetime) else d.astimezone(PT).isoformat(timespec="minutes")
def eid(*parts): return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

# ----------------------------------------------------------------- NACES
def _hidden(page):
    return {m.group(1): html.unescape(m.group(2)) for m in
            re.finditer(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)}

def _cell(x): return clean(html.unescape(re.sub(r"<[^>]+>", " ", x)))

def _naces_rows(page):
    g = re.search(r'<table[^>]*id="ctl00_ContentPlaceHolder1_GridView1".*?</table>', page, flags=re.S)
    rows = []
    if g:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", g.group(0), flags=re.S):
            c = [_cell(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
            if len(c) >= 7: rows.append(c)
    return rows

def _naces_dt(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]m)", s.strip(), flags=re.I)
    if not m: return None
    mo, d, y, h, mi, ap = m.groups(); h = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    return dt.datetime(int(y), int(mo), int(d), h, int(mi), tzinfo=PT)

def fetch_naces(ua=UA):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)); op.addheaders = [("User-Agent", ua)]
    def post(page, extra):
        f = _hidden(page); f.setdefault("ctl00$ContentPlaceHolder1$txtDateSelect", ""); f.update(extra)
        req = urllib.request.Request(NACES_URL, data=urllib.parse.urlencode(f).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": NACES_URL})
        return op.open(req, timeout=45).read().decode("utf-8", "replace")
    p0 = op.open(NACES_URL, timeout=45).read().decode("utf-8", "replace")
    p1 = post(p0, {"__EVENTTARGET": "ctl00$ContentPlaceHolder1$mnuCalendarView", "__EVENTARGUMENT": "List View"})
    rows = _naces_rows(p1)
    if not rows: raise RuntimeError("NACES list view returned no rows (page layout may have changed)")
    nxt = dt.date.today().year + 1      # list view shows one calendar year; ask for the next one too
    p2 = post(p1, {"__EVENTTARGET": "", "__EVENTARGUMENT": "",
                   "ctl00$ContentPlaceHolder1$txtDateSelect": "01/15/%d" % nxt,
                   "ctl00$ContentPlaceHolder1$btnDateSelect": "Go"})
    rows += [r for r in _naces_rows(p2) if r not in rows]
    out = []
    for c in rows:
        name, when, sponsor, dtype, loc, _coordinator, comments = c[:7]   # coordinator name/phone deliberately dropped
        parts = when.split(" - ")
        start = _naces_dt(parts[0]); end = _naces_dt(parts[1]) if len(parts) > 1 else None
        if not start: continue
        title = clean(name) or clean(sponsor)
        out.append({"id": "naces-" + eid(start.isoformat(), title), "title": title, "start": iso(start),
                    "end": iso(end) if end else None, "allDay": False, "category": "drill",
                    "subtype": clean(dtype), "location": scrub(loc), "notes": scrub(comments),
                    "source": "naces", "sourceLabel": "WA Ecology NACES", "url": NACES_URL})
    return out

# ----------------------------------------------------------------- Marine Exchange iCal
def _ics_text(v):
    return clean(re.sub(r"<[^>]+>", " ", html.unescape(
        v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace(BS + ";", ";").replace("\\\\", "\\"))))

def _ics_dt(line_key, value):
    if "VALUE=DATE" in line_key or re.fullmatch(r"\d{8}", value):
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    m = re.match(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)(Z?)", value)
    if not m: return None, False
    y, mo, d, h, mi, s, z = m.groups()
    tz = UTC if z else PT
    t = re.search(r"TZID=([^:;]+)", line_key)
    if t and not z:
        try: tz = ZoneInfo(t.group(1))
        except Exception: tz = PT
    return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=tz), False

def categorize_marex(title):
    t = title.lower()
    if re.search(r"pilotage|area committee|ecology|wdfw|fish & wildlife|coast guard|uscg|harbor safety|regional response|legislat|rulemaking|public hearing", t):
        return "drill" if re.search(r"\bdrill\b|exercise|deployment", t) else "regulatory"
    if re.search(r"^port of |seaport alliance|commission meeting|managing members", t): return "port"
    return "industry"

def fetch_marex():
    req = urllib.request.Request(MAREX_ICS, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    raw = re.sub(r"\r?\n[ \t]", "", raw)                       # unfold continuation lines
    out = []
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, flags=re.S):
        f = {}
        for line in blk.strip().splitlines():
            if ":" not in line: continue
            k, v = line.split(":", 1); f[k.split(";")[0]] = (k, v)
        if "DTSTART" not in f or f.get("STATUS", ("", ""))[1] == "CANCELLED": continue
        start, allday = _ics_dt(*f["DTSTART"])
        if start is None: continue
        end = _ics_dt(*f["DTEND"])[0] if "DTEND" in f else None
        if allday and end: end = end - dt.timedelta(days=1)   # iCal all-day DTEND is exclusive
        title = _ics_text(f.get("SUMMARY", ("", ""))[1])
        desc = _ics_text(f.get("DESCRIPTION", ("", ""))[1])
        url = (re.search(r"https?://[^\s<>\"']+", desc) or [None])[0]
        notes = scrub(re.sub(r"https?://\S+", "", desc))
        notes = re.sub(r"\s*(More info|Details|Info|Register|Agenda)\s*:?\s*$", "", notes, flags=re.I).strip()[:400]
        out.append({"id": "marex-" + eid(f.get("UID", ("", title))[1]), "title": title, "start": iso(start),
                    "end": iso(end) if end else None, "allDay": allday, "category": categorize_marex(title),
                    "subtype": "", "location": scrub(_ics_text(f.get("LOCATION", ("", ""))[1])),
                    "notes": notes, "source": "marex", "sourceLabel": "Marine Exchange of Puget Sound",
                    "url": (url or MAREX_PAGE).rstrip(".,)")})
    return out

# ----------------------------------------------------------------- Marine Exchange committee pages
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}

def _page_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA_FALLBACK})
    return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")

def _text(raw):
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", raw)))

def _page_text(url): return _text(_page_html(url))

def _pshsc_archive_dates(page_html, year):
    """The committee's yearly 'Meetings Archive' PDF lists every meeting date for the year, including ones
    not yet announced on the page. Needs the pdftotext tool (poppler); quietly returns [] without it."""
    m = re.search(r'href="([^"]+%d-PSHSC-Meetings-Archive[^"]*[.]pdf)"' % year, page_html)
    if not m or not shutil.which("pdftotext"): return []
    try:
        req = urllib.request.Request(html.unescape(m.group(1)), headers={"User-Agent": UA_FALLBACK})
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(urllib.request.urlopen(req, timeout=45).read()); f.flush()
            txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    out = []
    for mon, day in re.findall(r"(?m)^[ \t]*([A-Z][a-z]+)[ \t]+(\d{1,2})[ \t]*$", txt):
        if mon.lower() in MONTHS:
            try: out.append(dt.date(year, MONTHS[mon.lower()], int(day)))
            except ValueError: pass
    return out

def _committee_event(title, start, end, location, notes, url):
    return {"id": "marexc-" + eid(title, start.isoformat()), "title": title, "start": iso(start),
            "end": iso(end) if end else None, "allDay": False, "category": "regulatory", "subtype": "",
            "location": location, "notes": notes, "source": "marex-committee",
            "sourceLabel": "Marine Exchange of Puget Sound", "url": url}

def fetch_committees():
    """Meeting dates published on two Marine Exchange committee pages. Join links are not republished;
    each event links back to the committee page, which always carries the current link."""
    out = []
    # --- Puget Sound Marine Firefighting Commission: a list of M/D/YYYY dates plus one sentence giving the time
    t = _page_text(PSMFC_PAGE)
    tm = re.search(r"All meetings[^.]*?\bat\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", t, flags=re.I)
    hour, minute = (int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "p" else 0), int(tm.group(2) or 0)) if tm else (11, 0)
    online = bool(re.search(r"All meetings[^.]*Microsoft Teams", t, flags=re.I))
    a = t.find("MEETINGS & MINUTES"); b = t.find("Recent Reports", a if a >= 0 else 0)
    seg = t[a:b if b > a else len(t)] if a >= 0 else ""
    seen = set()
    for mo, d, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", seg):
        try: start = dt.datetime(int(y), int(mo), int(d), hour, minute, tzinfo=PT)
        except ValueError: continue
        if start in seen: continue
        seen.add(start)
        out.append(_committee_event("Puget Sound Marine Firefighting Commission \u2014 Meeting", start, None,
                                    "Online (Microsoft Teams)" if online else "",
                                    "Meeting of the Puget Sound Marine Firefighting Commission. The join link and past minutes are on the commission page.",
                                    PSMFC_PAGE))
    # --- Puget Sound Harbor Safety Committee: a single "Upcoming Meeting" block
    raw = _page_html(PSHSC_PAGE); t = _text(raw)
    m = re.search(r"Upcoming Meeting\s+Date:\s*([A-Za-z]+)\s+(\d{1,2})\s*,\s*(\d{4})(?:\s+from\s+(\d{2})(\d{2})\s*-\s*(\d{2})(\d{2}))?(.*?)(?:Join Here|MEETING ARCHIVE)", t, flags=re.S)
    if m and m.group(1).lower() in MONTHS:
        y, mo, d = int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))
        sh, sm_, eh, em = (int(x) for x in m.group(4, 5, 6, 7)) if m.group(4) else (10, 0, 12, 0)
        start = dt.datetime(y, mo, d, sh, sm_, tzinfo=PT); end = dt.datetime(y, mo, d, eh, em, tzinfo=PT)
        rest = m.group(8)
        loc = re.search(r"Location:\s*(.+)", rest)
        extra = re.search(r"\(([^)]*networking[^)]*)\)", rest, flags=re.I)
        notes = "Quarterly meeting of the Puget Sound Harbor Safety Committee. Open to the public."
        if extra: notes += " " + extra.group(1).strip().capitalize() + "."
        out.append(_committee_event("Puget Sound Harbor Safety Committee \u2014 Quarterly Meeting", start, end,
                                    scrub(loc.group(1)) if loc else "", notes + " The join link and agenda are on the committee page.", PSHSC_PAGE))
    detailed = {dt.date.fromisoformat(e["start"][:10]) for e in out if "Harbor Safety" in e["title"]}
    today = dt.date.today()
    for d in _pshsc_archive_dates(raw, today.year):
        if d >= today and d not in detailed:
            ev = _committee_event("Puget Sound Harbor Safety Committee \u2014 Quarterly Meeting", dt.datetime(d.year, d.month, d.day, tzinfo=PT), None, "",
                                  "Date listed in the committee's %d meeting archive. Time and location are posted on the committee page closer to the meeting. Open to the public." % d.year,
                                  PSHSC_PAGE)
            ev.update({"start": d.isoformat(), "end": d.isoformat(), "allDay": True})
            out.append(ev)
    return out

# ----------------------------------------------------------------- merge
def _norm(t): return re.sub(r"[^a-z0-9 ]", "", re.sub(r"^wa ecology( drill)?:\s*", "", t.lower()))
def _day(ev): return dt.date.fromisoformat(ev["start"][:10])

def merge(naces, marex, wsmc, committees=()):
    by_day = {}
    for n in naces: by_day.setdefault(_day(n), []).append(_norm(n["title"]))
    kept, dropped = [], 0
    cdays = {(_day(c), 'harbor safety' if 'Harbor Safety' in c['title'] else 'firefight') for c in committees}
    for m in marex:
        if any((_day(m), k) in cdays for k in ('harbor safety', 'firefight') if k in m['title'].lower()):
            dropped += 1; continue                                       # committee page is authoritative
        if re.match(r"wa ecology drill", m["title"], flags=re.I):      # NACES is authoritative for drills
            dropped += 1; continue
        if re.match(r"wa ecology", m["title"], flags=re.I):
            near = [t for off in (-1, 0, 1) for t in by_day.get(_day(m) + dt.timedelta(days=off), [])]
            if any(difflib.SequenceMatcher(None, _norm(m["title"]), t).ratio() > 0.6 for t in near):
                dropped += 1; continue
        kept.append(m)
    events = naces + kept + list(committees) + wsmc
    for e in events:
        if re.search(r"\bwsmc\b|washington state maritime coop", e["title"], flags=re.I): e["category"] = "wsmc"
    today = dt.date.today()
    lo, hi = today - dt.timedelta(days=PAST_DAYS), today + dt.timedelta(days=FUTURE_DAYS)
    events = [e for e in events if lo <= dt.date.fromisoformat((e["end"] or e["start"])[:10]) and _day(e) <= hi]
    events.sort(key=lambda e: (e["start"][:10], e["allDay"] is False, e["start"], e["title"]))
    return events, dropped

def load_wsmc():
    p = os.path.join(HERE, "wsmc_events.json")
    if not os.path.exists(p): return []
    out = []
    for w in json.load(open(p, encoding="utf-8")):
        out.append({"id": "wsmc-" + eid(w["start"], w["title"]), "title": w["title"], "start": w["start"],
                    "end": w.get("end"), "allDay": w.get("allDay", True), "category": "wsmc", "subtype": "",
                    "location": w.get("location", ""), "notes": w.get("notes", ""), "source": "wsmc",
                    "sourceLabel": "WSMC", "url": w.get("url", "https://www.wsmcoop.org")})
    return out

# ----------------------------------------------------------------- outputs
def write_ics(events, path):
    def esc(s): return (s or "").replace("\\", "\\\\").replace(";", BS + ";").replace(",", "\\,").replace("\n", "\\n")
    def fold(line):
        b, out = line.encode("utf-8"), []
        while len(b) > 73:
            cut = 73
            while cut > 0 and (b[cut] & 0xC0) == 0x80: cut -= 1
            out.append(b[:cut].decode("utf-8")); b = b" " + b[cut:]
        out.append(b.decode("utf-8")); return "\r\n".join(out)
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WSMC//Regional Calendar//EN", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", "X-WR-CALNAME:WSMC Regional Maritime & Response Calendar", "X-WR-TIMEZONE:America/Los_Angeles"]
    for e in events:
        L += ["BEGIN:VEVENT", "UID:%s@wsmcoop.org" % e["id"], "DTSTAMP:" + stamp]
        if e["allDay"]:
            s = dt.date.fromisoformat(e["start"][:10]); en = dt.date.fromisoformat((e["end"] or e["start"])[:10]) + dt.timedelta(days=1)
            L += ["DTSTART;VALUE=DATE:" + s.strftime("%Y%m%d"), "DTEND;VALUE=DATE:" + en.strftime("%Y%m%d")]
        else:
            s = dt.datetime.fromisoformat(e["start"]).astimezone(UTC); L.append("DTSTART:" + s.strftime("%Y%m%dT%H%M%SZ"))
            if e["end"]: L.append("DTEND:" + dt.datetime.fromisoformat(e["end"]).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        desc = " ".join(x for x in [e.get("subtype"), e.get("notes"), "Source: %s %s" % (e["sourceLabel"], e["url"])] if x)
        L += [fold("SUMMARY:" + esc(e["title"])), fold("DESCRIPTION:" + esc(desc)), fold("CATEGORIES:" + esc(CATEGORIES[e["category"]]))]
        if e.get("location"): L.append(fold("LOCATION:" + esc(e["location"])))
        L += [fold("URL:" + e["url"]), "END:VEVENT"]
    L.append("END:VCALENDAR")
    open(path, "w", encoding="utf-8", newline="").write("\r\n".join(L) + "\r\n")

def embed_snapshot(payload, page):
    if not os.path.exists(page): return False
    s = open(page, encoding="utf-8").read()
    a, b = "/*WSMC-CAL-DATA-START*/", "/*WSMC-CAL-DATA-END*/"
    if a not in s or b not in s: return False
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    s = s[:s.index(a) + len(a)] + "\nwindow.WSMC_CAL_SNAPSHOT=" + blob + ";\n" + s[s.index(b):]
    open(page, "w", encoding="utf-8").write(s); return True

def main():
    status, naces, marex, committees = {}, [], [], []
    for name, fn in (("naces", fetch_naces), ("marex", fetch_marex), ("committees", fetch_committees)):
        try:
            got = fn(); status[name] = {"ok": True, "count": len(got)}
        except Exception as ex:
            got = []
            if name == "naces":
                try: got = fetch_naces(UA_FALLBACK); status[name] = {"ok": True, "count": len(got), "note": "browser UA"}
                except Exception as ex2: status[name] = {"ok": False, "error": str(ex2)[:200]}
            else: status[name] = {"ok": False, "error": str(ex)[:200]}
        if name == "naces": naces = got
        elif name == "marex": marex = got
        else: committees = got
    if not any(v.get("ok") for v in status.values()):
        log("All sources failed; leaving existing outputs untouched:", status); return 1
    # If one source is down, keep its events from the previous build instead of publishing a gap.
    prev_path = os.path.join(HERE, "events.json")
    if os.path.exists(prev_path) and not all(v.get("ok") for v in status.values()):
        try: previous = json.load(open(prev_path, encoding="utf-8")).get("events", [])
        except Exception: previous = []
        keys = {"naces": "naces", "marex": "marex", "committees": "marex-committee"}
        for name, src in keys.items():
            if not status[name].get("ok"):
                kept = [e for e in previous if e.get("source") == src]
                status[name].update({"stale": True, "count": len(kept)})
                if name == "naces": naces = kept
                elif name == "marex": marex = kept
                else: committees = kept
    events, dropped = merge(naces, marex, load_wsmc(), committees)
    payload = {"generated": dt.datetime.now(UTC).isoformat(timespec="seconds"), "timezone": "America/Los_Angeles",
               "categories": CATEGORIES, "sources": status, "events": events}
    json.dump(payload, open(os.path.join(HERE, "events.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_ics(events, os.path.join(HERE, "calendar.ics"))
    emb = embed_snapshot(payload, os.path.join(HERE, "wsmc-regional-calendar.html"))
    log("sources:", status); log("events: %d (dropped %d duplicate Marine Exchange entries) | snapshot embedded: %s" % (len(events), dropped, emb))
    return 0

if __name__ == "__main__":
    sys.exit(main())
