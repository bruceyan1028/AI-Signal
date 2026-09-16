"""MySQL backend implementing the small record API used by the pipeline.

Records keep their original field names in a JSON column, making migration
lossless while allowing normal MySQL backups and deployment.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from . import config, data_scope

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
                scope_type ENUM('public','personal') NOT NULL DEFAULT 'public',
                owner_user_id VARCHAR(128) NOT NULL DEFAULT '',
                fields JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_table_record_scope (table_key, record_id, scope_type, owner_user_id),
                KEY idx_table_scope (table_key, scope_type, owner_user_id)
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
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(128) PRIMARY KEY,
                display_name VARCHAR(255),
                email VARCHAR(255),
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS user_preferences (
                user_id VARCHAR(128) PRIMARY KEY,
                preferences JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                CONSTRAINT fk_preferences_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
                subscription_id CHAR(36) PRIMARY KEY,
                user_id VARCHAR(128) NOT NULL,
                source_id VARCHAR(128) NULL,
                query_rule JSON NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                KEY idx_subscriptions_user (user_id, enabled),
                CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS user_signal_state (
                user_id VARCHAR(128) NOT NULL,
                signal_id VARCHAR(128) NOT NULL,
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                is_saved BOOLEAN NOT NULL DEFAULT FALSE,
                tags JSON NOT NULL,
                note TEXT,
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                PRIMARY KEY (user_id, signal_id),
                KEY idx_user_signal_saved (user_id, is_saved, updated_at),
                CONSTRAINT fk_signal_state_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS media_assets (
                media_asset_id CHAR(36) PRIMARY KEY,
                scope_type ENUM('public','personal') NOT NULL DEFAULT 'public',
                owner_user_id VARCHAR(128) NOT NULL DEFAULT '',
                storage_url TEXT NOT NULL,
                content_type VARCHAR(128),
                byte_size BIGINT UNSIGNED,
                content_hash CHAR(64),
                metadata JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_media_scope_hash (scope_type, owner_user_id, content_hash),
                KEY idx_media_owner (scope_type, owner_user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            _migrate_scope_columns(cur)
    finally:
        conn.close()


def _table_key(table_id: str) -> str:
    return str(table_id or "default")[:128]


def _migrate_scope_columns(cur) -> None:
    """为已存在的 records 表追加范围字段；历史记录默认就是公共数据。"""
    cur.execute("""SELECT COLUMN_NAME FROM information_schema.columns
        WHERE table_schema=DATABASE() AND table_name='records'""")
    columns = {row["COLUMN_NAME"] for row in cur.fetchall()}
    if "scope_type" not in columns:
        cur.execute("ALTER TABLE records ADD COLUMN scope_type ENUM('public','personal') NOT NULL DEFAULT 'public' AFTER record_id")
    if "owner_user_id" not in columns:
        cur.execute("ALTER TABLE records ADD COLUMN owner_user_id VARCHAR(128) NOT NULL DEFAULT '' AFTER scope_type")
    else:
        cur.execute("UPDATE records SET owner_user_id='' WHERE owner_user_id IS NULL")
        cur.execute("ALTER TABLE records MODIFY owner_user_id VARCHAR(128) NOT NULL DEFAULT ''")
    cur.execute("""SELECT INDEX_NAME FROM information_schema.statistics
        WHERE table_schema=DATABASE() AND table_name='records'""")
    indexes = {row["INDEX_NAME"] for row in cur.fetchall()}
    if "idx_table_scope" not in indexes:
        cur.execute("CREATE INDEX idx_table_scope ON records(table_key, scope_type, owner_user_id)")
    if "uq_table_record" in indexes:
        cur.execute("ALTER TABLE records DROP INDEX uq_table_record")
    if "uq_table_record_scope" not in indexes:
        cur.execute("ALTER TABLE records ADD UNIQUE KEY uq_table_record_scope (table_key, record_id, scope_type, owner_user_id)")


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


def _rows(
    table_id: str,
    with_ids: bool = False,
    *,
    scope_type: str = data_scope.PUBLIC_SCOPE,
    owner_user_id: str | None = None,
) -> list[dict[str, Any]]:
    scope, owner = data_scope.normalize_scope(scope_type, owner_user_id)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT record_id, fields FROM records
                WHERE table_key=%s AND scope_type=%s AND owner_user_id=%s ORDER BY id""",
                (_table_key(table_id), scope, owner or ""),
            )
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
    """公共采集配置。运行流水线不读取个人配置。"""
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


