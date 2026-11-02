#!/usr/bin/env python3
"""Third pass: parse event-listing pages that need structured extraction.

Handles:
  * Granicus-style listing pages (downtownorlando.com/events) - split into
    repeating event blocks of [title, date, description, venue, address]
  * GoSee Local (goseelocal.com / orlandonightmarket.com) event pages

Writes results3/ with compact JSON + markdown.
"""
import concurrent.futures as cf
import gzip
import html as htmlmod
import json
import os
import re
import ssl
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "results3")
os.makedirs(OUT, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PROXIES = [
    ("direct", "{u}"),
    ("allorigins", "https://api.allorigins.win/raw?url={u}"),
    ("codetabs", "https://api.codetabs.com/v1/proxy?quest={u}"),
    ("jina", "https://r.jina.ai/{u}"),
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    })
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
            "to discuss automated access")


def looks_blocked(t):
    low = t[:12000].lower()
    return any(b in low for b in BLOCKERS)


def fetch(url):
    att = []
    for label, tmpl in PROXIES:
        u = url if label == "direct" else tmpl.format(u=urllib.parse.quote(url, safe=""))
        try:
            st, fin, body = http_get(u)
            att.append("%s:%s:%d" % (label, st, len(body)))
            if len(body) > 400 and not looks_blocked(body):
                return body, label, att
        except Exception as e:
            att.append("%s-err:%s" % (label, type(e).__name__))
    return None, None, att


TAG_RE = re.compile(r"<[^>]+>")
A_RE = re.compile(r'(?is)<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>')


def strip(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|td|span|a)>", "\n", h)
    h = TAG_RE.sub(" ", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n[ \t]*\n+", "\n", h)
    return h.strip()


MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
          "November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
DATE_BLOCK = re.compile(
    r"(\d{1,2})\s*\n?\s*(" + MONTHS + r")\s*\n?\s*\n?\s*(20\d{2})", re.I)

TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*(?:am|pm)(?:\s*[-–to]+\s*\d{1,2}:\d{2}\s*(?:am|pm))?)", re.I)


def parse_granicus(text):
    """Split cleaned listing text into event blocks and pull fields."""
    lines = [l.strip() for l in text.split("\n")]
    events = []
    # find index of each date marker "05\nNov\n2026"
    marks = []
    for i in range(len(lines) - 2):
        d, mo, y = lines[i], lines[i + 1], lines[i + 2]
        if (d.isdigit() and 1 <= int(d) <= 31 and mo.lower().rstrip('.') in
                [m.lower() for m in MONTHS.split("|")]):
            if re.match(r"^20\d{2}$", y):
                marks.append((i, d, mo, y))
    for k, (i, d, mo, y) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else min(len(lines), i + 18)
        blk = lines[i + 3:end]
        blk = [b for b in blk if b and b not in ("Tagged as:", "Downtown Events")]
        # title = first non-empty line after date
        title = blk[0] if blk else ""
        rest = blk[1:]
        desc = ""
        addr = ""
        venue = ""
        for b in rest:
            if not desc and len(b) > 40 and not TIME_RE.fullmatch(b):
                desc = b
                continue
            if not venue and len(b) < 90 and not TIME_RE.search(b) and not b.startswith("Tagged"):
                venue = b
                continue
            if not addr:
                m = re.match(r"^(\d+[^,]*,\s*\w[\w\s]*(\s+\d{5})?)$", b)
                if m:
                    addr = b
        times = [x for x in rest if TIME_RE.search(x)]
        events.append({
            "date": "%s %s %s" % (mo, d, y),
            "title": title, "venue": venue, "address": addr,
            "time": times[0] if times else "",
            "description": desc[:500],
        })
    return events


def extract_event_links(body):
    out = []
    seen = set()
    for m in A_RE.finditer(body):
        url = htmlmod.unescape(m.group(1)).strip()
        text = strip(m.group(2))
        if "event" not in url.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((text[:120], url))
    return out


