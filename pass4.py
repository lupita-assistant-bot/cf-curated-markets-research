#!/usr/bin/env python3
"""Pass 4: targeted detail pages + recurring-market confirmation.

Fetches specific detail URLs whose exact hours/addresses matter, and
reports the cleaned text so the parent agent can quote verified details.
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
OUT = os.path.join(ROOT, "results4")
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
        "User-Agent": UA, "Accept": "text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate", "Connection": "close"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return r.status, r.geturl(), raw.decode("utf-8", "replace")


BLOCKERS = ("captcha", "are you a robot", "unusual traffic", "access denied",
            "enable javascript and cookies", "checking your browser",
            "request unsuccessful", "attention required!", "are you a human",
            "security check", "to discuss automated access")


def strip(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|td|span|a)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n[ \t]*\n+", "\n", h)
    return h.strip()


def fetch(url):
    att = []
    for label, tmpl in PROXIES:
        u = url if label == "direct" else tmpl.format(u=urllib.parse.quote(url, safe=""))
        try:
            st, fin, body = http_get(u)
            att.append("%s:%s:%d" % (label, st, len(body)))
            low = body[:12000].lower()
            if len(body) > 400 and not any(b in low for b in BLOCKERS):
                return body, label, att
        except Exception as e:
            att.append("%s-err:%s" % (label, type(e).__name__))
    return None, None, att


TARGETS = [
    # Thornton Park night market detail
    ("tpd_nightmarket", "https://www.thorntonparkdistrict.com/tpd-night-market"),
    ("tpd_happenings", "https://www.thorntonparkdistrict.com/happenings"),
    ("tpd_events2", "https://www.thorntonparkdistrict.com/events"),
    # Renninger's Mt Dora detail + hours/address
    ("renn_mtdora_home", "https://renningers.net/mt-dora/"),
    ("renn_guitars_cars", "https://renningers.net/events/guitars-and-cars-2-733-490-999-567-508-593-557-384-610-683-940-103-520/"),
    ("renn_holiday_market", "https://renningers.net/events/holiday-market-antiques-show-531-502-558/"),
    ("renn_events_all", "https://renningers.net/events/"),
    # Winter Park Sunday Market
    ("wp_sunmarket", "https://www.winterparkmainstreet.org/sunday-market"),
    ("wp_events", "https://www.winterparkmainstreet.org/events"),
    ("wp_calendar", "https://www.winterparkmainstreet.org/events/list"),
    # Sanford First Friday
    ("sanford_ff2", "https://www.sanfordmainstreet.org/first-friday-art-walk/"),
    ("sanford_events", "https://www.sanfordmainstreet.org/events/"),
    ("sanford_home", "https://www.sanfordmainstreet.org/"),
    ("sanford_arts", "https://www.sanfordartsandheritage.com/"),
    # Downtown Orlando - paginate Nov
    ("dto_nov_p1", "https://www.downtownorlando.com/events/"),
    ("dto_nov_p2", "https://www.downtownorlando.com/events/?page=2"),
    ("dto_nov_p3", "https://www.downtownorlando.com/events/?page=3"),
    ("dto_art", "https://www.downtownorlando.com/art-culture/"),
    # Mt Dora / Lake County antique + market orgs
    ("lakeantiques", "https://www.lakeantiquesmall.com/"),
    ("mda_events", "https://www.mountdoraarts.org/"),
    ("mda_home", "https://www.mountdoraarts.org/"),
    ("historicmd", "https://historicmountdora.com/"),
    ("md_month", "https://historicmountdora.com/events/month/2026-11/"),
    ("tavares_art2", "https://www.tavaresartclub.org/"),
    ("tavares_downtown", "https://www.tavaresdowntown.com/"),
    ("clermont_downtown", "https://www.clermontdowntown.com/"),
    # Altamonte / antique malls
    ("altamonte_moa", "https://www.altamonteartandantiques.com/"),
    ("oc_moa", "https://www.ocma-net.org/"),
    # Maker/artisan orgs
    ("lando_arts", "https://www.lando.org/events/"),
    ("lando_home", "https://www.lando.org/"),
    ("accent_events", "https://www.accentorlando.com/events"),
    ("maitland_arts", "https://www.maitlandarts.org/"),
    ("maitland_arts_events", "https://www.maitlandarts.org/events/"),
    ("opalam", "https://www.opalarts.com/"),
    ("nona_events", "https://www.nonafashionrow.com/events"),
    ("beachpark_events", "https://www.beachparkorlando.com/events"),
    # Tampa art walks (target area per task)
    ("ybor_events", "https://www.yborcity.com/events/"),
    ("heacock", "https://www.heacockheights.com/events/"),
    ("tampa_artwalk", "https://www.artwalktampa.com/"),
    ("tampa_artwalk2", "https://artwalktampa.com/"),
    ("hyde_events", "https://www.hydeparkartfest.com/"),
    # Space coast / daytona
    ("db_arts", "https://www.daytonabeacharts.com/"),
    ("db_artwalk", "https://www.daytonabeacharts.com/events"),
    # Central FL makers
    ("makers_gfl", "https://www.gulfmakerspace.org/"),
    ("makerfest", "https://www.makerfestflorida.com/"),
]


def main():
    res = {}
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch, u): (t, u) for t, u in TARGETS}
        for f in cf.as_completed(futs):
            t, u = futs[f]
            try:
                body, label, att = f.result()
            except Exception as e:
                body, label, att = None, None, ["err:%s" % type(e).__name__]
            if body:
                txt = strip(body)
                with open(os.path.join(OUT, t + ".txt"), "w", encoding="utf-8") as fh:
                    fh.write(txt)
                res[t] = {"url": u, "engine": label, "attempts": att,
                          "bytes": len(body), "text": txt}
                print("%-24s %-10s bytes=%-7d lines=%d" % (
                    t, label, len(body), txt.count("\n")), flush=True)
            else:
                res[t] = {"url": u, "engine": None, "attempts": att, "bytes": 0}
                print("%-24s FAIL %s" % (t, att), flush=True)

    with open(os.path.join(OUT, "all.json"), "w", encoding="utf-8") as fh:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "text"}
                   for k, v in res.items()}, fh, indent=1, default=str)

    # keyword-highlighted digest
    KW = re.compile(r"(market|vintage|antique|artisan|maker|handcraft|craft|art walk|"
                    r"artwalk|flea|night market|pop-?up|thrift|swap meet|gallery hop|"
                    r"bazaar|festival|sunday market|open studios)", re.I)
    with open(os.path.join(OUT, "digest.md"), "w", encoding="utf-8") as fh:
        for t in sorted(res):
            v = res[t]
            fh.write("### %s  (%s)\nURL: %s\n" % (t, v.get("engine") or "FAIL", v["url"]))
            if not v.get("text"):
                fh.write("  FAILED: %s\n\n" % ", ".join(v.get("attempts", [])))
                continue
            lines = [l.strip() for l in v["text"].split("\n")]
            keep, seen = [], set()
            for i, l in enumerate(lines):
                if not l or len(l) > 400:
                    continue
                if KW.search(l):
                    ctx = " | ".join(x for x in lines[max(0, i - 1):i + 2] if x)
                    if ctx[:90] in seen:
                        continue
                    seen.add(ctx[:90])
                    keep.append(ctx)
            for k in keep[:60]:
                fh.write("  - %s\n" % k[:330])
            fh.write("\n")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