def create_scoped_record(
    table_id: str,
    fields: dict[str, Any],
    *,
    scope_type: str = data_scope.PUBLIC_SCOPE,
    owner_user_id: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    """创建有归属的记录；个人调用者必须传认证层确认过的 user_id。"""
    scope, owner = data_scope.normalize_scope(scope_type, owner_user_id)
    init_schema()
    rid = str(record_id or uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO records(table_key,record_id,scope_type,owner_user_id,fields)
                VALUES(%s,%s,%s,%s,%s)""",
                (_table_key(table_id), rid, scope, owner or "", json.dumps(fields, ensure_ascii=False, default=str)),
            )
            if scope == data_scope.PUBLIC_SCOPE:
                _sync_normalized(table_id, rid, fields, cur)
    finally:
        conn.close()
    return {"record_id": rid, "fields": fields}


def read_scoped_records(
    table_id: str,
    *,
    scope_type: str = data_scope.PUBLIC_SCOPE,
    owner_user_id: str | None = None,
    with_ids: bool = True,
) -> list[dict[str, Any]]:
    """按范围读取记录，不能通过缺失 owner 意外读取所有个人数据。"""
    return _rows(table_id, with_ids, scope_type=scope_type, owner_user_id=owner_user_id)


def create_record(token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return create_scoped_record(table_id, fields)


def batch_create_table_records(token: str, table_id: str, fields_list: list[dict[str, Any]], chunk: int = 100) -> int:
    for fields in fields_list: create_record(token, table_id, fields)
    return len(fields_list)


def batch_create_records(token: str, fields_list: list[dict[str, Any]], chunk: int = 100) -> int:
    return batch_create_table_records(token, config.FEISHU_ENTRY_TABLE_ID, fields_list, chunk)


def update_record(token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    init_schema(); conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fields FROM records WHERE table_key=%s AND record_id=%s AND scope_type='public' AND owner_user_id=''", (_table_key(table_id), str(record_id)))
            row = cur.fetchone()
            merged = (row["fields"] if row else {})
            if not isinstance(merged, dict): merged = json.loads(merged)
            merged.update(fields)
            cur.execute("INSERT INTO records(table_key,record_id,scope_type,owner_user_id,fields) VALUES(%s,%s,'public','',%s) ON DUPLICATE KEY UPDATE fields=VALUES(fields)", (_table_key(table_id), str(record_id), json.dumps(merged, ensure_ascii=False, default=str)))
            _sync_normalized(table_id, str(record_id), merged, cur)
            return {"record_id": str(record_id), "fields": merged}
    finally: conn.close()


def update_scoped_record(
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
    *,
    scope_type: str = data_scope.PERSONAL_SCOPE,
    owner_user_id: str,
) -> dict[str, Any]:
    """更新指定范围的记录；个人记录绝不允许借此落到公共范围。"""
    scope, owner = data_scope.normalize_scope(scope_type, owner_user_id)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT fields FROM records WHERE table_key=%s AND record_id=%s
                AND scope_type=%s AND owner_user_id=%s""",
                (_table_key(table_id), str(record_id), scope, owner or ""),
            )
            row = cur.fetchone()
            if not row:
                raise MySQLError("找不到该范围内的记录")
            merged = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
            merged.update(fields)
            cur.execute(
                """UPDATE records SET fields=%s WHERE table_key=%s AND record_id=%s
                AND scope_type=%s AND owner_user_id=%s""",
                (json.dumps(merged, ensure_ascii=False, default=str), _table_key(table_id), str(record_id), scope, owner or ""),
            )
            return {"record_id": str(record_id), "fields": merged}
    finally:
        conn.close()


def batch_update_records(token: str, table_id: str, updates: list[dict[str, Any]], chunk: int = 100) -> int:
    for item in updates: update_record(token, table_id, item["record_id"], item.get("fields") or {})
    return len(updates)


def delete_record(token: str, table_id: str, record_id: str) -> None:
    init_schema(); conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM records WHERE table_key=%s AND record_id=%s AND scope_type='public' AND owner_user_id=''", (_table_key(table_id), str(record_id)))
    finally: conn.close()


def batch_delete_records(token: str, table_id: str, record_ids: list[str], chunk: int = 500) -> int:
    for rid in record_ids: delete_record(token, table_id, rid)
    return len(record_ids)


def upsert_user(user_id: str, *, display_name: str = "", email: str = "") -> None:
    """认证层成功识别用户后创建或刷新其可展示资料。"""
    owner = data_scope.personal_owner(user_id)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users(user_id,display_name,email) VALUES(%s,%s,%s)
                ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),email=VALUES(email)""",
                (owner, str(display_name or ""), str(email or "")),
            )
    finally:
        conn.close()


def set_user_preferences(user_id: str, preferences: dict[str, Any]) -> None:
    owner = data_scope.personal_owner(user_id)
    upsert_user(owner)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_preferences(user_id,preferences) VALUES(%s,%s)
                ON DUPLICATE KEY UPDATE preferences=VALUES(preferences)""",
                (owner, json.dumps(preferences, ensure_ascii=False, default=str)),
            )
    finally:
        conn.close()


