"""Copy all configured Feishu tables into the MySQL records store.

Usage: DB_BACKEND=feishu python -m tools.migrate_feishu_to_mysql
Then switch DB_BACKEND=mysql after verifying row counts.
"""
from __future__ import annotations

from src import config, feishu, mysql_backend


TABLES = (
    ("FEISHU_PARAM_TABLE_ID", "一级参数"),
    ("FEISHU_ENTRY_TABLE_ID", "条目表"),
    ("FEISHU_PAPER_CONFIG_TABLE_ID", "二级参数-论文"),
    ("FEISHU_WECHAT_CONFIG_TABLE_ID", "二级参数-公众号"),
    ("FEISHU_VIDEO_CONFIG_TABLE_ID", "二级参数-视频"),
    ("FEISHU_SOCIAL_CONFIG_TABLE_ID", "二级参数-社媒"),
    ("FEISHU_GITHUB_CONFIG_TABLE_ID", "二级参数-GitHub"),
    ("FEISHU_BRIEF_TABLE_ID", "每日简报"),
    ("FEISHU_WEEKLY_TABLE_ID", "AI 周报"),
    ("FEISHU_WEEKLY_PENDING_TABLE_ID", "周报待分析"),
    ("FEISHU_TRACKED_ENTITY_TABLE_ID", "追踪对象"),
    ("FEISHU_TRACKED_EVENT_TABLE_ID", "追踪事件"),
)


def main() -> int:
    if config.DB_BACKEND == "mysql":
        raise SystemExit("迁移时请设置 DB_BACKEND=feishu，完成后再切换为 mysql")
    token = feishu.get_tenant_access_token()
    mysql_backend.init_schema()
    total = 0
    for env_name, label in TABLES:
        table_id = getattr(config, env_name, "")
        if not table_id:
            continue
        rows = feishu.read_all_records_with_ids(token, table_id)
        for row in rows:
            # Keep the original table ID as key; DB_BACKEND=mysql can then
            # reuse the same env values or fall back to logical names.
            mysql_backend.update_record("mysql", table_id, str(row.get("record_id") or ""), row.get("fields") or {})
        print(f"{label}: {len(rows)}")
        total += len(rows)
    print(f"迁移完成，共 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
