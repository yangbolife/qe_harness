"""eval_harness · 权限组 + 细粒度 ACL（N12）

Braintrust 的 permissions groups / service account 对应物。坚持「零信任、可审计」：
- Capability：细粒度能力（评审/发起运行/删除/管理/导出/管理员）。
- PolicyEngine：用户→能力集合、用户→权限组、服务账号（带密钥）三类主体。
- 行级 ACL：run 的 owner 与可见人（viewers）；非 owner 且无 EXPORT 能力不能看。
- require_capability：Web 依赖项，读 X-Harness-User 头，缺能力返回 403。

v1 不强制接入真实 IdP（O2 企业 SSO 另行负责）；本模块提供可单测的策略引擎，
服务账号用于 CI/自动化（O10 工具托管、O11 远程评测）的鉴权。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Cap(str, Enum):
    REVIEW = "review"          # 评审 / 看板查看
    CREATE_RUN = "create_run"  # 发起评测
    DELETE = "delete"          # 删除 run/看板
    MANAGE = "manage"          # 管理 rubric/工具/视图
    EXPORT = "export"          # 数据出库 / 检索
    ADMIN = "admin"            # 全部

    def implies(self, other: "Cap") -> bool:
        return self == other or self == Cap.ADMIN


# 默认权限组 → 能力集合
DEFAULT_GROUPS = {
    "viewer": {Cap.REVIEW},
    "engineer": {Cap.REVIEW, Cap.CREATE_RUN, Cap.EXPORT},
    "admin": {Cap.REVIEW, Cap.CREATE_RUN, Cap.DELETE, Cap.MANAGE, Cap.EXPORT, Cap.ADMIN},
}


class PolicyEngine:
    def __init__(self):
        self.users: dict[str, set[Cap]] = {}          # 显式用户能力
        self.groups: dict[str, set[Cap]] = {k: set(v) for k, v in DEFAULT_GROUPS.items()}
        self.user_groups: dict[str, str] = {}         # user → group 名
        self.service_accounts: dict[str, set[Cap]] = {}  # token → 能力

    # ---- 主体管理 ----
    def grant(self, user: str, cap: Cap):
        self.users.setdefault(user, set()).add(cap)

    def assign_group(self, user: str, group: str):
        if group not in self.groups:
            raise KeyError(f"未知权限组：{group}")
        self.user_groups[user] = group

    def add_service_account(self, token: str, caps: set[Cap]):
        self.service_accounts[token] = set(caps)

    # ---- 判定 ----
    def _caps_of(self, principal: str) -> set[Cap]:
        caps: set[Cap] = set()
        if principal in self.service_accounts:
            caps |= self.service_accounts[principal]
        if principal in self.users:
            caps |= self.users[principal]
        g = self.user_groups.get(principal)
        if g:
            caps |= self.groups[g]
        return caps

    def can(self, principal: str, cap: Cap) -> bool:
        for c in self._caps_of(principal):
            if c.implies(cap):
                return True
        return False

    def require(self, principal: str, cap: Cap) -> None:
        if not self.can(principal, cap):
            raise PermissionError(f"主体 {principal} 缺少能力 {cap.value}")

    # ---- 行级 ACL ----
    def can_view_run(self, principal: str, run_owner: Optional[str], viewers: set[str]) -> bool:
        if self.can(principal, Cap.ADMIN):
            return True
        if run_owner is None:
            return True  # 无主 run 默认可见
        if principal == run_owner:
            return True
        return principal in (viewers or set())

    def can_export(self, principal: str) -> bool:
        return self.can(principal, Cap.EXPORT)


# 全局默认引擎（可被测试/启动初始化替换）
_default_engine = PolicyEngine()


def get_engine() -> PolicyEngine:
    return _default_engine


def set_engine(eng: PolicyEngine):
    global _default_engine
    _default_engine = eng


def caller_from_headers(headers: dict) -> str:
    """从 HTTP 头解析调用主体（X-Harness-User 或 Bearer token）。"""
    user = headers.get("x-harness-user") or headers.get("X-Harness-User")
    if user:
        return user
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return "anonymous"
