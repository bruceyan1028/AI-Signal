"""Rebuild a read-only ingest snapshot and export its links to an XLSX workbook.

This tool deliberately does not update source cursors, collect stats, health records,
or signals.  It is intended for examining the candidates from a recent ingest run.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from src import feishu, process, rss, scrape, social, sources, typed_config


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "run-links-2026-09-15.json"


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _item_row(item: dict) -> dict[str, str]:
    feed = item.get("feed") or {}
    return {
        "source_id": _text(item.get("source_id") or feed.get("id")),
        "source": _text(feed.get("name")),
        "method": _text(feed.get("fetch_method")),
        "title": _text(item.get("title")),
        "url": _text(item.get("url")),
        "published_raw": _text(item.get("published_raw")),
        "published_ms": _text(item.get("published_ms")),
        "duplicate_key": _text(item.get("duplicate_key")),
        "body_chars": str(len(_text(item.get("body")))),
    }


def main() -> None:
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    type_configs = typed_config.load_typed_configs(token)

    rss_feeds = sources.map_feed_sources(records)
    for feed in rss_feeds:
        cfg = type_configs.get(feed.get("id") or "") or {}
        feed["source_type"] = sources.infer_signal_format(
            feed.get("id") or "", endpoint=feed.get("url") or "", extra=feed.get("extra_config"),
            fetch_method=feed.get("fetch_method") or "", entity_type=cfg.get("entity_type"),
            explicit_type=feed.get("source_type"),
        )
    scrape_feeds = sources.map_scrape_sources(records)
    specs = __import__("src.capture_spec", fromlist=["load"]).load()
    for feed in scrape_feeds:
        feed["_capture_specs"] = specs
        cfg = type_configs.get(feed.get("id") or "") or {}
        if cfg.get("entity_type"):
            feed["source_type"] = sources.infer_signal_format(
                feed.get("id") or "", endpoint=feed.get("url") or "", extra=feed.get("extra_config"),
                fetch_method="Scrape", entity_type=cfg.get("entity_type"), explicit_type=feed.get("source_type"),
            )
        if cfg.get("entity_type") == "github":
            feed["github_config"] = cfg.get("params") or {}
        feed["cohort"] = sources.scrape_cohort(str(feed.get("id") or ""), category=str(feed.get("category") or ""), url=str(feed.get("url") or ""))

    raw, _ = rss.fetch_feed_sources_with_stats(rss_feeds)
    scraped, _ = scrape.fetch_scrape_sources_with_stats(scrape_feeds, engine="auto")
    raw.extend(scraped)

    # Ignore persisted since_id so the export represents the same configured lookback window.
    social_feeds = sources.map_social_sources(
        records,
        {sid: cfg.get("params") or {} for sid, cfg in type_configs.items() if cfg.get("entity_type") == "social"},
    )
    for feed in social_feeds:
        feed["cursor_state"] = {}
    batch = social.fetch_social_sources(social_feeds)
    cleaned = process.process_and_clean(raw, type_configs)

    payload = {
        "cleaned": [_item_row(item) for item in cleaned],
        "social_raw": [_item_row(item) for item in batch.items],
        "social_read_counts": batch.read_counts,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cleaned": len(payload["cleaned"]), "social_raw": len(payload["social_raw"]), "path": str(OUT_JSON)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
