"""公共与个人数据的归属约束。

这个模块刻意不读取 HTTP 请求或环境变量。调用 API 时必须先由认证层拿到可信的
user_id，再把它传进数据层；不能相信浏览器提交的 owner_user_id。
"""
from __future__ import annotations

PUBLIC_SCOPE = "public"
PERSONAL_SCOPE = "personal"
SCOPES = frozenset({PUBLIC_SCOPE, PERSONAL_SCOPE})


class ScopeError(ValueError):
    """数据归属不合法，或个人数据缺少所属用户。"""


def normalize_scope(scope_type: str = PUBLIC_SCOPE, owner_user_id: str | None = None) -> tuple[str, str | None]:
    scope = str(scope_type or PUBLIC_SCOPE).strip().lower()
    owner = str(owner_user_id or "").strip() or None
    if scope not in SCOPES:
        raise ScopeError(f"未知数据范围：{scope_type}")
    if scope == PUBLIC_SCOPE:
        if owner:
            raise ScopeError("公共数据不能指定 owner_user_id")
        return scope, None
    if not owner:
        raise ScopeError("个人数据必须指定 owner_user_id")
    return scope, owner


def personal_owner(user_id: str) -> str:
    """为仅允许个人数据的方法统一校验 owner。"""
    _, owner = normalize_scope(PERSONAL_SCOPE, user_id)
    assert owner is not None
    return owner
