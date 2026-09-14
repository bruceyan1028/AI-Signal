"""聚合近七天已分析信号，生成并持久化 AI 自动周报。"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cluster, config, daily, feishu, report

log = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))


def week_window(end_day: date | None = None) -> tuple[date, date]:
    end_day = end_day or datetime.now(CN_TZ).date()
    return end_day - timedelta(days=max(1, config.WEEKLY_LOOKBACK_DAYS) - 1), end_day


def week_id(end_day: date) -> str:
    iso = end_day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _day_ms(value: date, *, next_day: bool = False) -> int:
    if next_day:
        value += timedelta(days=1)
    return int(datetime.combine(value, time.min, tzinfo=CN_TZ).timestamp() * 1000)


def _table_id(token: str, configured: str, ensure) -> str:
    return configured or ensure(token)


def read_pending(
    token: str, pending_table_id: str, target_week: str
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for record in feishu.read_all_records_with_ids(token, pending_table_id):
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("状态")) or "") != "待纳入":
            continue
        target = str(daily.scalar(fields.get("目标周期")) or "").strip()
        if target and target != target_week:
            continue
        pending.append(record)
    return pending


def collect_candidates(
    records: list[dict[str, Any]],
    params: list[dict[str, Any]],
    start_day: date,
    end_day: date,
    pending_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    start_ms = _day_ms(start_day)
    end_ms = _day_ms(end_day, next_day=True)
    priorities = daily._priority_map(params)
    active = daily._active_source_ids(params)
    candidates: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    pending_ids = pending_ids or set()
    for record in records:
        record_id = str(record.get("record_id") or "")
        if not record_id or record_id in seen_record_ids:
            continue
        fields = record.get("fields") or {}
        source_id = str(daily.scalar(fields.get("source_id")) or "")
        stamp = int(float(daily.scalar(fields.get("发布时间")) or 0))
        in_window = start_ms <= stamp < end_ms
        if record_id not in pending_ids:
            if not in_window or source_id not in active:
                continue
            if daily._existing_analysis(fields) is None:
                continue
        candidates.append(
            {
                "record_id": record_id,
                "fields": fields,
                "source_id": source_id,
                "priority": priorities.get(source_id, "P2"),
                "stamp": stamp,
            }
        )
        seen_record_ids.add(record_id)
    collapsed = cluster.collapse_for_brief(
        candidates, threshold=0.85, limit=config.WEEKLY_SIGNAL_LIMIT
    )
    # 管理员明确加入的条目不能因为同事件折叠而消失；它们需要在 pendingFocus
    # 中可追溯，即使同簇已有更高层级的官方主条目。
    kept_ids = {str(item.get("record_id") or "") for item in collapsed}
    for item in candidates:
        record_id = str(item.get("record_id") or "")
        if record_id in pending_ids and record_id not in kept_ids:
            collapsed.append(item)
            kept_ids.add(record_id)
    return collapsed


def _analysis_fields(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "中文标题": analysis["title_cn"],
        "中文摘要": analysis["summary_cn"],
        "AI深度解读": analysis.get("deep_analysis_cn") or "",
        "为何重要": analysis["why"],
        "影响分": analysis["impact"],
        "新颖度": analysis["novelty"],
        "可行动性": analysis["actionability"],
        "紧迫度": daily.URGENCY_TO_TABLE[analysis["urgency"]],
        "主题": analysis["topics"],
        "状态": "已分析",
    }


def ensure_pending_analyses(
    token: str,
    records_by_id: dict[str, dict[str, Any]],
    pending_records: list[dict[str, Any]],
) -> None:
    updates: list[dict[str, Any]] = []
    for pending in pending_records:
        pending_fields = pending.get("fields") or {}
        record_id = str(daily.scalar(pending_fields.get("条目记录ID")) or "")
        entry = records_by_id.get(record_id)
        if not entry:
            continue
        fields = entry.get("fields") or {}
        if daily._existing_analysis(fields) is not None:
            continue
        analysis = daily.analyze_signal(fields)
        new_fields = _analysis_fields(analysis)
        fields.update(new_fields)
        updates.append({"record_id": record_id, "fields": new_fields})
    feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)


def signal_from_candidate(item: dict[str, Any], *, include_peers: bool = True) -> dict[str, Any] | None:
    fields = item.get("fields") or {}
    analysis = daily._existing_analysis(fields)
    if analysis is None:
        return None
    signal = daily._signal_from_fields(
        str(item.get("record_id") or ""),
        fields,
        analysis,
        priority=str(item.get("priority") or "P2"),
    )
    signal["qualityScore"] = float(daily.scalar(fields.get("质量分")) or 0)
    if include_peers:
        peers = []
        for peer in item.get("eventPeers") or []:
            peer_signal = signal_from_candidate(peer, include_peers=False)
            if peer_signal:
                peer_signal["eventRole"] = str(peer.get("eventRole") or "")
                peer_signal["eventPerspective"] = str(peer.get("eventPerspective") or "")
                peers.append(peer_signal)
        if peers:
            signal["eventPeers"] = peers
    return signal


def deterministic_metrics(
    signals: list[dict[str, Any]], previous: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    impacts = [int(signal.get("impact") or 0) for signal in signals]
    categories = {str(signal.get("category") or "其他") for signal in signals}
    sources = {str(signal.get("source") or "") for signal in signals if signal.get("source")}
    values = {
        "信号总数": len(signals),
        "高影响(≥80)": sum(score >= 80 for score in impacts),
        "覆盖领域": len(categories),
        "覆盖来源": len(sources),
        "平均影响分": round(sum(impacts) / len(impacts)) if impacts else 0,
    }
    previous_values: dict[str, int] = {}
    for item in (previous or {}).get("metrics") or []:
        try:
            previous_values[str(item.get("label") or "")] = int(item.get("value") or 0)
        except (TypeError, ValueError):
            continue
    result: list[dict[str, str]] = []
    for label, value in values.items():
        if label in previous_values:
            delta = value - previous_values[label]
            sub = f"较上周 {delta:+d}" if delta else "与上周持平"
        else:
            sub = "本周"
        result.append({"label": label, "value": str(value), "sub": sub})
    return result


def deterministic_breakdowns(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Build chart-ready facts without asking the LLM to count or rank anything."""
    by_category: dict[str, dict[str, Any]] = {}
    by_day: dict[str, int] = {}
    for signal in signals:
        category = str(signal.get("category") or "其他")
        bucket = by_category.setdefault(category, {"category": category, "count": 0, "highImpact": 0, "_sum": 0})
        impact = int(signal.get("impact") or 0)
        bucket["count"] += 1
        bucket["highImpact"] += int(impact >= 80)
        bucket["_sum"] += impact
        day = str(signal.get("publishedDate") or "")
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    categories = []
    for bucket in by_category.values():
        count = bucket.pop("count")
        total = bucket.pop("_sum")
        bucket["count"] = count
        bucket["avgImpact"] = round(total / count) if count else 0
        categories.append(bucket)
    categories.sort(key=lambda item: (item["count"], item["avgImpact"]), reverse=True)
    return {"categories": categories[:8], "daily": [{"date": day, "count": by_day[day]} for day in sorted(by_day)]}


