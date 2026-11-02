# Central Florida Curated Markets Research

Temporary research workspace. `fetch.py` fetches target URLs (with direct fetch + jina reader fallback),
strips HTML, and extracts JSON-LD Event objects. `urls.txt` holds the target list.
Results land in `results/` (raw HTML, cleaned text, parsed JSON) and are committed by the Actions workflow.

Trigger: push any change to `main`.
