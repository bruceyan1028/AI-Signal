"""Compare today's brief URLs with the current HTML extractor and Mozilla Readability.

Usage: READABILITY_NODE_PREFIX=/tmp/feishu-readability-run python3 -m tools.compare_readability
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from src import rss

ROOT = Path(__file__).resolve().parents[1]
BRIEF = ROOT / "site/data/brief-2026-09-14.json"
NODE_PREFIX = Path(os.environ.get("READABILITY_NODE_PREFIX", "/tmp/feishu-readability-run"))
NODE_SCRIPT = ROOT / "tools/readability_extract.mjs"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/133 Safari/537.36"
PROBE = re.compile(r"read\s+more|subscribe|sign\s*(?:in|up)|cookie|privacy\s+policy|terms\s+of|related\s+(?:articles?|posts?)|相关阅读|推荐阅读|关注我们", re.I)
JUNK = re.compile(r'"_id"|avatarUrl|"updatedAt"|__NEXT_DATA__|window\.__|"pageProps"', re.I)


def main() -> None:
    data = json.loads(BRIEF.read_text(encoding="utf-8"))
    rows = []
    for signal in data.get("signals", []):
        url = str(signal.get("url") or signal.get("link") or "")
        if not url.startswith("http"):
            continue
        title = str(signal.get("title") or signal.get("titleEn") or signal.get("titleCn") or "")
        t0 = time.perf_counter()
        try:
            response = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            response.raise_for_status()
            html = response.content[:1_500_000].decode(response.encoding or "utf-8", errors="replace")
            fetch_error = ""
        except Exception as exc:  # noqa: BLE001
            html, fetch_error = "", f"{type(exc).__name__}: {exc}"
        row = {"url": url, "title": title, "source": signal.get("source") or signal.get("sourceName") or "", "html": html, "fetch_ms": round((time.perf_counter() - t0) * 1000), "fetch_error": fetch_error}
        current = rss.parse_article_html(html, url, title) if html else {"text": "", "images": []}
        row["current"] = {"chars": len(current.get("text") or ""), "images": len(current.get("images") or []), "noise": sorted(set(m.group(0).lower() for m in PROBE.finditer(current.get("text") or ""))), "junk": bool(JUNK.search(current.get("text") or "")), "preview": (current.get("text") or "")[:320]}
        rows.append(row)
    payload = [{k: r[k] for k in ("url", "title", "html")} for r in rows if r["html"]]
    env = dict(os.environ)
    env["NODE_PATH"] = str(NODE_PREFIX / "node_modules")
    proc = subprocess.run(["node", str(NODE_SCRIPT)], input=json.dumps(payload), text=True, capture_output=True, check=True, env=env)
    readability = {r["url"]: r for r in json.loads(proc.stdout)}
    for row in rows:
        parsed = readability.get(row["url"], {})
        text = parsed.get("text") or ""
        row["readability"] = {"chars": len(text), "noise": sorted(set(m.group(0).lower() for m in PROBE.finditer(text))), "junk": bool(JUNK.search(text)), "title": parsed.get("title", ""), "preview": text[:320], "error": parsed.get("error", "")}
        row.pop("html", None)
    out = ROOT / "output/extractor-readability-2026-09-14.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"date": "2026-09-14", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"输出: {out}")
    print(f"{'#':>2} {'来源':<18} {'当前':>7} {'Readability':>12} {'差异':>8} {'当前图':>5}  标题")
    print("-" * 100)
    for i, row in enumerate(rows, 1):
        cur, read = row["current"], row["readability"]
        print(f"{i:>2} {str(row['source'])[:18]:<18} {cur['chars']:>7} {read['chars']:>12} {read['chars']-cur['chars']:>+8} {cur['images']:>5}  {row['title'][:42]}")
    print("\n失败:", sum(bool(r["fetch_error"] or r["readability"].get("error")) for r in rows))


if __name__ == "__main__":
    main()