def _previous_weekly(
    token: str, table_id: str, current_week_id: str
) -> dict[str, Any] | None:
    rows: list[tuple[int, dict[str, Any]]] = []
    for record in feishu.read_all_records_with_ids(token, table_id):
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("周报ID")) or "") == current_week_id:
            continue
        try:
            payload = json.loads(str(daily.scalar(fields.get("周报内容")) or "{}"))
            end_ms = int(float(daily.scalar(fields.get("周期结束")) or 0))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append((end_ms, payload))
    return max(rows, key=lambda item: item[0])[1] if rows else None


def synthesize(
    signals: list[dict[str, Any]],
    metrics: list[dict[str, str]],
    pending_ids: set[str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    numbered = "\n".join(
        (
            f"[{signal['recordId']}] {signal.get('titleCn') or signal.get('title')}｜"
            f"{signal.get('source')}｜{signal.get('publishedDate')}｜{signal.get('category')}｜"
            f"影响{signal.get('impact')} 新颖{signal.get('novelty')} 可行动{signal.get('actionability')}｜"
            f"摘要：{signal.get('summary')}｜为什么重要：{signal.get('why')}｜"
            f"跨源聚合：{json.dumps(signal.get('eventAggregation') or {}, ensure_ascii=False)}｜"
            f"舆论样本：{json.dumps(signal.get('topComments') or [], ensure_ascii=False)}"
        )
        for signal in signals
    )
    pending_note = "、".join(sorted(pending_ids)) or "无"
    previous_context = json.dumps({
        "headline": (previous or {}).get("headline", ""),
        "keyChanges": (previous or {}).get("keyChanges", []),
        "metrics": (previous or {}).get("metrics", []),
    }, ensure_ascii=False)
    prompt = f"""你是面向产品和技术负责人的 AI 情报主编。只依据给定信号输出严格 JSON，不得虚构事实、数字、因果关系或引用。
你的任务不是复述新闻，而是找出本周真正发生的变化。每个判断必须能被 refs 指向的信号支持。
优先从跨源聚合完整或带有热门评论的信号中选事件；sourceSynthesis 必须比较不同来源各自新增的信息、确认和分歧；publicReaction 只能基于提供的评论样本，样本不足时写“暂无足够公开舆论样本”，不得臆测舆论方向。
禁止使用“值得关注、持续观察、快速发展、赋能行业、意义重大、加强布局”等没有对象、指标或动作的空话；如果证据不足就明确写“证据不足”。
输出字段：
headline：本周唯一主线，一句话，必须包含具体对象或变化；
thesis：180-320字，只写“发生了什么 → 为什么重要 → 对业务的直接含义”，至少包含2个具体信号对象；
events：3-5项，每项含 title、verdict、facts、sourceSynthesis、publicReaction、refs；每项对应一个关键事件，优先选择有多个来源或有明显舆论反馈的事件；refs 只能使用方括号中的 recordId；
areas：仅作为兼容字段，3-6项，每项含 cat、title、insight、evidence、refs；不要写空泛的领域趋势；refs 只能使用方括号中的 recordId；
topSignals：最重要的3-5个 recordId；
keyChanges：2-4项，每项含 title、change、evidence、refs；只写有明确证据的变化。
指标与图表数据由程序计算，不要在正文改写或新增统计数字。
上期周报上下文（仅用于识别变化，不得把上期结论当作本期事实）：{previous_context}
管理员额外关注 recordId：{pending_note}
确定性指标：{json.dumps(metrics, ensure_ascii=False)}
信号：
{numbered}"""
    return report._llm_json(prompt)


def validate_synthesis(
    raw: dict[str, Any], valid_ids: set[str], fallback_ids: list[str]
) -> dict[str, Any]:
    def strings(name: str, limit: int = 5) -> list[str]:
        return [
            str(item).strip()
            for item in (raw.get(name) or [])
            if str(item).strip()
        ][:limit]

    areas: list[dict[str, Any]] = []
    for item in raw.get("areas") or []:
        if not isinstance(item, dict):
            continue
        refs = [str(ref) for ref in item.get("refs") or [] if str(ref) in valid_ids]
        text = str(item.get("text") or "").strip()
        if not text:
            text = "；".join(
                str(item.get(key) or "").strip()
                for key in ("insight", "evidence", "implication", "action")
                if str(item.get(key) or "").strip()
            )
        category = str(item.get("cat") or "").strip()
        if text and category:
            areas.append({
                "cat": category,
                "title": str(item.get("title") or category).strip(),
                "text": text,
                "insight": str(item.get("insight") or "").strip(),
                "evidence": str(item.get("evidence") or "").strip(),
                "confidence": str(item.get("confidence") or "medium").strip().lower(),
                "refs": refs[:8],
            })
    top = [str(ref) for ref in raw.get("topSignals") or [] if str(ref) in valid_ids]
    top = list(dict.fromkeys(top))[:5] or fallback_ids[:5]
    headline = str(raw.get("headline") or "").strip()
    thesis = str(raw.get("thesis") or "").strip()
    if not headline or not thesis or not areas:
        raise RuntimeError("周报 LLM 输出缺少 headline、thesis 或 areas")
    return {
        "headline": headline,
        "thesis": thesis,
        "areas": areas[:6],
        "events": [
            {
                "title": str(item.get("title") or "").strip(),
                "verdict": str(item.get("verdict") or "").strip(),
                "facts": str(item.get("facts") or "").strip(),
                "sourceSynthesis": str(item.get("sourceSynthesis") or "").strip(),
                "publicReaction": str(item.get("publicReaction") or "").strip(),
                "refs": [str(ref) for ref in item.get("refs") or [] if str(ref) in valid_ids][:8],
            }
            for item in (raw.get("events") or [])
            if isinstance(item, dict) and str(item.get("title") or "").strip() and str(item.get("verdict") or "").strip()
        ][:5],
        "topSignals": top,
        "keyChanges": [
            {
                "title": str(item.get("title") or "").strip(),
                "change": str(item.get("change") or "").strip(),
                "evidence": str(item.get("evidence") or "").strip(),
                "refs": [str(ref) for ref in item.get("refs") or [] if str(ref) in valid_ids][:8],
            }
            for item in (raw.get("keyChanges") or [])
            if isinstance(item, dict) and str(item.get("title") or "").strip() and str(item.get("change") or "").strip()
        ][:4],
    }


def _upsert_weekly(
    token: str, table_id: str, payload: dict[str, Any]
) -> str:
    record = None
    for item in feishu.read_all_records_with_ids(token, table_id):
        if str(daily.scalar((item.get("fields") or {}).get("周报ID")) or "") == payload["weekId"]:
            record = item
            break
    # 完整 signals 含正文、媒体和聚合信息，写进一个飞书文本单元格会超过
    # 100KB 限制。表内保存周报结构与 recordId，网页所需完整快照留在 JSON。
    stored_payload = {key: value for key, value in payload.items() if key != "signals"}
    fields = {
        "周报ID": payload["weekId"],
        "周期开始": _day_ms(date.fromisoformat(payload["periodStart"])),
        "周期结束": _day_ms(date.fromisoformat(payload["periodEnd"])),
        "周报标题": payload["title"],
        "核心判断": payload["headline"],
        "综述": payload["thesis"],
        "周报内容": json.dumps(stored_payload, ensure_ascii=False),
        "信号记录ID": json.dumps(
            [signal["recordId"] for signal in payload["signals"]], ensure_ascii=False
        ),
        "额外关注记录ID": json.dumps(
            [item["recordId"] for item in payload["pendingFocus"]], ensure_ascii=False
        ),
        "状态": "已发布",
        "网页路径": f"/?page=tasks&tab=report&week={payload['weekId']}",
    }
    if record:
        feishu.update_record(token, table_id, str(record["record_id"]), fields)
        return str(record["record_id"])
    fields["发送状态"] = "待发送"
    return str(feishu.create_record(token, table_id, fields).get("record_id") or "")


def generate(end_day: date | None = None) -> dict[str, Any]:
    if not config.LLM_API_KEY:
        raise config.ConfigError("生成真实周报需要 LLM_API_KEY")
    start_day, end_day = week_window(end_day)
    current_week = week_id(end_day)
    token = feishu.get_tenant_access_token()
    weekly_table_id = _table_id(
        token, config.FEISHU_WEEKLY_TABLE_ID, feishu.ensure_weekly_report_table
    )
    pending_table_id = _table_id(
        token,
        config.FEISHU_WEEKLY_PENDING_TABLE_ID,
        feishu.ensure_weekly_pending_table,
    )
    params = feishu.read_param_records(token)
    records = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    records_by_id = {str(record.get("record_id") or ""): record for record in records}
    pending_records = read_pending(token, pending_table_id, current_week)
    pending_ids = {
        str(daily.scalar((item.get("fields") or {}).get("条目记录ID")) or "")
        for item in pending_records
    }
    pending_ids.discard("")
    ensure_pending_analyses(token, records_by_id, pending_records)
    candidates = collect_candidates(records, params, start_day, end_day, pending_ids)
    signals = [signal_from_candidate(item) for item in candidates]
    signals = [signal for signal in signals if signal]
    signals.sort(
        key=lambda signal: (
            float(signal.get("qualityScore") or 0),
            int(signal.get("impact") or 0),
            int(signal.get("novelty") or 0),
            int(signal.get("actionability") or 0),
        ),
        reverse=True,
    )
    signals = cluster.attach_aggregations(signals)
    if not signals:
        raise RuntimeError("近七天没有可用于周报的已分析信号")
    previous = _previous_weekly(token, weekly_table_id, current_week)
    metrics = deterministic_metrics(signals, previous)
    breakdowns = deterministic_breakdowns(signals)
    raw = synthesize(signals, metrics, pending_ids, previous)
    synthesized = validate_synthesis(
        raw,
        {str(signal["recordId"]) for signal in signals},
        [str(signal["recordId"]) for signal in signals],
    )
    pending_focus = [
        {
            "recordId": record_id,
            "titleCn": next(
                (
                    str(signal.get("titleCn") or signal.get("title") or "")
                    for signal in signals
                    if signal.get("recordId") == record_id
                ),
                "",
            ),
        }
        for record_id in sorted(pending_ids)
        if record_id in {str(signal["recordId"]) for signal in signals}
    ]
    payload: dict[str, Any] = {
        "weekId": current_week,
        "periodStart": start_day.isoformat(),
        "periodEnd": end_day.isoformat(),
        "period": f"{start_day.isoformat()} → {end_day.isoformat()}",
        "title": f"AI Signal 自动周报 · {current_week}",
        "metrics": metrics,
        "breakdowns": breakdowns,
        "signals": signals,
        "pendingFocus": pending_focus,
        "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        **synthesized,
    }
    weekly_record_id = _upsert_weekly(token, weekly_table_id, payload)
    payload["weeklyRecordId"] = weekly_record_id
    payload["weeklyTableId"] = weekly_table_id
    if pending_records:
        feishu.batch_update_records(
            token,
            pending_table_id,
            [
                {"record_id": str(item["record_id"]), "fields": {"状态": "已纳入"}}
                for item in pending_records
            ],
        )
    return payload


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="周期结束日期 YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--output", default="output/weekly-report.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    end_day = date.fromisoformat(args.date) if args.date else None
    payload = generate(end_day)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成 %s，共 %d 条信号", payload["weekId"], len(payload["signals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