def main():
    targets = []
    # Downtown Orlando listing pages (Nov 2026) - pagination variants
    dto = "https://www.downtownorlando.com/events/"
    for p in range(0, 6):
        targets.append(("dto_p%d" % p, dto + ("?page=%d" % (p + 1) if p else "")))
    # GoSee Local / Orlando night market platform
    targets.append(("gosee_events", "https://goseelocal.com/events/"))
    targets.append(("gosee_home", "https://goseelocal.com/"))
    targets.append(("gosee_market", "https://goseelocal.com/events/?q=market"))
    targets.append(("gosee_vintage", "https://goseelocal.com/events/?q=vintage"))
    targets.append(("gosee_art", "https://goseelocal.com/events/?q=art"))
    targets.append(("gosee_craft", "https://goseelocal.com/events/?q=craft"))
    targets.append(("gosee_thrift", "https://goseelocal.com/events/?q=thrift"))
    targets.append(("gosee_antique", "https://goseelocal.com/events/?q=antique"))
    targets.append(("gosee_flea", "https://goseelocal.com/events/?q=flea"))
    targets.append(("gosee_popup", "https://goseelocal.com/events/?q=pop%20up"))
    targets.append(("gosee_maker", "https://goseelocal.com/events/?q=maker"))
    # More listings
    targets.append(("gosee_orlando", "https://goseelocal.com/events/?location=orlando"))
    targets.append(("gosee_nov", "https://goseelocal.com/events/?month=11&year=2026"))
    targets.append(("dtolive", "https://dtolivearts.com/events/"))
    targets.append(("dtolive2", "https://dtolivearts.com/"))
    targets.append(("lando_arts", "https://www.lando.org/events/"))
    targets.append(("lando_arts2", "https://www.lando.org/"))
    targets.append(("accent_events", "https://www.accentorlando.com/events"))
    targets.append(("maitland_moa_events", "https://www.maitlandarts.org/events"))
    targets.append(("wp_sunday2", "https://www.winterparkmainstreet.org/sunday-market"))
    targets.append(("wp_about", "https://www.winterparkmainstreet.org/about"))
    targets.append(("sanford_home2", "https://www.sanfordmainstreet.org/"))
    targets.append(("sanford_ff", "https://www.sanfordmainstreet.org/events/?cat=arts"))
    targets.append(("mountdora_home", "https://www.mountdora.com/"))
    targets.append(("mountdora_events2", "https://www.mountdora.com/events/?date=2026-11"))
    targets.append(("historic_md", "https://historicmountdora.com/"))
    targets.append(("historic_md_events", "https://historicmountdora.com/events/"))
    targets.append(("tavares_downtown2", "https://www.tavaresdowntown.com/events/"))
    targets.append(("clermont_downtown2", "https://www.clermontdowntown.com/events/"))
    targets.append(("clermont_art", "https://www.clermontartleague.org/"))
    targets.append(("altamonte_moa2", "https://www.altamonteartandantiques.com/"))
    targets.append(("sanford_heritage", "https://www.sanfordartsandheritage.com/"))
    targets.append(("eastend_popups", "https://eastendmkt.com/apply-to-pop-up/"))
    targets.append(("eastend_merchants", "https://eastendmkt.com/our-merchants/"))

    results = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, u): (tid, u) for tid, u in targets}
        for fut in cf.as_completed(futs):
            tid, u = futs[fut]
            try:
                body, label, att = fut.result()
            except Exception as e:
                body, label, att = None, None, ["err:%s" % type(e).__name__]
            rec = {"id": tid, "url": u, "engine": label, "attempts": att,
                   "bytes": len(body) if body else 0}
            if body:
                txt = strip(body)
                with open(os.path.join(OUT, tid + ".txt"), "w", encoding="utf-8") as fh:
                    fh.write(txt)
                evs = parse_graniscus(txt)
                links = extract_event_links(body)
                rec["granicus_events"] = len(evs)
                rec["event_links"] = len(links)
                results[tid] = {"rec": rec, "events": evs, "links": links[:60]}
            else:
                results[tid] = {"rec": rec, "events": [], "links": []}
            print("%-24s %-10s bytes=%-7s g_events=%-4s links=%s" % (
                tid, label or "FAIL", rec["bytes"], rec.get("granicus_events", 0),
                rec.get("event_links", 0)), flush=True)

    with open(os.path.join(OUT, "all.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1, default=str)

    with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("# Pass 3 report\n\n")
        for tid in sorted(results):
            r = results[tid]
            fh.write("## %s  (%s)  %s\n" % (tid, r["rec"].get("engine"), r["rec"]["url"]))
            if r["rec"].get("attempts"):
                fh.write("attempts: %s\n" % ", ".join(r["rec"]["attempts"]))
            for e in r["events"]:
                fh.write("- **%s** | %s | %s | %s | %s\n" % (
                    e["date"], e["title"], e["time"], e["venue"], e["address"]))
                if e["description"]:
                    fh.write("    %s\n" % e["description"][:250])
            if r["links"]:
                fh.write("links:\n")
                for t, u in r["links"][:30]:
                    fh.write("  - %s | %s\n" % (t, u))
            fh.write("\n")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
