#!/usr/bin/env python3
"""Mine harvested text/JSON for Nov 2-8 2026 curated-market candidates.

Scans results/text/*.txt, results/parsed/*.json, results2/text/*.txt and
results2/parsed/*.json for lines mentioning markets/art walks/crafts/antiques
plus Nov 2026 dates or recurring weekday schedules.
"""
import json
import os
import re
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))
BASES = [os.path.join(ROOT, "results2"), os.path.join(ROOT, "results")]

MKT = re.compile(
    r"(vintage|antique|antiques|maker|makers|handcraft|hand-?made|artisan|craft fair|"
    r"art walk|artwalk|art fest|flea|night market|pop-?up|thrift|shabby|treasure hunt|"
    r"swap meet|maker faire|open studios|gallery hop|bazaar|vendor|market|"
    r"street fair|market day)", re.I)

NOV = re.compile(
    r"(Nov(?:ember)?\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*2026|"
    r"\d{1,2}\s*Nov(?:ember)?\.?\s*2026|"
    r"Nov(?:ember)?\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|"
    r"Nov(?:ember)?\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*(?:2026|'26))", re.I)

WEEKDAY = re.compile(
    r"(every (?:sun|mon|tues|wednes|thurs|fri|satur)day|"
    r"(?:sun|mon|tues|wednes|thurs|fri|satur)days?\s|\bweekly\b|"
    r"\b1st\b|\b2nd\b|\b3rd\b|\b4th\b|\b5th\b)", re.I)

DATE_WINDOW = re.compile(r"2026-11-(0[2-8])")
HOURS = re.compile(r"\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)", re.I)
ADDR = re.compile(r"\d{3,6}\s+[A-Z][\w.'&\- ]{1,35}(?:St|Ave|Rd|Blvd|Dr|Ln|Way|Ct|Pl|Pkwy|Cir|Hwy|Trl|Drive|Street|Avenue|Road)\b", re.I)

CTX = 1


def window(lines, idx):
    lo = max(0, idx - CTX)
    hi = min(len(lines), idx + CTX + 1)
    return " | ".join(l for l in lines[lo:hi])


def scan_text(path):
    tid = os.path.basename(path)[:-4]
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    lines = [l.strip() for l in txt.split("\n")]
    hits = []
    for i, l in enumerate(lines):
        if not l or len(l) > 600:
            continue
        if not MKT.search(l):
            continue
        has_nov = bool(NOV.search(l))
        has_win = bool(DATE_WINDOW.search(l))
        has_wd = bool(WEEKDAY.search(l))
        has_hr = bool(HOURS.search(l))
        if not (has_nov or has_win or (has_wd and has_hr)):
            continue
        hits.append({
            "id": tid, "line": l, "date_window": has_win, "nov": has_nov,
            "weekday": has_wd, "hours": has_hr, "addr": bool(ADDR.search(l)),
            "context": window(lines, i),
        })
    return hits


def scan_parsed(path):
    tid = os.path.basename(path)[:-4]
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for e in data.get("events", []):
        sd = str(e.get("startDate") or "")
        ed = str(e.get("endDate") or "")
        blob = sd + " " + ed + " " + (e.get("name") or "") + " " + (e.get("description") or "")
        inwin = bool(DATE_WINDOW.search(sd + " " + ed))
        desc = re.sub(r"<[^>]+>", " ", e.get("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()
        if inwin:
            kind = "EVENT"
        elif NOV.search(sd + " " + ed) and MKT.search(blob):
            kind = "EVENT_NOV"
        else:
            continue
        out.append({"id": tid, "kind": kind, "name": e.get("name"),
                    "start": sd, "end": ed, "url": e.get("url"),
                    "location": e.get("locationName"),
                    "street": e.get("streetAddress"), "city": e.get("addressLocality"),
                    "desc": desc[:700]})
    return out


def main():
    events, lines = [], []
    for base in BASES:
        for p in sorted(glob.glob(os.path.join(base, "parsed", "*.json"))):
            events.extend(scan_parsed(p))
        for p in sorted(glob.glob(os.path.join(base, "text", "*.txt"))):
            lines.extend(scan_text(p))

    manifest = {}
    for base in BASES:
        mp = os.path.join(base, "manifest.json")
        if os.path.exists(mp):
            try:
                for r in json.load(open(mp, encoding="utf-8")):
                    manifest.setdefault(r.get("id"), r)
            except Exception:
                pass

    out = os.path.join(ROOT, "results2")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "candidates.md"), "w", encoding="utf-8") as fh:
        fh.write("# CANDIDATES - Central Florida curated markets, Nov 2-8 2026\n\n")
        fh.write("## A. JSON-LD events inside window\n\n")
        for e in sorted(events, key=lambda x: (x["kind"] != "EVENT", str(x["start"]))):
            fh.write("- [%s] %s\n" % (e["kind"], e.get("name")))
            fh.write("  start=%s end=%s\n" % (e.get("start"), e.get("end")))
            fh.write("  loc=%s / %s %s\n" % (e.get("location"), e.get("street"), e.get("city")))
            fh.write("  url=%s\n" % (e.get("url")))
            if e.get("desc"):
                fh.write("  desc=%s\n" % e["desc"])
            fh.write("  src=%s\n" % e["id"])
            fh.write("\n")
        fh.write("## B. Text lines: market keywords + Nov date/weekday+hours\n\n")
        seen = set()
        for h in lines:
            key = h["line"][:110]
            if key in seen:
                continue
            seen.add(key)
            m = manifest.get(h["id"], {})
            fh.write("- [%s] %s\n" % (h["id"], h["line"][:320]))
            if h["context"] and h["context"] != h["line"]:
                fh.write("    ctx: %s\n" % h["context"][:320])
            fh.write("    src=%s status=%s\n" % (h["id"], m.get("status")))
            fh.write("\n")

    summary = {"events_in_window": [e for e in events if e["kind"] == "EVENT"],
               "events_nov_outside": [e for e in events if e["kind"] == "EVENT_NOV"],
               "text_hits": len(lines)}
    with open(os.path.join(out, "candidates.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, default=str)
    print("events_in_window=%d events_nov=%d text_hits=%d" % (
        len(summary["events_in_window"]), len(summary["events_nov_outside"]), len(lines)),
        flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
