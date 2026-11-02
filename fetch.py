#!/usr/bin/env python3
"""Fetch a list of URLs and dump raw HTML, cleaned text, and parsed JSON-LD.

Runs inside GitHub Actions (outbound internet available).
Parallel fetch across a thread pool; per-target fallbacks: direct -> jina reader.
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
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "results", "raw")
TXT = os.path.join(ROOT, "results", "text")
JSN = os.path.join(ROOT, "results", "parsed")
for d in (RAW, TXT, JSN):
    os.makedirs(d, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
JINA = "https://r.jina.ai/"


def http_get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return r.status, r.geturl(), raw.decode("utf-8", "replace")


def looks_blocked(txt):
    low = txt[:8000].lower()
    for bad in ("captcha", "are you a robot", "unusual traffic", "access denied",
                "enable javascript and cookies", "cf-browser-verification",
                "checking your browser", "request unsuccessful",
                "attention required!", "unusual activity"):
        if bad in low:
            return True
    return False


def fetch(url):
    notes = []
    try:
        st, fin, body = http_get(url)
        notes.append("direct:%s:%d" % (st, len(body)))
        if len(body) > 400 and not looks_blocked(body):
            return st, fin, body, ";".join(notes)
        notes.append("direct-weak-or-blocked")
    except Exception as e:
        notes.append("direct-error:%s" % type(e).__name__)
    for label, u in (("jina", JINA + url), ("jina-http", JINA + url.replace("https://", "http://"))):
        try:
            st, fin, body = http_get(u)
            notes.append("%s:%s:%d" % (label, st, len(body)))
            if len(body) > 200 and not looks_blocked(body):
                return st, fin, body, ";".join(notes)
        except Exception as e:
            notes.append("%s-error:%s" % (label, type(e).__name__))
    return None, url, "", ";".join(notes)


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|td|span|a)>", "\n", h)
    h = TAG_RE.sub(" ", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n[ \t]*\n+", "\n", h)
    return h.strip()


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
               "Market", "SaleEvent", "ShoppingEvent", "Exhibition")


def collect_events(objs):
    events = []
    seen = set()
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
            addr = {}
            locname = None
            if isinstance(loc, dict):
                locname = loc.get("name")
                a = loc.get("address")
                if isinstance(a, dict):
                    addr = a
                elif isinstance(a, list) and a and isinstance(a[0], dict):
                    addr = a[0]
            elif isinstance(loc, list) and loc:
                l0 = loc[0]
                if isinstance(l0, dict):
                    locname = l0.get("name")
                    a = l0.get("address")
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
                "startDate": d.get("startDate"),
                "endDate": d.get("endDate"),
                "streetAddress": addr.get("streetAddress"),
                "addressLocality": addr.get("addressLocality"),
                "addressRegion": addr.get("addressRegion"),
                "postalCode": addr.get("postalCode"),
                "locationName": locname,
                "description": (str(d.get("description"))[:900] if d.get("description") else ""),
                "url": d.get("url") or ourl,
                "eventStatus": d.get("eventStatus"),
                "type": [t for t in types if t],
            })
    return events


def do_one(tid, url):
    rec = {"id": tid, "url": url}
    try:
        st, fin, body, note = fetch(url)
        rec.update({"status": st, "final_url": fin, "note": note, "bytes": len(body)})
        with open(os.path.join(RAW, tid + ".html"), "w", encoding="utf-8") as fh:
            fh.write(body)
        clean = body if body.lstrip().startswith("https://") else strip_html(body)
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
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    return rec


def main():
    urlfile = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "urls.txt")
    outfile = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "results", "manifest.json")
    targets = []
    with open(urlfile, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            targets.append((parts[0].strip(), parts[1].strip()))

    manifest = []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(do_one, t, u): t for t, u in targets}
        for fut in cf.as_completed(futs):
            rec = fut.result()
            manifest.append(rec)
            print("%-22s status=%-5s bytes=%-8s jsonld=%-4s events=%-4s %s" % (
                rec.get("id"), rec.get("status"), rec.get("bytes"),
                rec.get("jsonld_objects"), rec.get("events_found"),
                (rec.get("note") or rec.get("error") or "")[:90]), flush=True)

    order = {t: i for i, (t, _) in enumerate(targets)}
    manifest.sort(key=lambda r: order.get(r.get("id"), 9999))
    with open(outfile, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)
    print("DONE %d targets" % len(manifest), flush=True)


if __name__ == "__main__":
    main()
