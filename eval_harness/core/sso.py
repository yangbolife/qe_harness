"""eval_harness · 企业合规 SSO（O2）

Braintrust 的「企业 SSO / 合规接入」对应物。坚持「等保可审计、协议可离线测试」：
- OIDCClient：标准 Authorization Code 流程（auth_url → 交换 code → 验签 id_token → 解析 claims）。
  * http 调用与 token 验签均可注入（http_client / verifier），无需真实 IdP 即可确定性测试。
- map_claims_to_group：把 IdP 返回的 claims（role / groups / email 域名）映射到内部 RBAC 组
  （viewer / engineer / admin），纯函数、可单测。
- ComplianceAudit：等保要求的「操作留痕」——登录/换组/评测触发均落审计表，可被监管抽查。

默认不强制接入真实 IdP（与 N12 一致：策略引擎可独立单测）；本模块提供协议骨架 + 可审计钩子。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .rbac import Cap, PolicyEngine


# ----------------------------------------------------------------------------
# claim → 内部 RBAC 组映射（纯函数，可单测）
# ----------------------------------------------------------------------------
def default_group_mapping() -> dict:
    """默认映射规则（按优先级从高到低评估）。

    - claims["role"] / claims["groups"] 命中 admin/engineer/viewer 关键词 → 对应组
    - email 域名命中管理员域名 → admin
    - 其余 → viewer（最小权限）
    """
    return {
        "admin_keywords": ["admin", "administrator", "root", "superuser"],
        "engineer_keywords": ["engineer", "dev", "developer", "qe", "evaluator", "sre"],
        "admin_domains": ["corp.example.com", "qualengine.com"],
    }


def map_claims_to_group(claims: dict, mapping: Optional[dict] = None) -> str:
    """把 IdP claims 映射为内部 RBAC 组名（viewer/engineer/admin）。"""
    m = mapping or default_group_mapping()
    role = str(claims.get("role", "") or "").lower()
    groups = [str(g).lower() for g in (claims.get("groups") or [])]
    email = str(claims.get("email", "") or "").lower()
    domain = email.split("@")[-1] if "@" in email else ""

    admin_kw = set(m.get("admin_keywords", []))
    eng_kw = set(m.get("engineer_keywords", []))

    # 管理员判定
    if role in admin_kw or any(g in admin_kw for g in groups) or domain in set(m.get("admin_domains", [])):
        return "admin"
    # 工程师判定
    if role in eng_kw or any(g in eng_kw for g in groups):
        return "engineer"
    return "viewer"


def claims_to_capabilities(claims: dict, mapping: Optional[dict] = None) -> set:
    """claim 解析后，直接给出该主体的 RBAC 能力集合（便于 SSO 登录后即时鉴权）。"""
    from .rbac import DEFAULT_GROUPS
    group = map_claims_to_group(claims, mapping)
    return set(DEFAULT_GROUPS.get(group, DEFAULT_GROUPS["viewer"]))


# ----------------------------------------------------------------------------
# 审计钩子（等保留痕）
# ----------------------------------------------------------------------------
class AuditSink:
    """审计落地接口（可注入内存/DB/外部 SIEM）。"""

    def record(self, event_type: str, principal: str, detail: str = "",
               ip: str = "", ok: bool = True):
        raise NotImplementedError


class MemoryAuditSink(AuditSink):
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event_type: str, principal: str, detail: str = "",
               ip: str = "", ok: bool = True):
        self.events.append({
            "event_type": event_type, "principal": principal, "detail": detail,
            "ip": ip, "ok": ok, "ts": time.time(),
        })

    def query(self, event_type: Optional[str] = None, principal: Optional[str] = None) -> list:
        out = self.events
        if event_type:
            out = [e for e in out if e["event_type"] == event_type]
        if principal:
            out = [e for e in out if e["principal"] == principal]
        return out


# ----------------------------------------------------------------------------
# OIDC 客户端（Authorization Code 流程）
# ----------------------------------------------------------------------------
class OIDCClient:
    """标准 OIDC Authorization Code 客户端。

    http_client：具备 .get(url, params) / .post(url, json=) 的对象（测试注入 fake）。
    verifier：callable(token) -> claims（测试注入 fake；生产用 PyJWT 验签）。
    """

    def __init__(self, issuer: str, client_id: str, client_secret: str,
                 redirect_uri: str, scopes: Optional[list] = None,
                 http_client=None, verifier: Optional[Callable[[str], dict]] = None):
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or ["openid", "profile", "email"]
        self.http_client = http_client
        self.verifier = verifier
        self._mapping = default_group_mapping()

    def authorization_url(self, state: str = "state", nonce: str = "") -> str:
        scope = "+".join(self.scopes)
        return (f"{self.issuer}/auth?response_type=code&client_id={self.client_id}"
                f"&redirect_uri={self.redirect_uri}&scope={scope}&state={state}"
                f"&nonce={nonce}")

    def exchange_code(self, code: str) -> dict:
        """用 code 换 token（真实走 token 端点；测试走注入 http_client）。"""
        if self.http_client is None:
            raise RuntimeError("未配置 http_client，无法真实交换 code（测试应注入 fake）")
        resp = self.http_client.post(
            f"{self.issuer}/token",
            json={"grant_type": "authorization_code", "code": code,
                  "client_id": self.client_id, "client_secret": self.client_secret,
                  "redirect_uri": self.redirect_uri},
        )
        return resp  # 期望 {"id_token": "...", ...}

    def verify_id_token(self, token: str) -> dict:
        """验签并解析 id_token → claims（默认用注入 verifier）。"""
        if self.verifier is None:
            raise RuntimeError("未配置 verifier，无法验签（生产用 PyJWT / 测试注入 fake）")
        return self.verifier(token)

    def handle_callback(self, code: str, audit: Optional[AuditSink] = None,
                        ip: str = "") -> dict:
        """完整回调：换 token → 验签 → 映射组 → 审计留痕。

        返回 {principal, group, claims, capabilities}。
        """
        tokens = self.exchange_code(code)
        id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
        if not id_token:
            if audit:
                audit.record("sso_login", "?", detail="no id_token", ip=ip, ok=False)
            raise ValueError("IdP 未返回 id_token")
        claims = self.verify_id_token(id_token)
        group = map_claims_to_group(claims, self._mapping)
        principal = claims.get("sub") or claims.get("email") or claims.get("preferred_username") or "anonymous"
        if audit:
            audit.record("sso_login", principal, detail=f"group={group}", ip=ip, ok=True)
        return {
            "principal": principal, "group": group, "claims": claims,
            "capabilities": [c.value for c in claims_to_capabilities(claims, self._mapping)],
        }


# ----------------------------------------------------------------------------
# SSO 登录后把 claim 组写入策略引擎（联动 N12）
# ----------------------------------------------------------------------------
def apply_sso_principal(engine: PolicyEngine, principal: str, group: str) -> None:
    """把 SSO 登录得到的主体 + 组写入 RBAC 策略引擎（幂等，重复登录覆盖组）。"""
    if group not in engine.groups:
        # 未知组不降级为 viewer（保持最小权限语义），但允许写入为自定义组
        engine.groups.setdefault(group, set())
    engine.assign_group(principal, group)
