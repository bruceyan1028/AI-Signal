"""RSS 源定向诊断；允许 experimental，可选写入条目表。"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from . import config, feishu, main, policy_document, process, rss, sources, typed_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag_rss")

def run(
    source_ids: list[str],
    *,
    write: bool,
    out_path: Path | None = None,
) -> dict[str, Any]:
    config.validate()
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    wanted = {source_id.strip() for source_id in source_ids if source_id.strip()}
    feeds = [
        feed
        for feed in sources.map_feed_sources_for_diag(records)
        if not wanted or str(feed.get("id") or "") in wanted
    ]
    missing = wanted - {str(feed.get("id") or "") for feed in feeds}
    if missing:
        raise ValueError(f"RSS source_id not found or paused: {sorted(missing)}")
    if not feeds:
        raise ValueError("no RSS sources selected")

    raw_items = rss.fetch_feed_sources(feeds)
    drop_stats: dict[str, int] = {}
    cleaned = process.process_and_clean(raw_items, typed_config.load_typed_configs(token), drop_stats)
    existing = feishu.read_existing_dedup_keys(token)
    new_items = main.filter_new_items(cleaned, existing)
    policy_stats = policy_document.enrich_items(new_items)
    rss.backfill_full_text(new_items)
    new_items, _ = process.drop_too_short(new_items)

    created = 0
    if write and new_items:
        created = feishu.batch_create_records(
            token,
            [process.format_for_feishu(item) for item in new_items],
        )
    attempted = {str(feed.get("id") or "") for feed in feeds}
    feishu.sync_param_collect_stats(
        token,
        records,
        attempted,
        cleaned,
        new_items,
        drop_stats,
    )

    result = {
        "sources": sorted(attempted),
        "raw": len(raw_items),
        "cleaned": len(cleaned),
        "new": len(new_items),
        "created": created,
        "policy_documents": policy_stats,
        "raw_by_source": dict(
            Counter(str((item.get("feed") or {}).get("id") or "") for item in raw_items)
        ),
        "cleaned_by_source": dict(Counter(str(item.get("source_id") or "") for item in cleaned)),
        "items": [
            {
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "published_ms": item.get("published_ms"),
                "policy": (item.get("media_assets") or {}).get("policy"),
                "documents": (item.get("media_assets") or {}).get("documents") or [],
                "raw_content_chars": len(str(item.get("raw_content") or "")),
            }
            for item in new_items
        ],
    }
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("RSS 诊断结果：%s", json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="定向诊断 RSS 源")
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="可重复指定；至少指定一个 RSS source_id",
    )
    parser.add_argument("--write", action="store_true", help="将跨轮去重后的条目写入飞书")
    parser.add_argument("--out", type=Path, help="可选 JSON 诊断结果路径")
    args = parser.parse_args()
    raise SystemExit(
        0
        if run(
            args.source_id,
            write=args.write,
            out_path=args.out,
        )
        else 1
    )
