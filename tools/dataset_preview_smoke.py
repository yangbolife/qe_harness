"""数据集「用例在线预览」端到端验证（走真实 HTTP，不用测试客户端）。

覆盖：建临时集 → 造一行坏 JSON 与一行超长字段 → 预览的文件事实 / 条目顺序 / 坏行提示 /
截断标注 → 关键词搜索与字段筛选 → 分页与越界夹紧 → 写后索引失效（导入用例立刻可见）→
数据库视角的负例（404 / 400 / 422）→ 删除复原。

特点：
- **可反复跑**：开头清同名残留，结尾断言条目数回到起点。
- **诚实验收**：坏行、截断这类「数据有毛病」的情况必须能被界面看见，本脚本逐项断言，
  而不是只测「正常文件能翻页」。
- **会写真实 catalog 与样本文件**（自动备份、删除时文件入 .trash）。别对生产库跑。

用法：
    python tools/dataset_preview_smoke.py                     # 默认 127.0.0.1:8848
    BASE=http://127.0.0.1:8859 python tools/dataset_preview_smoke.py
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8848")
# 关掉代理：本机请求被 HTTP_PROXY 劫持会 502
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DSID = "zz-pv-smoke"
DATASETS_DIR = Path(__file__).resolve().parents[1] / "eval_harness" / "datasets"
LONG_CHARS = 21000          # 故意超过服务端 20000 的单字段展示上限
all_ok = True


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=30) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt)
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt


def show(tag, ok, extra=""):
    global all_ok
    all_ok &= bool(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {tag}  {extra}")
    return ok


def ids_of(payload):
    return [("坏行" if c.get("error") else (c.get("data") or {}).get("id")) for c in payload["cases"]]


print("=== 0) 起点 ===")
call("DELETE", f"/api/datasets/{DSID}?purge_file=true")     # 清上次中断的残留
st, base = call("GET", "/api/datasets")
before_total = base["summary"]["total"]
print(f"  起点条目数 {before_total}")

print("\n=== 1) 建临时集（2 条正常用例）===")
st, r = call("POST", "/api/datasets", {
    "id": DSID, "name": "临时·预览健壮性验证", "tier": "basic", "domain": "general_llm",
    "capabilities": ["knowledge"], "scenario": "验证坏行与超长字段的预览表现。跑完即删。",
    "license": "自有", "commercial_use": True, "recommended_grader": "code",
    "cases": ['{"id":"PV-01","suite":"pv","grader":"code","input":"1+1=?","gold":"2","meta":{}}',
              '{"id":"PV-02","suite":"pv","grader":"code","input":"2+2=?","gold":"4","meta":{}}'],
})
if not show("POST /api/datasets", st == 200, f"HTTP {st} {r if st != 200 else ''}"):
    raise SystemExit(1)
suite_file = r["entry"]["suite_file"]
path = DATASETS_DIR / suite_file
print(f"  样本文件 {path.name}")

print("\n=== 2) 造脏数据：1 行坏 JSON + 1 行超长字段 ===")
long_text = "超长字段测试" * (LONG_CHARS // 6)
with open(path, "a", encoding="utf-8") as f:
    f.write("{ this is not valid json\n")
    f.write(json.dumps({"id": "PV-04", "suite": "pv", "grader": "code", "input": long_text,
                        "gold": "ok", "meta": {"big": long_text}}, ensure_ascii=False) + "\n")
show("文件共 4 行（2 正常 + 1 坏 + 1 超长）",
     sum(1 for ln in open(path, encoding="utf-8") if ln.strip()) == 4)

print("\n=== 3) 预览：文件事实与条目顺序 ===")
st, d = call("GET", f"/api/datasets/{DSID}/cases?page_size=10")
show("HTTP 200", st == 200, f"HTTP {st}")
show("file 事实 lines=4 / valid=3 / bad=1",
     d["file"]["lines"] == 4 and d["file"]["valid"] == 3 and d["file"]["bad"] == 1, str(d["file"]))
show("条目顺序保持文件行序", ids_of(d) == ["PV-01", "PV-02", "坏行", "PV-04"], str(ids_of(d)))
show("默认选中第 1 条含完整 data", "input" in d["cases"][0]["data"])
bad = next(c for c in d["cases"] if c.get("error"))
show("坏行给出解析错误 + 保留原文",
     "JSON 解析失败" in bad["error"] and bad["raw_text"].startswith("{ this is not"),
     f"line={bad['line']}")

print("\n=== 4) 超长字段截断并标注字段路径 ===")
t4 = next(c for c in d["cases"] if (c.get("data") or {}).get("id") == "PV-04")
show("truncated 同时标出 input 与 meta.big",
     set(t4["truncated"]) >= {"input", "meta.big"}, str(t4["truncated"]))
show("展示文本已截断且短于原文",
     len(t4["data"]["input"]) < LONG_CHARS and "已截断" in t4["data"]["input"],
     f"{len(t4['data']['input'])} < {LONG_CHARS}")

print("\n=== 5) 搜索与字段筛选 ===")
st, s1 = call("GET", f"/api/datasets/{DSID}/cases?q=1%2B1&page_size=10")
show("搜索命中 1 条（坏行不计入）", s1["total"] == 1, str(s1["total"]))
st, s2 = call("GET", f"/api/datasets/{DSID}/cases?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E8%AF%8D")
show("搜不到时 total=0 且 cases 为空", s2["total"] == 0 and not s2["cases"], str(s2["total"]))
st, s3 = call("GET", f"/api/datasets/{DSID}/cases?grader=code&page_size=10")
show("按 grader 筛选命中 3 条（坏行排除）", s3["total"] == 3, str(s3["total"]))
st, s4 = call("GET", f"/api/datasets/{DSID}/cases?grader=nonexistent")
show("筛选值不存在时 total=0", s4["total"] == 0, str(s4["total"]))
show("facets 反映全文件分布（不受筛选影响）", s3["facets"]["grader"].get("code") == 3,
     str(s3["facets"]["grader"]))

print("\n=== 6) 分页与越界夹紧 ===")
st, p1 = call("GET", f"/api/datasets/{DSID}/cases?page=1&page_size=2&grader=code")
st, p2 = call("GET", f"/api/datasets/{DSID}/cases?page=2&page_size=2&grader=code")
st, p9 = call("GET", f"/api/datasets/{DSID}/cases?page=99&page_size=2&grader=code")
show("pages = ceil(3/2) = 2", p1["pages"] == 2, f"pages={p1['pages']}")
show("第 1/2 页不重叠", ids_of(p1) == ["PV-01", "PV-02"] and ids_of(p2) == ["PV-04"],
     f"{ids_of(p1)} / {ids_of(p2)}")
show("越界页码夹到末页", p9["page"] == 2 and ids_of(p9) == ["PV-04"], f"page={p9['page']}")

print("\n=== 7) 坏行会挡住追加导入（报错要指向文件，而不是指向刚粘贴的内容）===")
new_case = '{"id":"PV-05","suite":"pv","grader":"code","input":"5+5=?","gold":"10","meta":{}}'
st, r7 = call("POST", f"/api/datasets/{DSID}/cases", {"mode": "append", "text": new_case})
detail = str(r7.get("detail") if isinstance(r7, dict) else r7)
show("含坏行时 append 被拒（400）", st == 400, f"HTTP {st}")
show("错误信息指向「现有用例文件」", "现有用例文件" in detail, detail[:80])

print("\n=== 8) 写后索引失效：修好文件后导入，预览立即可见 ===")
clean = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
         if ln.strip() and not ln.strip().startswith("{ this")]
path.write_text("\n".join(clean) + "\n", encoding="utf-8")
st, r8 = call("POST", f"/api/datasets/{DSID}/cases", {"mode": "append", "text": new_case})
show("修好文件后 append 成功", st == 200, f"HTTP {st} cases={r8.get('cases') if st == 200 else r8}")
st, d2 = call("GET", f"/api/datasets/{DSID}/cases?page_size=10")
show("预览立即含 PV-05（不是旧索引）", "PV-05" in ids_of(d2), str(ids_of(d2)))
show("file.lines 同步为 4", d2["file"]["lines"] == 4, str(d2["file"]["lines"]))

print("\n=== 9) 负例 ===")
st, r1 = call("GET", "/api/datasets/nope-xxx/cases")
show("不存在 → 404", st == 404, f"HTTP {st}")
st, r2 = call("GET", "/api/datasets/swebench-verified/cases")
show("需执行环境（无文件）→ 400", st == 400, f"HTTP {st} {str(r2.get('detail'))[:34]}")
for q, label in [("page=0", "page=0"), ("page_size=500", "page_size 超上限"),
                 ("page_size=0", "page_size=0")]:
    st, _ = call("GET", f"/api/datasets/{DSID}/cases?{q}")
    show(f"{label} → 422", st == 422, f"HTTP {st}")

print("\n=== 10) 清理 ===")
st, _ = call("DELETE", f"/api/datasets/{DSID}?purge_file=true")
show("删除 200", st == 200, f"HTTP {st}")
show("样本文件已移出 datasets/", not path.exists())
st, after = call("GET", "/api/datasets")
show("条目数回到起点", after["summary"]["total"] == before_total,
     f"{after['summary']['total']} == {before_total}")

print("\n" + ("=== 全部通过 ===" if all_ok else "=== 有失败项，见上 ==="))
raise SystemExit(0 if all_ok else 1)
