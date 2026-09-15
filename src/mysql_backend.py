"""MySQL backend implementing the small record API used by the pipeline.

Records keep their original field names in a JSON column, making migration
lossless while allowing normal MySQL backups and deployment.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

try:
    import pymysql
except ImportError as exc:  # pragma: no cover
    pymysql = None
    _IMPORT_ERROR = exc


class MySQLError(RuntimeError):
    pass


def _conn(database: str | None = None, *, server_only: bool = False):
    if pymysql is None:
        raise MySQLError("缺少 pymysql 依赖，请安装 requirements.txt") from _IMPORT_ERROR
    return pymysql.connect(
        host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        **({"database": database or config.MYSQL_DATABASE} if not server_only else {}),
        charset=config.MYSQL_CHARSET, connect_timeout=config.MYSQL_CONNECT_TIMEOUT,
        autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    )


def init_schema() -> None:
    conn = _conn(server_only=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{config.MYSQL_DATABASE}` CHARACTER SET utf8mb4")
        conn.select_db(config.MYSQL_DATABASE)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS records (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                table_key VARCHAR(128) NOT NULL,
                record_id VARCHAR(128) NOT NULL,
                fields JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_table_record (table_key, record_id),
                KEY idx_table (table_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sources (
                source_id VARCHAR(128) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                fetch_method VARCHAR(32) NOT NULL,
                endpoint TEXT,
                priority VARCHAR(8),
                dimension VARCHAR(64),
                dedup_key VARCHAR(255),
                lookback_window VARCHAR(32),
                keyword_regex TEXT,
                title_exclude_regex TEXT,
                min_content_chars INT,
                extra_config JSON,
                collect_stats JSON,
                cursor_state JSON,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                KEY idx_sources_status (status), KEY idx_sources_method (fetch_method)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS signals (
                signal_id VARCHAR(128) PRIMARY KEY,
                source_id VARCHAR(128), title TEXT, url TEXT, published_at BIGINT,
                summary TEXT, body MEDIUMTEXT, signal_format VARCHAR(64),
                impact_score DECIMAL(8,2), content_category VARCHAR(64),
                fields JSON NOT NULL, created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                KEY idx_signals_source_date (source_id, published_at), KEY idx_signals_score (impact_score)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    finally:
        conn.close()


def _table_key(table_id: str) -> str:
    return str(table_id or "default")[:128]


def _scalar(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0].get("text") if isinstance(value[0], dict) else value[0]
    if isinstance(value, dict):
        return value.get("text") or value.get("link") or value.get("value")
    return value


def _sync_normalized(table_id: str, record_id: str, fields: dict[str, Any], cur) -> None:
    if _table_key(table_id) == _table_key(config.FEISHU_PARAM_TABLE_ID):
        sid = str(_scalar(fields.get("source_id") or record_id) or "").strip()
        if not sid: return
        values = [sid, str(_scalar(fields.get("name")) or sid), str(_scalar(fields.get("status")) or "active"), str(_scalar(fields.get("fetch_method")) or ""), _scalar(fields.get("endpoint")), _scalar(fields.get("priority")), _scalar(fields.get("dimension")), _scalar(fields.get("dedup_key")), _scalar(fields.get("lookback_window")), _scalar(fields.get("keyword_regex")), _scalar(fields.get("title_exclude_regex")), _scalar(fields.get("min_content_chars")) or None, fields.get("extra_config"), json.dumps({k: fields.get(k) for k in ("通过", "条目数", "查重过滤", "时间窗过滤", "最近采集时间") if k in fields}, ensure_ascii=False), fields.get("采集游标")]
        cur.execute("""INSERT INTO sources(source_id,name,status,fetch_method,endpoint,priority,dimension,dedup_key,lookback_window,keyword_regex,title_exclude_regex,min_content_chars,extra_config,collect_stats,cursor_state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),status=VALUES(status),fetch_method=VALUES(fetch_method),endpoint=VALUES(endpoint),priority=VALUES(priority),dimension=VALUES(dimension),dedup_key=VALUES(dedup_key),lookback_window=VALUES(lookback_window),keyword_regex=VALUES(keyword_regex),title_exclude_regex=VALUES(title_exclude_regex),min_content_chars=VALUES(min_content_chars),extra_config=VALUES(extra_config),collect_stats=VALUES(collect_stats),cursor_state=VALUES(cursor_state)""", values)
    elif _table_key(table_id) == _table_key(config.FEISHU_ENTRY_TABLE_ID):
        val = lambda *keys: next((_scalar(fields.get(k)) for k in keys if fields.get(k) is not None), None)
        cur.execute("""INSERT INTO signals(signal_id,source_id,title,url,published_at,summary,body,signal_format,impact_score,content_category,fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE source_id=VALUES(source_id),title=VALUES(title),url=VALUES(url),published_at=VALUES(published_at),summary=VALUES(summary),body=VALUES(body),signal_format=VALUES(signal_format),impact_score=VALUES(impact_score),content_category=VALUES(content_category),fields=VALUES(fields)""", (str(record_id), val("source_id"), val("标题", "中文标题"), val("链接", "url"), val("发布时间"), val("摘要", "中文摘要"), val("原文", "中文正文"), val("来源类型", "路由来源"), val("影响分", "质量分"), val("内容分类"), json.dumps(fields, ensure_ascii=False, default=str)))


def _rows(table_id: str, with_ids: bool = False) -> list[dict[str, Any]]:
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT record_id, fields FROM records WHERE table_key=%s ORDER BY id", (_table_key(table_id),))
            rows = cur.fetchall()
        out = []
        for row in rows:
            fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
            out.append({"record_id": row["record_id"], "fields": fields} if with_ids else fields)
        return out
    finally:
        conn.close()


def get_tenant_access_token() -> str:
    return "mysql"


def read_param_records(token: str) -> list[dict[str, Any]]:
    return _rows(config.FEISHU_PARAM_TABLE_ID, True)


def read_all_records(token: str, table_id: str, field_names: list[str] | None = None) -> list[dict[str, Any]]:
    rows = _rows(table_id)
    if field_names:
        return [{k: row.get(k) for k in field_names} for row in rows]
    return rows


def read_all_records_with_ids(token: str, table_id: str, field_names: list[str] | None = None) -> list[dict[str, Any]]:
    rows = _rows(table_id, True)
    if field_names:
        for row in rows:
            row["fields"] = {k: row["fields"].get(k) for k in field_names}
    return rows


def read_existing_dedup_keys(token: str) -> set[str]:
    return {str(row.get("去重键")).strip() for row in _rows(config.FEISHU_ENTRY_TABLE_ID) if row.get("去重键")}


def create_record(token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    init_schema(); rid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO records(table_key,record_id,fields) VALUES(%s,%s,%s)", (_table_key(table_id), rid, json.dumps(fields, ensure_ascii=False, default=str)))
            _sync_normalized(table_id, rid, fields, cur)
    finally: conn.close()
    return {"record_id": rid, "fields": fields}


def batch_create_table_records(token: str, table_id: str, fields_list: list[dict[str, Any]], chunk: int = 100) -> int:
    for fields in fields_list: create_record(token, table_id, fields)
    return len(fields_list)


def batch_create_records(token: str, fields_list: list[dict[str, Any]], chunk: int = 100) -> int:
    return batch_create_table_records(token, config.FEISHU_ENTRY_TABLE_ID, fields_list, chunk)


def update_record(token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    init_schema(); conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fields FROM records WHERE table_key=%s AND record_id=%s", (_table_key(table_id), str(record_id)))
            row = cur.fetchone()
            merged = (row["fields"] if row else {})
            if not isinstance(merged, dict): merged = json.loads(merged)
            merged.update(fields)
            cur.execute("INSERT INTO records(table_key,record_id,fields) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE fields=VALUES(fields)", (_table_key(table_id), str(record_id), json.dumps(merged, ensure_ascii=False, default=str)))
            _sync_normalized(table_id, str(record_id), merged, cur)
            return {"record_id": str(record_id), "fields": merged}
    finally: conn.close()


def batch_update_records(token: str, table_id: str, updates: list[dict[str, Any]], chunk: int = 100) -> int:
    for item in updates: update_record(token, table_id, item["record_id"], item.get("fields") or {})
    return len(updates)


def delete_record(token: str, table_id: str, record_id: str) -> None:
    init_schema(); conn = _conn()
    try:
        with conn.cursor() as cur: cur.execute("DELETE FROM records WHERE table_key=%s AND record_id=%s", (_table_key(table_id), str(record_id)))
    finally: conn.close()


def batch_delete_records(token: str, table_id: str, record_ids: list[str], chunk: int = 500) -> int:
    for rid in record_ids: delete_record(token, table_id, rid)
    return len(record_ids)


def sync_param_collect_stats(token, param_records, attempted_ids, cleaned_items, final_items, time_window_counts=None):
    from collections import Counter
    ids = {str(x) for x in attempted_ids if x}
    cleaned = Counter(str(x.get("source_id") or "") for x in cleaned_items)
    final = Counter(str(x.get("source_id") or "") for x in final_items)
    count = 0
    for rec in param_records:
        fields = rec.get("fields") or {}; sid = str(fields.get("source_id") or "")
        if sid in ids:
            update_record(token, config.FEISHU_PARAM_TABLE_ID, rec.get("record_id") or sid, {"通过": cleaned[sid] > 0, "最近采集时间": int(__import__('time').time()*1000), "条目数": final[sid], "查重过滤": max(cleaned[sid]-final[sid], 0)})
            count += 1
    return count


def update_social_cursor_states(token, feeds, cursor_states):
    updates = []
    for feed in feeds:
        sid = str(feed.get("id") or "")
        rid = str(feed.get("record_id") or sid)
        if sid in cursor_states:
            updates.append({"record_id": rid, "fields": {"采集游标": json.dumps(cursor_states[sid], ensure_ascii=False, separators=(",", ":"))}})
    return batch_update_records(token, config.FEISHU_PARAM_TABLE_ID, updates)


def ensure_daily_brief_table(token): return config.FEISHU_BRIEF_TABLE_ID or "每日简报"
def ensure_weekly_report_table(token): return config.FEISHU_WEEKLY_TABLE_ID or "AI 周报"
def ensure_weekly_pending_table(token): return config.FEISHU_WEEKLY_PENDING_TABLE_ID or "周报待分析"
def ensure_tracked_entity_table(token): return config.FEISHU_TRACKED_ENTITY_TABLE_ID or "追踪对象"
def ensure_tracked_event_table(token): return config.FEISHU_TRACKED_EVENT_TABLE_ID or "追踪事件"


if __name__ == "__main__":
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for table_id in (config.FEISHU_PARAM_TABLE_ID, config.FEISHU_ENTRY_TABLE_ID):
                cur.execute("SELECT record_id, fields FROM records WHERE table_key=%s", (_table_key(table_id),))
                for row in cur.fetchall():
                    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
                    _sync_normalized(table_id, row["record_id"], fields, cur)
    finally:
        conn.close()
    print(f"MySQL schema ready: {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE}")
