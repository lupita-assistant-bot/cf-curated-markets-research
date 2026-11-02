#!/usr/bin/env python3
"""Fetch pages AND run search queries from a GitHub Actions runner (fresh IP).

Jobs file format (tab separated):
    url     <id>   <url>
    search  <id>   <query>

Outputs:
    results2/raw/<id>.txt      raw body (or search result listing)
    results2/text/<id>.txt     cleaned text
    results2/parsed/<id>.json  parsed JSON-LD events (url jobs only)
    results2/digest.md          compact keyword/date digest of everything
    results2/manifest.json      per-job status
"""
import concurrent.futures as cf
import gzip
import html as htmlmod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "results2")
RAW = os.path.join(BASE, "raw")
TXT = os.path.join(BASE, "text")
JSN = os.path.join(BASE, "parsed")
for d in (RAW, TXT, JSN):
    os.makedirs(d, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Proxy chains: each entry is (label, url_template) where {u} is the encoded target.
PROXIES = [
    ("direct", "{u}"),
    ("allorigins", "https://api.allorigins.win/raw?url={u}"),
    ("codetabs", "https://api.codetabs.com/v1/proxy?quest={u}"),
    ("corsproxy", "https://corsproxy.io/?{u}"),
    ("jina", "https://r.jina.ai/{u}"),
    ("jina-http", "https://r.jina.ai/{u}"),
]

SEARCH_ENGINES = [
    ("ddg_html", "https://html.duckduckgo.com/html/?q={q}"),
    ("ddg_lite", "https://lite.duckduckgo.com/lite/?q={q}"),
    ("bing", "https://www.bing.com/search?q={q}&count=30"),
    ("mojeek", "https://www.mojeek.com/search?q={q}"),
    ("marginalia", "https://search.marginalia.nu/search?query={q}"),
    ("startpage", "https://www.startpage.com/sp/search?query={q}"),
    ("brave", "https://search.brave.com/search?q={q}"),
    ("searx_be", "https://searx.be/search?q={q}&format=json"),
    ("ecosia", "https://www.ecosia.org/search?q={q}"),
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=40, extra_headers=None):
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return r.status, r.geturl(), raw.decode("utf-8", "replace")


BLOCKERS = ("captcha", "are you a robot", "unusual traffic", "access denied",
            "enable javascript and cookies", "cf-browser-verification",
            "checking your browser", "request unsuccessful", "attention required!",
            "unusual activity", "are you a human", "security check",
            "to discuss automated access", "rate limit", "too many requests",
            "captcha-delivery", "px-captcha")


def looks_blocked(t):
    low = t[:12000].lower()
    for b in BLOCKERS:
        if b in low:
            return True
    return False


def try_chain(target, is_search=False):
    """Try proxy chain; return (body, note, attempts[])"""
    attempts = []
    for label, tmpl in PROXIES:
        if label == "direct":
            u = target
        elif label == "jina-http":
            u = tmpl.format(u=urllib.parse.quote(target.replace("https://", "http://"), safe=""))
        else:
            u = tmpl.format(u=urllib.parse.quote(target, safe=""))
        try:
            st, fin, body = http_get(u)
            attempts.append("%s:%s:%d" % (label, st, len(body)))
            if len(body) > 300 and not looks_blocked(body):
                return body, "%s:%s:%d" % (label, st, len(body)), attempts
        except Exception as e:
            attempts.append("%s-err:%s" % (label, type(e).__name__))
    return None, "FAILED", attempts


def try_search(query):
    """Return (engine, body, attempts)."""
    attempts = []
    for label, tmpl in SEARCH_ENGINES:
        u = tmpl.format(q=urllib.parse.quote(query))
        try:
            st, fin, body = http_get(u)
            attempts.append("%s:%s:%d" % (label, st, len(body)))
            if len(body) > 800 and not looks_blocked(body):
                return label, body, attempts
        except Exception as e:
            attempts.append("%s-err:%s" % (label, type(e).__name__))
    return None, None, attempts


TAG_RE = re.compile(r"<[^>]+>")
A_RE = re.compile(r'(?is)<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>')


def strip_html(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|td|span|a|td)>", "\n", h)
    h = TAG_RE.sub(" ", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n[ \t]*\n+", "\n", h)
    return h.strip()


def extract_links(body):
    """Extract (text, url) pairs of outbound links."""
    out = []
    seen = set()
    for m in A_RE.finditer(body):
        url = htmlmod.unescape(m.group(1)).strip()
        text = strip_html(m.group(2)).strip()
        if not url.startswith("http"):
            continue
        if any(x in url for x in ("duckduckgo.com", "bing.com", "mojeek.com", "google.",
                                  "startpage.com", "brave.com", "ecosia.org",
                                  "marginalia.nu", "searx.", "w3.org", "schema.org",
                                  "microsoft.com", "msn.com", "yandex.")):
            continue
        key = (text[:70], url[:180])
        if key in seen:
            continue
        seen.add(key)
        out.append((text[:160], url[:300]))
    return out


def jsonld_objects(txt):
    out = []
    for m in re.finditer(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', txt):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            out.append(json.loads(blob))
        except Exception:
            try:
                out.append(json.loads(re.sub(r"(?is)<[^>]+>", "", blob)))
            except Exception:
                pass
    return out


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


EVENT_TYPES = ("Event", "SocialEvent", "MusicEvent", "Festival", "ExhibitionEvent",
               "CraftEvent", "BusinessEvent", "LiteraryEvent", "FoodEvent",
               "Market", "SaleEvent", "ShoppingEvent", "Exhibition", "PublicationEvent")


def collect_events(objs):
    events, seen = [], set()
    for o in objs:
        for d in walk(o):
            typ = d.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if not any(t in EVENT_TYPES for t in types if t):
                continue
            name = d.get("name")
            if not name:
                continue
            key = (str(name)[:80], str(d.get("startDate", "")))
            if key in seen:
                continue
            seen.add(key)
            loc = d.get("location")
            addr, locname = {}, None
            if isinstance(loc, dict):
                locname = loc.get("name")
                a = loc.get("address")
                if isinstance(a, dict):
                    addr = a
                elif isinstance(a, list) and a and isinstance(a[0], dict):
                    addr = a[0]
            elif isinstance(loc, list) and loc and isinstance(loc[0], dict):
                locname = loc[0].get("name")
                a = loc[0].get("address")
                if isinstance(a, dict):
                    addr = a
            offers = d.get("offers")
            ourl = None
            if isinstance(offers, dict):
                ourl = offers.get("url")
            elif isinstance(offers, list):
                for o2 in offers:
                    if isinstance(o2, dict) and o2.get("url"):
                        ourl = o2.get("url")
                        break
            events.append({
                "name": name,
                "startDate": d.get("startDate"), "endDate": d.get("endDate"),
                "streetAddress": addr.get("streetAddress"),
                "addressLocality": addr.get("addressLocality"),
                "addressRegion": addr.get("addressRegion"),
                "postalCode": addr.get("postalCode"),
                "locationName": locname,
                "description": (str(d.get("description"))[:700] if d.get("description") else ""),
                "url": d.get("url") or ourl,
                "eventStatus": d.get("eventStatus"),
            })
    return events


KEYWORDS = re.compile(
    r"(market|vintage|antique|artisan|maker|handcraft|craft fair|art walk|"
    r"artwalk|flea|night market|pop-?up|thrift|shabby|treasure|swap meet|"
    r"maker faire|open studios|gallery hop|night market|boutique|bazaar|"
    r"vendor|street fair|art fest|festival)", re.I)

DATE_RE = re.compile(
    r"(Nov\w*\s+\d{1,2}|November\s+\d{1,2}|\d{1,2}\s+Nov\w*|"
    r"2026-11-\d{2}|Nov\.?\s*\d{1,2}|"
    r"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day)", re.I)

HOURS_RE = re.compile(
    r"(\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)[^.\n]{0,30}|"
    r"\d{1,2}\s*[-–]\s*\d{1,2}\s*(?:am|pm)|"
    r"(?:hours|open|daily|every (?:sunday|saturday|friday|thursday|monday))[^.\n]{0,60})", re.I)

ADDR_RE = re.compile(
    r"(\d{3,6}\s+[A-Z][A-Za-z0-9.'&\- ]{2,40}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|"
    r"Dr|Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Pkwy|Parkway|Cir|Circle|Hwy|Highway|Trl|Trail)[^\n]{0,60})")


def digest_text(tid, url, text, limit=45):
    """Extract candidate market-relevant lines."""
    lines = [l.strip() for l in text.split("\n")]
    hits, seen = [], set()
    for i, l in enumerate(lines):
        if len(l) < 8 or len(l) > 400:
            continue
        if KEYWORDS.search(l) or DATE_RE.search(l) or HOURS_RE.search(l):
            key = l.lower()[:90]
            if key in seen:
                continue
            seen.add(key)
            hits.append(l)
            if len(hits) >= limit:
                break
    out = ["### %s" % tid, "URL: %s" % url]
    for h in hits:
        out.append("- " + h)
    if not hits:
        out.append("- (no keyword/date lines matched)")
    return "\n".join(out)


def do_url(tid, url):
    rec = {"id": tid, "url": url, "type": "url"}
    body, note, attempts = try_chain(url)
    rec["attempts"] = attempts
    rec["note"] = note
    if body is None:
        rec["status"] = "FAILED"
        return rec, None
    rec["status"] = "OK"
    rec["bytes"] = len(body)
    with open(os.path.join(RAW, tid + ".html"), "w", encoding="utf-8") as fh:
        fh.write(body)
    clean = strip_html(body)
    with open(os.path.join(TXT, tid + ".txt"), "w", encoding="utf-8") as fh:
        fh.write(clean)
    objs = jsonld_objects(body)
    evs = collect_events(objs)
    with open(os.path.join(JSN, tid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": tid, "url": url, "jsonld_count": len(objs), "events": evs},
                  fh, indent=1, default=str)
    rec["jsonld_objects"] = len(objs)
    rec["events_found"] = len(evs)
    rec["text_len"] = len(clean)
    return rec, clean


def do_search(tid, query):
    rec = {"id": tid, "query": query, "type": "search"}
    engine, body, attempts = try_search(query)
    rec["attempts"] = attempts
    if body is None:
        rec["status"] = "FAILED"
        return rec, None
    rec["status"] = "OK"
    rec["engine"] = engine
    rec["bytes"] = len(body)
    with open(os.path.join(RAW, tid + ".html"), "w", encoding="utf-8") as fh:
        fh.write(body)
    links = extract_links(body)
    clean = strip_html(body)
    with open(os.path.join(TXT, tid + ".txt"), "w", encoding="utf-8") as fh:
        fh.write(clean)
    with open(os.path.join(JSN, tid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": tid, "query": query, "engine": engine, "links": links},
                  fh, indent=1, default=str)
    rec["links"] = len(links)
    return rec, clean


def main():
    jobfile = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "jobs.txt")
    jobs = []
    with open(jobfile, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            jobs.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))

    manifest = []
    digests = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = []
        for kind, tid, target in jobs:
            if kind == "url":
                futs.append(ex.submit(do_url, tid, target))
            else:
                futs.append(ex.submit(do_search, tid, target))
        for fut in cf.as_completed(futs):
            rec, clean = fut.result()
            manifest.append(rec)
            tag = rec.get("id")
            print("%-24s %-7s %-8s %s" % (tag, rec.get("status"),
                                          rec.get("bytes") or rec.get("links"),
                                          (rec.get("note") or rec.get("engine") or "")), flush=True)
            if clean:
                digests.append((tag, rec.get("url") or rec.get("query"), clean))

    order = {j[1]: i for i, j in enumerate(jobs)}
    manifest.sort(key=lambda r: order.get(r.get("id"), 9999))
    with open(os.path.join(BASE, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)

    digests.sort(key=lambda d: order.get(d[0], 9999))
    with open(os.path.join(BASE, "digest.md"), "w", encoding="utf-8") as fh:
        for tag, url, clean in digests:
            fh.write(digest_text(tag, url, clean) + "\n\n")

    # Link dump for search jobs (compact)
    with open(os.path.join(BASE, "searchlinks.md"), "w", encoding="utf-8") as fh:
        for rec in manifest:
            if rec.get("type") != "search":
                continue
            p = os.path.join(JSN, rec["id"] + ".json")
            if not os.path.exists(p):
                continue
            data = json.load(open(p, encoding="utf-8"))
            fh.write("### %s  [engine=%s]  query=%s\n" % (rec["id"], data.get("engine"), rec.get("query")))
            for text, url in data.get("links", [])[:28]:
                fh.write("- %s | %s\n" % (text, url))
            fh.write("\n")

    print("DONE %d jobs" % len(manifest), flush=True)


if __name__ == "__main__":
    main()
