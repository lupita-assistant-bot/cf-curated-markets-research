#!/usr/bin/env python3
"""Fetch a list of URLs and dump raw HTML, cleaned text, and parsed JSON-LD.

Run inside GitHub Actions (outbound internet available).
"""
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


def http_get(url, timeout=60):
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
    low = txt.lower()
    for bad in ("captcha", "are you a robot", "unusual traffic", "access denied",
                "enable javascript and cookies", "cf-browser-verification",
                "checking your browser", "request unsuccessful"):
        if bad in low:
            return True
    return False


def fetch(url):
    """Return (status, final_url, text, note)."""
    notes = []
    try:
        st, fin, body = http_get(url)
        notes.append("direct:%s:%d:%d" % (st, len(body), 0))
        if len(body) > 400 and not looks_blocked(body):
            return st, fin, body, ";".join(notes)
        notes.append("direct-weak-or-blocked")
    except Exception as e:
        notes.append("direct-error:%s" % type(e).__name__)
    # fallback 1: jina reader proxy
    try:
        st, fin, body = http_get(JINA + url)
        notes.append("jina:%s:%d" % (st, len(body)))
        if len(body) > 200 and not looks_blocked(body):
            return st, fin, body, ";".join(notes)
    except Exception as e:
        notes.append("jina-error:%s" % type(e).__name__)
    # fallback 2: jina with http scheme forced
    try:
        st, fin, body = http_get(JINA + url.replace("https://", "http://"))
        notes.append("jina-http:%s:%d" % (st, len(body)))
        if len(body) > 200:
            return st, fin, body, ";".join(notes)
    except Exception as e:
        notes.append("jina-http-error:%s" % type(e).__name__)
    return None, url, "", ";".join(notes)


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|td|span)>", "\n", h)
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
    """Yield every dict nested inside a JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def collect_events(objs):
    """Pull Event-ish dicts out of parsed JSON-LD."""
    events = []
    seen = set()
    for o in objs:
        for d in walk(o):
            typ = d.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            is_event = any(t in ("Event", "SocialEvent", "MusicEvent", "Festival",
                                 "ExhibitionEvent", "CraftEvent", "BusinessEvent",
                                 "LiteraryEvent", "FoodEvent", "Market", "SaleEvent")
                           for t in types if t)
            if not is_event:
                continue
            name = d.get("name")
            if not name:
                continue
            key = (str(name), str(d.get("startDate", "")))
            if key in seen:
                continue
            seen.add(key)
            loc = d.get("location")
            addr = {}
            if isinstance(loc, dict):
                a = loc.get("address")
                if isinstance(a, dict):
                    addr = a
                elif isinstance(a, list) and a and isinstance(a[0], dict):
                    addr = a[0]
            locname = loc.get("name") if isinstance(loc, dict) else None
            events.append({
                "name": name,
                "startDate": d.get("startDate"),
                "endDate": d.get("endDate"),
                "address": {
                    "streetAddress": addr.get("streetAddress"),
                    "addressLocality": addr.get("addressLocality"),
                    "addressRegion": addr.get("addressRegion"),
                    "postalCode": addr.get("postalCode"),
                },
                "locationName": locname,
                "description": (d.get("description") or "")[:900],
                "url": d.get("url") or d.get("offers", [{}])[0].get("url")
                       if isinstance(d.get("offers"), list) and d.get("offers") else d.get("url"),
                "offers": d.get("offers"),
                "eventStatus": d.get("eventStatus"),
                "organizer": d.get("organizer"),
                "image": d.get("image"),
                "type": types,
            })
    return events


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
            tid = parts[0].strip()
            url = parts[1].strip()
            targets.append((tid, url))

    manifest = []
    for i, (tid, url) in enumerate(targets, 1):
        print("[%d/%d] %s %s" % (i, len(targets), tid, url), flush=True)
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
                json.dump({"id": tid, "url": url, "jsonld_count": len(objs),
                           "events": evs}, fh, indent=1, default=str)
            rec["jsonld_objects"] = len(objs)
            rec["events_found"] = len(evs)
            rec["text_len"] = len(clean)
        except Exception as e:
            rec["error"] = "%s: %s" % (type(e).__name__, e)
        manifest.append(rec)
        print("   -> %s" % json.dumps({k: v for k, v in rec.items() if k != "url"})[:400], flush=True)
        time.sleep(1)

    with open(outfile, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)
    print("DONE %d targets" % len(manifest), flush=True)


if __name__ == "__main__":
    main()