def get_user_preferences(user_id: str) -> dict[str, Any]:
    owner = data_scope.personal_owner(user_id)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT preferences FROM user_preferences WHERE user_id=%s", (owner,))
            row = cur.fetchone()
            value = (row or {}).get("preferences") or {}
            return value if isinstance(value, dict) else json.loads(value)
    finally:
        conn.close()


def upsert_user_signal_state(
    user_id: str,
    signal_id: str,
    *,
    is_read: bool = False,
    is_saved: bool = False,
    tags: list[str] | None = None,
    note: str = "",
) -> None:
    owner = data_scope.personal_owner(user_id)
    upsert_user(owner)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_signal_state(user_id,signal_id,is_read,is_saved,tags,note)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE is_read=VALUES(is_read),is_saved=VALUES(is_saved),
                tags=VALUES(tags),note=VALUES(note)""",
                (owner, str(signal_id), bool(is_read), bool(is_saved), json.dumps(tags or [], ensure_ascii=False), str(note or "")),
            )
    finally:
        conn.close()


def create_subscription(
    user_id: str,
    *,
    source_id: str | None = None,
    query_rule: dict[str, Any] | None = None,
) -> str:
    """保存个人订阅规则；规则 JSON 可承载关键词、分类和通知偏好。"""
    owner = data_scope.personal_owner(user_id)
    upsert_user(owner)
    subscription_id = str(uuid.uuid4())
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO subscriptions(subscription_id,user_id,source_id,query_rule)
                VALUES(%s,%s,%s,%s)""",
                (subscription_id, owner, str(source_id or "") or None, json.dumps(query_rule or {}, ensure_ascii=False, default=str)),
            )
    finally:
        conn.close()
    return subscription_id


def list_subscriptions(user_id: str, *, enabled_only: bool = True) -> list[dict[str, Any]]:
    owner = data_scope.personal_owner(user_id)
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            sql = "SELECT subscription_id,source_id,query_rule,enabled,created_at,updated_at FROM subscriptions WHERE user_id=%s"
            if enabled_only:
                sql += " AND enabled=TRUE"
            sql += " ORDER BY created_at DESC"
            cur.execute(sql, (owner,))
            rows = cur.fetchall()
            for row in rows:
                if not isinstance(row["query_rule"], dict):
                    row["query_rule"] = json.loads(row["query_rule"])
            return rows
    finally:
        conn.close()


def register_media_asset(
    storage_url: str,
    *,
    scope_type: str = data_scope.PUBLIC_SCOPE,
    owner_user_id: str | None = None,
    content_type: str = "",
    byte_size: int | None = None,
    content_hash: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """记录对象存储引用，媒体二进制本身不进入 MySQL。"""
    scope, owner = data_scope.normalize_scope(scope_type, owner_user_id)
    url = str(storage_url or "").strip()
    if not url:
        raise MySQLError("storage_url 不能为空")
    asset_id = str(uuid.uuid4())
    init_schema()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO media_assets(media_asset_id,scope_type,owner_user_id,storage_url,content_type,byte_size,content_hash,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (asset_id, scope, owner or "", url, str(content_type or "") or None, byte_size, str(content_hash or "") or None, json.dumps(metadata or {}, ensure_ascii=False, default=str)),
            )
    finally:
        conn.close()
    return asset_id


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
                cur.execute(
                    """SELECT record_id, fields FROM records WHERE table_key=%s
                    AND scope_type='public' AND owner_user_id=''""",
                    (_table_key(table_id),),
                )
                for row in cur.fetchall():
                    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
                    _sync_normalized(table_id, row["record_id"], fields, cur)
    finally:
        conn.close()
    print(f"MySQL schema ready: {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE}")
