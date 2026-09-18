"""数据集管理 CRUD 端到端验证（走真实 HTTP，不用测试客户端）。

覆盖：新建（带用例）→ 详情核对 → 编辑元数据 → 非法值拦截 → 追加用例 → 导出/体检 →
列表与分类 → 删除（含文件回收）→ 条目数复原。

特点：
- **可反复跑**：开头先清同名残留，结尾断言条目数回到起点，不留垃圾。
- **负例齐全**：改 id / 非法 tier / 非受控 domain / 重复 id / 重复用例 id / 非法 JSONL / 空文本 /
  不存在 / 重复删除，逐项断言 400 或 404。
- **会写真实 catalog**（因此自动备份、自动回收文件）。**别对生产库跑**——它只是加一条再删掉，
  但期间会触发 catalog 备份与 .trash 目录（已在 .gitignore 内）。

用法：
    python tools/dataset_crud_smoke.py                       # 默认 127.0.0.1:8848
    BASE=http://127.0.0.1:8859 python tools/dataset_crud_smoke.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.environ.get("BASE", "http://127.0.0.1:8848")
# 关掉代理：本机请求被 HTTP_PROXY 劫持会 502
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=30) as r:
            txt = r.read().decode()
            # raw=True：导出端点返回的是 JSONL（多行 JSON），不能整体 json.loads
            return r.status, (txt if raw else (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt))
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt


def show(tag, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {tag}  {extra}")
    return ok


DSID = "zz-crud-smoke"
CASES = [
    '{"id":"CR-01","suite":"crud_smoke","grader":"code","input":"1+1=?","gold":"2","meta":{"note":"smoke"}}',
    '{"id":"CR-02","suite":"crud_smoke","grader":"code","input":"2+2=?","gold":"4","meta":{"note":"smoke"}}',
]

all_ok = True
print("=== 0) 起点 ===")
# 上一次中断可能留下残留，先清干净再测（脚本要能反复跑）
call("DELETE", f"/api/datasets/{DSID}?purge_file=true")
st, base = call("GET", "/api/datasets")
before_total = base["summary"]["total"]
print(f"  起点条目数 {before_total} · 域 {len([d for d in base['domains'] if d['count']])} 个")
all_ok &= show("起点无同名残留", not any(
    e["id"] == DSID for t in base["tiers"].values() for e in t["datasets"]))

print("\n=== 1) 新建（含用例）===")
st, r = call("POST", "/api/datasets", {
    "id": DSID, "name": "CRUD 冒烟集（临时）", "tier": "advanced",
    "domain": "safety", "capabilities": ["privacy", "content_safety"],
    "scenario": "端到端验证数据集管理动作：新建 / 编辑 / 导入 / 删除。跑完即删。",
    "est_duration_min": "3", "est_tokens": "1500", "license": "自有",
    "commercial_use": True, "recommended_grader": "code(exact)",
    "description": "临时冒烟数据集，验证管理链路后删除。",
    "languages": "zh", "source_kind": "curated_sample",
    "cases": CASES,
})
all_ok &= show("POST /api/datasets", st == 200, f"HTTP {st} {r if st != 200 else r.get('id')}")
if st == 200:
    all_ok &= show("返回 backups 路径", bool(r.get("backup")), r.get("backup", ""))
    all_ok &= show("用例数已同步=2", r["entry"].get("cases_count") == 2, str(r["entry"].get("cases_count")))

print("\n=== 2) 详情核对（文件事实）===")
st, d = call("GET", f"/api/datasets/{DSID}?preview=5")
if st == 200:
    f = d["facts"]
    all_ok &= show("文件就位", f["exists"], f["file_path"])
    all_ok &= show("磁盘实际 2 条", f["actual_cases"] == 2, f"actual={f['actual_cases']} recorded={f['recorded_cases']}")
    all_ok &= show("条数一致（无 cases 告警）",
                   not any(b["field"] == "cases" for b in d["blockers"]))
    all_ok &= show("分类已落", d["dataset"]["domain"] == "safety",
                   f"{d['dataset']['domain_label']} / {d['dataset']['capability_labels']}")
    all_ok &= show("预览读到 2 条", len(d["preview"]) == 2)
else:
    all_ok &= show("GET detail", False, f"HTTP {st} {d}")

print("\n=== 3) 编辑元数据 ===")
st, r = call("PATCH", f"/api/datasets/{DSID}", {
    "scenario": "改过的场景文案（验证 PATCH 生效）",
    "est_duration_min": 7, "capabilities": ["privacy", "permission", "fallback"],
    "domain": "integration_risk",
})
all_ok &= show("PATCH 200", st == 200, f"HTTP {st}")
if st == 200:
    ch = r.get("changed", {})
    all_ok &= show("只返回变化字段", set(ch) <= {"scenario", "est_duration_min", "capabilities", "domain"},
                   str(sorted(ch)))
st, d = call("GET", f"/api/datasets/{DSID}?preview=0")
if st == 200:
    ds = d["dataset"]
    all_ok &= show("场景已改", ds["scenario"].startswith("改过的"), ds["scenario"][:24])
    all_ok &= show("耗时已改=7", ds["est_duration_min"] == 7, str(ds["est_duration_min"]))
    all_ok &= show("域已改=集成风险面", ds["domain"] == "integration_risk", ds["domain_label"])
    all_ok &= show("能力 3 个", len(ds["capabilities"]) == 3, str(ds["capability_labels"]))

print("\n=== 4) id 与非法值不可改 ===")
st, r = call("PATCH", f"/api/datasets/{DSID}", {"id": "hacked-id"})
all_ok &= show("改 id 被拒（400）", st == 400, str(r)[:80])
st, r = call("PATCH", f"/api/datasets/{DSID}", {"tier": "nope"})
all_ok &= show("非法 tier 被拒（400）", st == 400, str(r)[:80])
st, r = call("PATCH", f"/api/datasets/{DSID}", {"domain": "not_a_domain"})
all_ok &= show("非受控 domain 被拒（400）", st == 400, str(r)[:80])
st, r = call("POST", "/api/datasets", {"id": DSID, "name": "重复", "tier": "basic"})
all_ok &= show("重复 id 被拒（400）", st == 400, str(r)[:80])

print("\n=== 5) 追加用例 ===")
st, r = call("POST", f"/api/datasets/{DSID}/cases", {
    "mode": "append",
    "text": '{"id":"CR-03","suite":"crud_smoke","grader":"code","input":"3+3=?","gold":"6","meta":{}}',
})
all_ok &= show("append 200，总数 3", st == 200 and r.get("cases") == 3, f"HTTP {st} cases={r.get('cases')}")
st, r = call("POST", f"/api/datasets/{DSID}/cases", {
    "mode": "append",
    "text": '{"id":"CR-01","suite":"crud_smoke","grader":"code","input":"dup","gold":"x","meta":{}}',
})
all_ok &= show("追加重复 id 被拒（400）", st == 400, str(r)[:90])
st, r = call("POST", f"/api/datasets/{DSID}/cases", {"mode": "replace", "text": "不是 JSON"})
all_ok &= show("非法 JSONL 被拒（400）", st == 400, str(r)[:90])
st, r = call("POST", f"/api/datasets/{DSID}/cases", {"mode": "replace", "text": ""})
all_ok &= show("空文本被拒（400）", st == 400)

print("\n=== 6) 导出与体检 ===")
st, r = call("GET", f"/api/datasets/{DSID}/download", raw=True)
all_ok &= show("导出 JSONL", st == 200 and isinstance(r, str) and r.count("\n") == 3,
               f"HTTP {st} lines={r.count(chr(10)) if isinstance(r, str) else '-'}")
st, r = call("POST", "/api/datasets/validate", {"ids": [DSID], "deep": True, "include_pin_status": True})
if st == 200:
    errs = [i for i in r["issues"] if i["level"] in ("error", "warn")]
    all_ok &= show("体检无错误/警告", not errs, str(errs)[:120] or "clean")

print("\n=== 7) 列表可见与分类到列 ===")
st, base2 = call("GET", "/api/datasets")
found = next((e for t in base2["tiers"].values() for e in t["datasets"] if e["id"] == DSID), None)
all_ok &= show("新集出现在目录", found is not None)
if found:
    all_ok &= show("归入「集成风险面」域", found["domain_label"] == "集成风险面", found["domain_label"])
    all_ok &= show("表格所需元数据齐全",
                   all(k in found for k in ("scenario", "est_duration_min", "est_tokens",
                                            "capability_labels", "license", "cases")))
all_ok &= show("条目数 +1", base2["summary"]["total"] == before_total + 1,
               f"{before_total} -> {base2['summary']['total']}")

print("\n=== 8) 删除（含文件回收）===")
st, r = call("DELETE", f"/api/datasets/{DSID}?purge_file=true")
all_ok &= show("DELETE 200", st == 200, f"HTTP {st} moved={r.get('file_moved_to') if st==200 else r}")
st, base3 = call("GET", "/api/datasets")
all_ok &= show("条目数复原", base3["summary"]["total"] == before_total,
               f"{before_total} -> {base3['summary']['total']}")
all_ok &= show("列表已无该集", not any(
    e["id"] == DSID for t in base3["tiers"].values() for e in t["datasets"]))
st, r = call("GET", f"/api/datasets/{DSID}")
all_ok &= show("详情 404", st == 404)
st, r = call("DELETE", f"/api/datasets/{DSID}")
all_ok &= show("重复删除 404", st == 404)

print("\n" + ("全部通过 ✅" if all_ok else "存在失败项 ❌"))
sys.exit(0 if all_ok else 1)
