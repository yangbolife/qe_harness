"""O1 / O2 / O3 / O4 功能测试。

覆盖本轮收口的 4 个 Braintrust 缺口项：
- O1 国内模型 AI 网关（加权轮询 + 故障转移，离线注入 fake 后端）
- O2 企业合规 SSO（claim→RBAC 组映射 + 等保审计落库）
- O3 在线评分 + 告警（滚动指标 + 阈值触发）
- O4 版本化资产（prompt/function CRUD + push/pull 包 + 注册为工具）

DB 用工作区内临时目录，规避 broker 沙箱对系统 /tmp 的 mkdir 拦截。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from eval_harness.core.persistence import Database


def _tmp_db():
    d = tempfile.mkdtemp(prefix="feat3_")
    return f"sqlite+aiosqlite:///{d}/t.db"


async def _init(url):
    db = Database(url)
    await db.init()
    return db


# =================== O1 国内模型 AI 网关 ===================
def test_o1_qwen_zhipu_registered():
    from eval_harness.core.domestic_gateway import (
        DomesticGatewayProvider, QwenProvider, ZhipuProvider)
    from eval_harness.core.registry import PROVIDERS
    # 注册表已接入
    assert PROVIDERS.get("qwen") is QwenProvider
    assert PROVIDERS.get("zhipu") is ZhipuProvider
    assert PROVIDERS.get("domestic_gateway") is DomesticGatewayProvider
    # 实例化不触发网络（仅构造 OpenAI 客户端惰性）
    q = QwenProvider()
    z = ZhipuProvider()
    assert q.name == "qwen" and q.base_url.endswith("/compatible-mode/v1")
    assert z.name == "zhipu" and "bigmodel" in z.base_url


def test_o1_gateway_roundrobin_and_failover():
    from eval_harness.core.domestic_gateway import DomesticGatewayProvider
    from eval_harness.core.ratelimit import RetryableError

    class Fake:
        name = "fake"

        def __init__(self, ret, fail=False, weight=1):
            self.ret = ret
            self.fail = fail
            self.weight = weight
            self.n = 0

        def ask(self, prompt, gold=""):
            self.n += 1
            if self.fail:
                raise RuntimeError("boom")
            return self.ret

    a, b = Fake("A"), Fake("B")
    gw = DomesticGatewayProvider(backends=[a, b])
    # 加权轮询（等权 → A,B,A,...）
    assert gw.ask("x") == "A"
    assert gw.ask("x") == "B"
    assert gw.ask("x") == "A"

    # 故障转移：a 失败 → 自动用 b
    gw2 = DomesticGatewayProvider(backends=[Fake("A", fail=True), Fake("B")])
    assert gw2.ask("x") == "B"
    assert gw2.ask("x") == "B"

    # 加权：a 权重 2 → 2/3 流量给 a
    gw3 = DomesticGatewayProvider(backends=[Fake("A", weight=2), Fake("B", weight=1)])
    seq = [gw3.ask("x") for _ in range(6)]
    assert seq.count("A") == 4 and seq.count("B") == 2

    # 全部失败 → RetryableError
    gw4 = DomesticGatewayProvider(backends=[Fake("X", fail=True), Fake("Y", fail=True)])
    with pytest.raises(RetryableError):
        gw4.ask("x")


# =================== O2 企业合规 SSO ===================
def test_o2_claim_mapping_and_callback():
    from eval_harness.core.sso import (
        OIDCClient, map_claims_to_group, claims_to_capabilities, MemoryAuditSink)

    # claim → 组映射
    assert map_claims_to_group({"role": "admin"}) == "admin"
    assert map_claims_to_group({"groups": ["qe", "dev"]}) == "engineer"
    assert map_claims_to_group({"email": "a@corp.example.com"}) == "admin"   # 管理员域名
    assert map_claims_to_group({"email": "user@gmail.com"}) == "viewer"     # 最小权限
    assert "admin" in claims_to_capabilities({"role": "admin"})

    # 完整回调：注入 fake http + verifier + 审计 sink
    calls = {"exchanged": False}

    class FakeHttp:
        def post(self, url, json=None):
            calls["exchanged"] = True
            return {"id_token": "fake.jwt.token"}

    client = OIDCClient(
        issuer="https://idp.example.com", client_id="c", client_secret="s",
        redirect_uri="https://app/cb", http_client=FakeHttp(),
        verifier=lambda tok: {"sub": "u1@corp.example.com", "role": "admin",
                              "email": "u1@corp.example.com"})
    audit = MemoryAuditSink()
    res = client.handle_callback("code123", audit=audit, ip="10.0.0.1")
    assert calls["exchanged"] is True
    assert res["principal"] == "u1@corp.example.com"
    assert res["group"] == "admin"
    # admin 组授予全部能力（含 admin/manage/delete/create_run/review/export）
    assert set(res["capabilities"]) >= {"admin", "manage", "delete", "create_run", "review", "export"}
    assert len(audit.query("sso_login")) == 1
    assert audit.query("sso_login")[0]["ok"] is True


def test_o2_audit_persist():
    db_url = _tmp_db()
    db = asyncio.run(_init(db_url))
    try:
        aid = asyncio.run(db.record_audit("sso_login", "u1", "group=admin", ip="10.0.0.1", ok=True))
        assert aid > 0
        rows = asyncio.run(db.list_audit(event_type="sso_login"))
        assert len(rows) == 1
        assert rows[0]["principal"] == "u1" and rows[0]["ok"] is True
        allrows = asyncio.run(db.list_audit())
        assert len(allrows) == 1
    finally:
        asyncio.run(db.close())


# =================== O3 在线评分 + 告警 ===================
def test_o3_alerting_rules():
    from eval_harness.core.alerting import AlertRule, AlertManager, RollingMetrics

    m = RollingMetrics()
    for passed in [True, True, False, False, False]:
        m.update(passed, latency_ms=100.0)
    snap = m.snapshot()
    assert snap["total_calls"] == 5
    assert abs(snap["pass_rate"] - 0.4) < 1e-9

    am = AlertManager(rules=[AlertRule("pass_rate", "lt", 0.5, name="low_pass")])
    fired = am.evaluate(snap)
    assert len(fired) == 1 and fired[0].metric == "pass_rate"
    # 阈值未击穿不触发
    am2 = AlertManager(rules=[AlertRule("pass_rate", "ge", 0.9)])
    assert am2.evaluate(snap) == []


def test_o3_online_feed_alerts():
    from eval_harness.core.online import OnlineEvaluator
    from eval_harness.core.alerting import AlertRule

    ev = OnlineEvaluator(default_grader="code",
                          rules=[AlertRule("pass_rate", "lt", 0.5, name="low_pass")])
    # code grader：response 含 gold 即通过
    r1 = ev.feed("q", gold="2", response="结果是2")          # 通过
    r2 = ev.feed("q", gold="2", response="我不知道")          # 失败
    r3 = ev.feed("q", gold="2", response="算不出来")          # 失败
    r4 = ev.feed("q", gold="2", response="答案是2元")          # 通过
    assert r1["passed"] and not r2["passed"] and not r3["passed"] and r4["passed"]
    snap = ev.snapshot()
    assert abs(snap["pass_rate"] - 0.5) < 1e-9
    # 在线评测逐条触发：feed#3 时 pass_rate=0.333<0.5 → 已触发 1 次告警
    # （feed#4 把通过率拉回 0.5，不再触发新告警）
    assert len(ev.alerts.history) == 1
    assert ev.alerts.history[0].metric == "pass_rate"
    # 再喂一条失败 → 通过率 0.4 < 0.5 → 再触发 1 次（累计 2 次）
    ev.feed("q", gold="2", response="失败")
    assert len(ev.alerts.history) == 2
    assert ev.alerts.history[1].metric == "pass_rate"


# =================== O4 版本化资产 ===================
def test_o4_assets_crud_push_pull():
    from eval_harness.core import assets as asset_mod

    db_url = _tmp_db()
    db = asyncio.run(_init(db_url))
    try:
        # 保存 v1 / v2（内容不同 → 两版本）
        id1 = asyncio.run(asset_mod.save_asset(db, "prompt", "greet", "你好，{name}", version="v1"))
        id2 = asyncio.run(asset_mod.save_asset(db, "prompt", "greet", "您好，{name}！", version="v2"))
        assert id1 > 0 and id2 > 0
        # get latest
        a = asyncio.run(asset_mod.get_asset(db, "greet"))
        assert a.version == "v2" and a.content == "您好，{name}！"
        # get 指定版本
        a1 = asyncio.run(asset_mod.get_asset(db, "greet", "v1"))
        assert a1.content == "你好，{name}"
        # versions 列表
        vers = asyncio.run(asset_mod.asset_versions(db, "greet"))
        assert {v["version"] for v in vers} == {"v1", "v2"}
        # push 包
        bundle_path = os.path.join(tempfile.mkdtemp(prefix="feat3_"), "assets.json")
        exp = asyncio.run(asset_mod.export_bundle(db, bundle_path))
        assert exp["count"] == 2
        # 在空库 pull
        db2_url = _tmp_db()
        db2 = asyncio.run(_init(db2_url))
        try:
            imp = asyncio.run(asset_mod.import_bundle(db2, bundle_path))
            assert imp["added"] == 2
            pulled = asyncio.run(asset_mod.get_asset(db2, "greet", "v2"))
            assert pulled.content == "您好，{name}！"
        finally:
            asyncio.run(db2.close())
        # function 资产 → 注册为工具
        asyncio.run(asset_mod.save_asset(db, "function", "norm", "lambda x: x.strip().lower()"))
        name = asyncio.run(asset_mod.register_function_asset_as_tool(db, "norm"))
        assert name == "norm"
        from eval_harness.core.tools import get_registry
        assert any(t["name"] == "norm" for t in get_registry().available())
        # 删除
        n = asyncio.run(asset_mod.delete_asset(db, "greet"))
        assert n == 2
    finally:
        asyncio.run(db.close())


# =================== Web 端点（O2/O3/O4） ===================
def test_web_o1_o2_o3_o4_endpoints():
    prev = os.environ.get("EVAL_SSO_DEV")
    os.environ["EVAL_SSO_DEV"] = "1"   # 启用 SSO dev 模式（允许直接传 claims 登录）
    try:
        from fastapi.testclient import TestClient
        from eval_harness.web.app import create_app
        from eval_harness.core.rbac import set_engine, PolicyEngine

        db_url = _tmp_db()
        app = create_app(db_url)
        with TestClient(app) as c:
            # ---- O2：SSO dev 登录 + 回调 + 审计 ----
            login = c.post("/api/sso/login", json={"provider": "default"})
            assert login.status_code == 200 and login.json()["dev_mode"] is True
            cb = c.post("/api/sso/callback",
                        json={"claims": {"sub": "ops@corp.example.com", "role": "admin"}})
            assert cb.status_code == 200
            assert cb.json()["group"] == "admin"
            aud = c.get("/api/audit")
            assert aud.status_code == 200 and any(
                e["event_type"] == "sso_login" for e in aud.json()["audit"])

            # ---- O3：告警规则 + 在线流 ----
            ar = c.post("/api/alert-rules",
                        json={"metric": "pass_rate", "op": "lt", "threshold": 0.5})
            assert ar.status_code == 200 and "name" in ar.json()
            rules = c.get("/api/alert-rules")
            assert rules.status_code == 200 and len(rules.json()["rules"]) >= 1
            # 在线流喂一条失败（code grader：response 不含 gold）
            feed = c.post("/api/online/feed",
                          json={"input": "q", "gold": "2", "response": "失败"})
            assert feed.status_code == 200 and feed.json()["passed"] is False
            met = c.get("/api/online/metrics")
            assert met.status_code == 200 and met.json()["total_calls"] >= 1
            # 删除规则
            nm = ar.json()["name"]
            assert c.delete(f"/api/alert-rules/{nm}").status_code == 200

            # ---- O4：资产 CRUD + 注册工具 ----
            add = c.post("/api/assets",
                         json={"kind": "prompt", "name": "greet", "content": "你好"})
            assert add.status_code == 200 and "id" in add.json()
            lst = c.get("/api/assets")
            assert lst.status_code == 200 and any(
                a["name"] == "greet" for a in lst.json()["assets"])
            got = c.get("/api/assets/greet")
            assert got.status_code == 200 and got.json()["content"] == "你好"
            fn = c.post("/api/assets",
                        json={"kind": "function", "name": "norm",
                              "content": "lambda x: x.strip().lower()"})
            assert fn.status_code == 200
            reg = c.post("/api/assets/norm/register-tool")
            assert reg.status_code == 200 and reg.json()["registered"] == "norm"
            assert c.delete("/api/assets/greet").status_code == 200
    finally:
        if prev is None:
            os.environ.pop("EVAL_SSO_DEV", None)
        else:
            os.environ["EVAL_SSO_DEV"] = prev
        # 重置 RBAC 全局引擎（dev 登录改过组映射，避免污染其他测试）
        set_engine(PolicyEngine())
