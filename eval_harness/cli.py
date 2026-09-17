"""eval_harness · 本地 CLI（Phase 1 ~ Phase 4）

新增：--db-url/--persist（PG 持久化）、--three-way（脚手架三方解耦）、
--judge-budget（judge 成本闸门）、--model-version/--dataset-version（版本钉）、
--generate（动态 curated 产题 + 版本钉落库）、--ingest-trace（生产 trace 回灌）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from .core.engine import aggregate, run_suite, run_suite_async
from .core.worker import run_distributed
from .core.telemetry import configure, get_tracer, finish
from .core.version import HARNESS_VERSION
from .core.persistence import Database
from .core.gate import GateConfig, evaluate_gate
from .core import datasets_catalog as dc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_harness",
                                description=f"智化融合评测 harness · v{HARNESS_VERSION}")
    p.add_argument("suite", nargs="?", help="用例集路径 (.jsonl / .yaml)；与 --ingest-trace/--generate 互斥")
    p.add_argument("--provider", default="mock",
                   help="被测系统 provider：mock / deepseek / qwen / zhipu / domestic_gateway（O1 国内网关）")
    p.add_argument("--provider-kwargs", default=None,
                   help='被测 provider 构造参数 JSON，如 \'{"mode":"scenario"}\'')
    p.add_argument("--grader", default="code", help="默认评分器：code / judge / human / sandbox / trajectory / trajectory_eval / 组合")
    p.add_argument("--judge-provider", default=None, help="LLM-Judge 评分器使用的强模型 provider")
    p.add_argument("--judge-provider-kwargs", default=None, help='judge provider 构造参数 JSON')
    p.add_argument("--judge-budget", type=float, default=0.0,
                   help="judge 通道成本预算(USD)，>0 时超预算中止（创新⑤）")
    p.add_argument("--combine", default="any", choices=["any", "all"], help="多评分器组合策略")
    # —— Phase 2 高并发 / 稳健性 —— #
    p.add_argument("--trials", type=int, default=1, help="同一用例重复执行次数 k")
    p.add_argument("--trial-policy", default="any", choices=["any", "all", "majority"])
    p.add_argument("--concurrency", type=int, default=4, help="并发数")
    p.add_argument("--rate", type=float, default=8.0, help="令牌桶速率（请求/秒）")
    p.add_argument("--inconclusive-ratio", type=float, default=0.34, help="熔断占比阈值")
    p.add_argument("--output", default=None, help="结果输出 json 路径")
    p.add_argument("--print", action="store_true", help="打印每条结果与评分细节")
    # —— Phase 3 —— #
    p.add_argument("--otel-console", action="store_true", help="开启 trace 打印到控制台")
    p.add_argument("--otel-endpoint", default=None, help="OTLP gRPC 端点")
    p.add_argument("--distributed", action="store_true", help="分布式多进程 Worker 模式")
    p.add_argument("--workers", type=int, default=2, help="分布式 Worker 进程数")
    # —— Phase 4 / 创新点 —— #
    p.add_argument("--persist", action="store_true", help="运行结果持久化到数据库（PG/SQLite）")
    p.add_argument("--db-url", default=None, help="数据库连接串（默认 EVAL_DB_URL 或本地 SQLite）")
    p.add_argument("--run-name", default="", help="本次运行命名（持久化展示用）")
    p.add_argument("--three-way", action="store_true", help="脚手架三方解耦（创新③）")
    p.add_argument("--model-version", default=None, help="版本钉：模型版本（创新④）")
    p.add_argument("--dataset-version", default=None, help="版本钉：数据集版本（创新④）")
    # —— N8：评测参数（Parameters，可重复，随运行记录便于横向对比） —— #
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="附加评测参数（可重复），如 --param temperature=0.7 --param top_p=0.9；随运行落库便于对比")
    p.add_argument("--generate", default=None,
                   help="动态产题：seed,n,out.jsonl（如 7,20,examples/curated.jsonl），并版本钉落库")
    p.add_argument("--ingest-trace", default=None, help="生产 trace 回灌：OTel JSON 文件路径（创新⑥）")
    # —— N9：只读 SQL 查询（CLI 入口，沙箱四道闸） —— #
    p.add_argument("--sql", default=None,
                   help="只读 SQL 查询（仅可引用预置视图 runs/cases/graders/trials/traces）；与 suite 互斥")
    p.add_argument("--sql-limit", type=int, default=200, help="SQL 结果行数上限（≤5000）")
    p.add_argument("--sql-timeout", type=float, default=15.0, help="SQL 超时（秒，≤60）")
    p.add_argument("--sql-out", default=None, help="SQL 结果导出 JSON 路径（可选）")
    # —— N11：外部评测框架 loader（lm-evaluation-harness）——
    p.add_argument("--ingest-lmeval", default=None,
                   help="导入 lm-eval samples_*.jsonl 为可比 run（N11 外部框架 loader）")
    p.add_argument("--lmeval-name", default=None, help="导入 run 的命名")
    p.add_argument("--lmeval-threshold", type=float, default=0.5, help="lm-eval 指标>=该值判通过")
    p.add_argument("--lmeval-suite", default=None, help="lm-eval 类别/套件名（落库 suite）")
    # —— N10：日志检索与数据出库 ——
    p.add_argument("--search", default=None, help="跨 runs/cases 检索关键词（N10 日志检索）")
    p.add_argument("--export", default=None, help="数据出库目录（N10）；配合 --export-fmt/--export-partition")
    p.add_argument("--export-fmt", default="jsonl", choices=["jsonl", "csv", "parquet"],
                   help="出库格式（parquet 需 pyarrow）")
    p.add_argument("--export-partition", default="date",
                   choices=["date", "provider", "model", "run", "none"], help="Hive 分区维度")
    p.add_argument("--export-s3", default=None, help="导出后上传到 s3://bucket/prefix/（需 boto3）")
    # —— N4：CI 质量闸门 ——
    p.add_argument("--ci", action="store_true", help="CI 模式：门禁失败则退出码非 0（N4）")
    p.add_argument("--gate-pass-rate", type=float, default=None, help="整体通过率下限（如 0.8）")
    p.add_argument("--gate-inconclusive", type=float, default=None, help="inconclusive 占比上限（如 0.34）")
    p.add_argument("--gate-regression", type=float, default=None, help="相对 baseline 通过率最大跌幅（如 0.05=5pp）")
    p.add_argument("--gate-baseline", default=None, help="baseline run id/name（与 --gate-regression 配合）")
    p.add_argument("--gate-category", action="append", default=[], metavar="CAT:MIN",
                   help="按类别最低通过率，可重复，如 --gate-category safety:0.9")
    p.add_argument("--junit", default=None, help="门禁结果导出 JUnit XML 路径（供 CI）")
    p.add_argument("--gate-json", default=None, help="门禁结果导出 JSON 路径")
    # —— O13：token/成本预算 ——
    p.add_argument("--cost-budget", type=float, default=0.0,
                   help="judge/远程评测总美元预算上限，超预算中止 judge 通道（O13）")
    p.add_argument("--token-budget", type=int, default=0,
                   help="judge/远程评测总 token 预算上限（O13）")
    p.add_argument("--owner", default=None, help="运行归属者（N12 行级 ACL；默认 anonymous）")
    p.add_argument("--visibility", default="all", choices=["all", "private"],
                   help="运行可见性（N12：all=所有人可见 / private=仅 owner）")
    p.add_argument("--external-id", default=None,
                   help="外部关联键（跨系统）：由调用方（如质擎智评工作台）传入其评测运行 id，"
                        "用于把本运行关联回工作台的评测项目")
    # —— 回链 meta（O14）：工作台调起时携带，供双向回查 —— #
    p.add_argument("--wb-callback-url", default=None,
                   help="工作台回链 URL：评测人员在本控制台查看运行时可一键返回工作台对应验收单")
    p.add_argument("--wb-contract-id", default=None,
                   help="关联的工作台合同/验收单 id（随运行记录，供回链与归集）")
    # —— N2：项目级 Rubric + 盲评 ——
    p.add_argument("--rubric", default=None, help="项目级 Rubric JSON 路径（N2）；配合 --rubric-run 套用并出盲评报告")
    p.add_argument("--rubric-run", default=None, help="把 Rubric 套用到该 run_id 并出加权评分报告（N2）")
    # —— O17：数据集快照 ——
    p.add_argument("--snapshot", action="store_true", help="评测前对数据集做快照留存（O17），可回滚复评")
    # —— Phase 0：数据集目录 —— #
    p.add_argument("--list-datasets", action="store_true",
                   help="列出 catalog 中可预置的开源评测数据集（分级/状态/决策字段：用例数·耗时·token·许可·场景）")
    # —— O14：push / trace ——
    p.add_argument("--push", default=None, help="把指定 run_id 导出为 .harnesspush 包（O14）")
    p.add_argument("--push-out", default=None, help="push 包输出路径（默认 ./<run_id>.harnesspush）")
    p.add_argument("--push-to", default=None, help="把 run 复制到另一数据库 URL（O14 远程同步）")
    p.add_argument("--trace", default=None, help="按 trace id 打印生产轨迹明细（O14/O15）")
    p.add_argument("--trace-file", default=None, help="对本地 OTel trace 文件直接评估（O14/O15）")
    # —— 创新⑦：Apple 风格可视化控制台 —— #
    p.add_argument("--web", action="store_true", help="启动 Web 可视化控制台（默认 http://127.0.0.1:8848）")
    p.add_argument("--web-host", default="127.0.0.1", help="Web 控制台监听地址")
    p.add_argument("--web-port", type=int, default=8848, help="Web 控制台监听端口")
    # —— O1：国内模型 AI 网关（provider 名 qwen/zhipu/domestic_gateway；网关后端用 --provider-kwargs '{"config":[...]}'） ——
    # —— O2：企业合规 SSO 审计查看 —— #
    p.add_argument("--sso-audit", default=None,
                   help="打印合规审计日志（O2）：可选 event_type 过滤，如 login；需配合 --persist/--db-url")
    # —— O3：在线评分 + 告警 —— #
    p.add_argument("--online", action="store_true",
                   help="在线流模式（O3）：从 stdin/--online-file 逐行读 NDJSON {input,gold,response,...} 实时评分+告警")
    p.add_argument("--online-file", default=None, help="在线流输入文件（NDJSON，每行一条结果）")
    p.add_argument("--online-grader", default="code", help="在线流默认评分器（O3）")
    p.add_argument("--alert-rules", default=None, help="在线流告警规则 JSON 路径（O3，list[dict]）")
    p.add_argument("--online-out", default=None, help="在线流最终指标导出 JSON 路径（O3）")
    # —— O4：版本化资产（prompt/function）push/pull —— #
    p.add_argument("--asset", default=None,
                   help="版本化资产（O4）：push:out.json / pull:in.json / list / "
                        "add:kind:name:file.txt / get:name[:version]；需配合 --persist/--db-url")
    return p


async def _async_make_store(args) -> Optional[Database]:
    if not args.persist and not args.db_url:
        return None
    db = Database(args.db_url)
    await db.init()
    return db


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)

    if args.otel_console or args.otel_endpoint:
        configure(console=args.otel_console, otlp_endpoint=args.otel_endpoint)

    # —— Phase 0：列出可预置数据集目录 —— #
    if args.list_datasets:
        return _cmd_list_datasets(args)

    import asyncio

    # —— 创新⑦：Web 可视化控制台 —— #
    if args.web:
        from .web.app import create_app
        import uvicorn
        print(f"[web] 启动控制台 http://{args.web_host}:{args.web_port}")
        print(f"[web] 数据库后端：{args.db_url or 'EVAL_DB_URL 或本地 SQLite'}")
        uvicorn.run(create_app(args.db_url), host=args.web_host, port=args.web_port, log_level="info")
        return 0

    # —— 创新⑥：生产 trace 回灌 —— #
    if args.ingest_trace:
        return _cmd_ingest_trace(args)

    # —— 创新④：动态产题 + 版本钉 —— #
    if args.generate:
        return _cmd_generate(args)

    # —— N9：只读 SQL 查询（CLI 入口） —— #
    if args.sql:
        return _cmd_sql(args)

    # —— N10：日志检索 —— #
    if args.search:
        return _cmd_search(args)

    # —— N10：数据出库 —— #
    if args.export:
        return _cmd_export(args)

    # —— N11：导入 lm-eval 结果 —— #
    if args.ingest_lmeval:
        return _cmd_ingest_lmeval(args)

    # —— O14：push / trace —— #
    if args.push:
        return _cmd_push(args)
    if args.trace:
        return _cmd_trace(args)
    if args.trace_file:
        return _cmd_trace_file(args)
    # —— O4：版本化资产 push/pull —— #
    if args.asset:
        return _cmd_asset(args)
    # —— O2：合规审计查看 —— #
    if args.sso_audit is not None:
        return _cmd_sso_audit(args)
    # —— O3：在线流评分 + 告警 —— #
    if args.online or args.online_file:
        return _cmd_online(args)
    # —— N2：Rubric 套用（盲评报告） —— #
    if args.rubric_run and args.rubric:
        return _cmd_rubric(args)

    if not args.suite:
        p.error("需提供 suite 路径，或 --generate / --ingest-trace")

    # —— Phase 0：把 suite（catalog id / 文件名 / 路径）解析为可加载路径 —— #
    try:
        resolved_suite = dc.resolve_dataset_path(args.suite)
    except FileNotFoundError as e:
        print(f"[harness] 错误：{e}")
        return 2

    jkw = json.loads(args.judge_provider_kwargs) if args.judge_provider_kwargs else None
    pkw = json.loads(args.provider_kwargs) if args.provider_kwargs else None

    # 分布式模式：多进程 Worker，无持久化落库
    if args.distributed:
        results = run_distributed(
            args.suite, workers=args.workers, provider_name=args.provider,
            provider_kwargs=pkw, judge_provider_name=args.judge_provider,
            judge_provider_kwargs=jkw, default_grader=args.grader, combine=args.combine,
            trials=args.trials, trial_policy=args.trial_policy,
            inconclusive_ratio=args.inconclusive_ratio, rate=args.rate, output_path=args.output,
        )
        _emit_report(args, results, f"distributed(workers={args.workers})", False)
        return 0

    # 单事件循环：store 与 run_suite_async 必须在同一 loop，
    # 否则 SQLAlchemy asyncpg 引擎会绑定到已关闭的 loop（"different loop" 错误）
    params = _parse_params(args.param)
    # —— O17：数据集快照（评测前留存，可回滚） —— #
    snapshot_id = None
    if args.snapshot and args.persist or args.snapshot:
        snapshot_id = _make_snapshot(args)
        if snapshot_id:
            params = dict(params)
            params["snapshot_id"] = snapshot_id
            print(f"[snapshot] 数据集快照已留存 id={snapshot_id}")
    async def _run():
        store = await _async_make_store(args)
        results = await run_suite_async(
            resolved_suite, provider_name=args.provider, provider_kwargs=pkw,
            default_grader=args.grader, judge_provider_name=args.judge_provider,
            judge_provider_kwargs=jkw, combine=args.combine, trials=args.trials,
            trial_policy=args.trial_policy, inconclusive_ratio=args.inconclusive_ratio,
            concurrency=args.concurrency, rate=args.rate, output_path=args.output,
            judge_budget=args.judge_budget, three_way=args.three_way, store=store,
            run_name=args.run_name,             model_version=args.model_version,
            dataset_version=args.dataset_version, params=params,
            token_budget=args.token_budget, cost_budget=args.cost_budget,
            owner=args.owner or "anonymous", visibility=args.visibility,
            external_id=args.external_id,
            wb_meta=_build_wb_meta(args),
        )
        if store is not None:
            await store.close()
        return results

    results = asyncio.run(_run())
    _emit_report(args, results, f"async(concurrency={args.concurrency}, rate={args.rate}/s)",
                 bool(args.persist or args.db_url))

    # —— N4：CI 质量闸门 —— #
    gate_requested = (args.ci or args.gate_pass_rate is not None or args.gate_inconclusive is not None
                      or args.gate_regression is not None or args.gate_baseline or args.gate_category)
    if gate_requested:
        return _run_gate(args, results)
    return 0


def _parse_params(items) -> dict:
    """把 ``--param KEY=VALUE`` 列表解析为 dict（VALUE 尝试转数值，否则保留字符串）。"""
    out: dict = {}
    for it in (items or []):
        if "=" not in it:
            print(f"[param] 忽略非法参数（需 KEY=VALUE）：{it}")
            continue
        k, v = it.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _build_wb_meta(args) -> "dict | None":
    """根据 CLI 参数拼出回链 meta（O14）：工作台调起时携带 contract_id / callback_url。"""
    meta: dict = {}
    if getattr(args, "wb_callback_url", None):
        meta["callback_url"] = args.wb_callback_url
    if getattr(args, "wb_contract_id", None):
        meta["contract_id"] = args.wb_contract_id
    return meta or None


def _emit_report(args, results, mode: str, persisted: bool):
    """打印评测报告（聚合 / 三方解耦 / 明细 / 遥测）。"""
    agg = aggregate(results)
    tw = getattr(results, "_three_way", None)

    print(f"\n[harness] 用例集 {args.suite}")
    print(f"[harness] mode={mode} | provider={args.provider} | grader={args.grader}"
          f" | judge={args.judge_provider or '-'} | combine={args.combine}")
    print(f"[harness] trials={args.trials} policy={args.trial_policy}"
          f" | harness_version={HARNESS_VERSION}")
    print(f"[harness] 通过率={agg['pass_rate']:.2%} ({agg['passed']}/{agg['total']})"
          f" | 平均pass@k={agg['avg_pass_at_k']:.2%} | inconclusive={agg['inconclusive']}")
    print(f"[harness] 按套件: {json.dumps(agg['by_suite'], ensure_ascii=False)}")
    print(f"[harness] 按类别: {json.dumps(agg['by_category'], ensure_ascii=False)}")

    # 创新③：脚手架三方解耦摘要
    if tw:
        print(f"\n[三方解耦] bare={tw['bare_pass_rate']:.2%} harness={tw['harness_pass_rate']:.2%}"
              f" full={tw['full_pass_rate']:.2%}")
        print(f"[三方解耦] harness_only_gain={tw['harness_only_gain']:+.2%}"
              f" scaffold_leakage={tw['scaffold_leakage']:+.2%}（全系统相对裸模型的增益）")

    if args.print:
        for r in results:
            tag = "⚠️INC" if r.inconclusive else ("✓" if r.passed else "✗")
            gd = " | ".join(f"{g.grader}:{g.score:.2f}({'✓' if g.passed else '✗'})"
                            for g in r.graders) or "—"
            extra = ""
            if r.k > 1:
                extra = f" | pass_rate={r.pass_rate:.0%} pass@k={r.pass_at_k:.0%} pass^k={r.pass_k}"
            print(f"  #{r.case.id} [{r.case.suite}/{r.case.category}] {tag} :: {gd}{extra}")

    if persisted:
        print(f"[persist] 已写入数据库（run 列表可用 Web UI / list_runs 查看）")

    summary = get_tracer().summary()
    print(f"\n[telemetry] spans={summary['spans']} 总耗时={summary['total_dur_ms']}ms mode={summary.get('mode')}")
    for name, s in summary["by_name"].items():
        print(f"  · {name}: {s['count']}次 平均{s['avg_ms']}ms 错误{s['err']}")
    if summary.get("mode") == "otel":
        print(f"  · 已导出到 {args.otel_endpoint or 'console'}")
    finish()


def _cmd_list_datasets(args):
    """Phase 0：列出 catalog 中可预置的开源评测数据集（分级 + 决策字段）。"""
    groups = dc.group_by_tier()
    print(f"\n[catalog] {dc.CATALOG_PATH}")
    for tier in dc.TIER_ORDER:
        items = groups.get(tier, [])
        if not items:
            continue
        print(f"\n=== {dc.TIER_LABELS[tier]}（{tier}）· {len(items)} 个 ===")
        for e in items:
            flag = {"ready": "✓可跑", "env": "⚙环境", "planned": "·待拉"}[e["status"]]
            dur = e.get("est_duration_min")
            tok = e.get("est_tokens")
            comm = "可商用" if e.get("commercial_use") else "非商用"
            print(f"  [{flag}] {e['id']:<22} {e['name']}")
            print(f"       用例={e['actual_cases']:<5} 耗时≈{dur}min token≈{tok} "
                  f"评分={e.get('recommended_grader','-')} 许可={e.get('license','-')}({comm})")
            print(f"       场景: {e.get('scenario','')}")
    fx = dc.list_fixtures()
    if fx:
        print(f"\n=== 开发/演示 fixtures · {len(fx)} 个 ===")
        for f in fx:
            print(f"  · {f['name']:<28} {f['cases']} 题（{f['note']}）")
    s = dc.summary()
    print(f"\n[统计] 共 {s['total']} 个 · 可跑 {s['ready']} · 环境占位 {s['env']} · 待拉取 {s['planned']}")
    print("[提示] 真实拉取官方抽样：python tools/fetch_datasets.py --only <id> --sample N（需联网）")
    return 0


def _cmd_generate(args):
    import asyncio
    from .core.generator import generate_curated, pin_dataset

    try:
        seed_s, n_s, out = args.generate.split(",")
        seed, n = int(seed_s), int(n_s)
    except Exception:
        print("[generate] 用法错误：--generate seed,n,out.jsonl （如 7,20,examples/curated.jsonl）")
        return 2
    cases = generate_curated(seed, n, path=out)
    ver = __import__("hashlib"), None  # noqa
    from .core.generator import version_of
    v = version_of(cases)
    print(f"[generate] 已生成 {n} 条 → {out} | 数据集版本钉={v}")
    if args.persist or args.db_url:
        async def _pin():
            db = Database(args.db_url)
            await db.init()
            csid = await pin_dataset(db, name=out, cases=cases, source_path=out)
            print(f"[generate] 版本钉已落库 case_sets.id={csid}")
        asyncio.run(_pin())
    return 0


def _cmd_ingest_trace(args):
    import asyncio
    from .core.trace_store import ingest_and_evaluate, evaluate_trace_payload

    with open(args.ingest_trace, encoding="utf-8") as f:
        payload = json.load(f)
    if args.persist or args.db_url:
        async def _ing():
            db = Database(args.db_url)
            await db.init()
            res = await ingest_and_evaluate(db, source=args.ingest_trace, payload=payload,
                                            grader_name="trajectory")
            print(json.dumps(res, ensure_ascii=False, indent=2))
        asyncio.run(_ing())
    else:
        res = evaluate_trace_payload(payload, grader_name="trajectory")
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _cmd_sql(args):
    import asyncio
    from .core.sql import run_query, format_table, SQLGuardError

    async def _go():
        db = Database(args.db_url)
        await db.init()
        try:
            result = await run_query(db, args.sql, limit=args.sql_limit, timeout=args.sql_timeout)
        except SQLGuardError as e:
            print(f"[sql] 只读查询被拒绝：{e}")
            return 2
        except Exception as e:  # noqa: BLE001
            print(f"[sql] 查询执行失败：{e}")
            return 1
        finally:
            await db.close()
        print(format_table(result))
        print(f"\n[sql] {result['row_count']} 行 | 截断={result['truncated']}"
              f" | 耗时={result['elapsed_ms']}ms | 上限={result['limit']}")
        if args.sql_out:
            with open(args.sql_out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"[sql] 结果已导出 → {args.sql_out}")
        return 0

    return asyncio.run(_go())


def _cmd_search(args):
    import asyncio
    async def _go():
        db = Database(args.db_url)
        await db.init()
        try:
            return await db.search(args.search)
        finally:
            await db.close()
    r = asyncio.run(_go())
    print(f"[search] 关键词「{r['query']}」 → runs={len(r['runs'])} cases={len(r['cases'])}")
    for run in r["runs"][:10]:
        print(f"  · run {run['name']} ({run['provider']}/{run['model_version']}) pr={run['pass_rate']:.1%}")
    for c in r["cases"][:10]:
        print(f"  · case {c['case_id']} [{c['suite']}/{c['category']}]"
              f" {'✓' if c['passed'] else '✗'} :: {c['input'][:40]}")
    return 0


def _cmd_export(args):
    import asyncio
    from .core import export as export_mod
    async def _go():
        db = Database(args.db_url)
        await db.init()
        try:
            return await export_mod.export(db, args.export, fmt=args.export_fmt,
                                           partition=args.export_partition, s3_url=args.export_s3)
        finally:
            await db.close()
    r = asyncio.run(_go())
    print(f"[export] 已出库 → {r['out_dir']} | fmt={r['fmt']} partition={r['partition']}")
    print(f"  runs={r['runs']} rows={r['rows']}"
          + (" | ⚠️ parquet 不可用已回退 jsonl" if r["parquet_fallback"] else "")
          + (f" | s3={r['s3']}" if r["s3"] else ""))
    return 0


def _cmd_ingest_lmeval(args):
    import asyncio
    from .core import lm_eval_loader
    name = args.lmeval_name or os.path.basename(args.ingest_lmeval)
    async def _go():
        db = Database(args.db_url)
        await db.init()
        try:
            return await lm_eval_loader.ingest_lm_eval(
                db, args.ingest_lmeval, name,
                metric_threshold=args.lmeval_threshold, suite=args.lmeval_suite)
        finally:
            await db.close()
    rid = asyncio.run(_go())
    print(f"[lmeval] 已导入 → run_id={rid} | name={name} | threshold={args.lmeval_threshold}")
    return 0


def _run_gate(args, results):
    import asyncio
    agg = aggregate(results)
    cat_min = {}
    for item in args.gate_category:
        if ":" not in item:
            continue
        cat, mn = item.split(":", 1)
        try:
            cat_min[cat] = float(mn)
        except ValueError:
            print(f"[gate] 忽略非法 --gate-category：{item}")
    cfg = GateConfig(
        pass_rate_min=args.gate_pass_rate or 0.0,
        inconclusive_max=args.gate_inconclusive if args.gate_inconclusive is not None else 1.0,
        regression_max_drop=args.gate_regression or 0.0,
        category_min=cat_min,
    )
    baseline = None
    if args.gate_baseline:
        async def _load():
            db = Database(args.db_url)
            await db.init()
            try:
                return await db.get_run(args.gate_baseline)
            finally:
                await db.close()
        baseline = asyncio.run(_load())
        if baseline is None:
            print(f"[gate] 找不到 baseline run：{args.gate_baseline}")
    gres = evaluate_gate(agg, baseline, cfg)
    print("\n[门禁] " + ("✅ 通过" if gres.passed else "❌ 未通过"))
    for c in gres.checks:
        print(f"  {'✓' if c.passed else '✗'} {c.name} — {c.detail}")
    if args.junit:
        with open(args.junit, "w", encoding="utf-8") as f:
            f.write(gres.junit_xml())
        print(f"[门禁] JUnit XML → {args.junit}")
    if args.gate_json:
        with open(args.gate_json, "w", encoding="utf-8") as f:
            json.dump(gres.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"[门禁] JSON → {args.gate_json}")
    if args.ci:
        return gres.exit_code()
    return 0


def _make_snapshot(args) -> Optional[str]:
    """O17：把 suite 文件内容留存为数据集快照。"""
    import asyncio
    import hashlib
    try:
        with open(args.suite, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"[snapshot] 读取数据集失败：{e}")
        return None
    h = hashlib.sha256(content.encode()).hexdigest()[:16]
    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        return asyncio.run(db.save_dataset_snapshot(
            name=os.path.basename(args.suite), source_path=args.suite,
            content=content, hashv=h))
    finally:
        asyncio.run(db.close())


def _cmd_push(args):
    import asyncio
    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        run = asyncio.run(db.get_run(args.push))
        if run is None:
            print(f"[push] 找不到 run：{args.push}")
            return 2
        bundle = {"run": run, "pushed_at": time.time()}
        out = args.push_out or f"{args.push}.harnesspush"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        print(f"[push] 已导出 run {args.push} → {out}（{len(run.get('cases', []))} 用例）")
        if args.push_to:
            target = Database(args.push_to)
            asyncio.run(target.init())
            try:
                from .core.models import CaseResult
                results = [CaseResult.from_dict(c) for c in run.get("cases", [])]
                meta = dict(run.get("config_json") or {})
                asyncio.run(target.save_run(
                    run.get("name", args.push), meta, results,
                    three_way=run.get("three_way_json"),
                    run_id=run["id"], owner=run.get("owner"),
                    visibility=run.get("visibility", "all"),
                    token_budget_usd=run.get("token_budget_usd", 0.0),
                    cost_used_usd=run.get("cost_used_usd", 0.0),
                    budget_aborted=run.get("budget_aborted", False),
                ))
                print(f"[push] 已同步到 {args.push_to}（run_id={run['id']}）")
            finally:
                asyncio.run(target.close())
        return 0
    finally:
        asyncio.run(db.close())


def _cmd_trace(args):
    import asyncio
    from .core.trace_store import evaluate_trace_payload, parse_trace_to_case
    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        traces = asyncio.run(db.list_traces(limit=10000))
        hit = next((t for t in traces if str(t["id"]) == str(args.trace)), None)
        if hit is None:
            print(f"[trace] 找不到 trace：{args.trace}")
            return 2
        res = evaluate_trace_payload(hit["payload"], grader_name="trajectory")
        case = parse_trace_to_case(hit["payload"], trace_id=f"trace:{args.trace}")
        print(f"[trace] id={args.trace} source={hit['source']}")
        print(f"  步骤: {case.meta.get('expected_steps')}")
        print(f"  评分: score={res['score']:.2f} passed={res['passed']} :: {res['detail']}")
        print(f"  轨迹:\n{case.meta.get('trajectory_text', '')}")
        return 0
    finally:
        asyncio.run(db.close())


def _cmd_trace_file(args):
    import json as _json
    from .core.trace_store import evaluate_trace_payload, parse_trace_to_case
    with open(args.trace_file, encoding="utf-8") as f:
        payload = _json.load(f)
    res = evaluate_trace_payload(payload, grader_name="trajectory")
    case = parse_trace_to_case(payload, trace_id="inline")
    print(f"[trace-file] 步骤: {case.meta.get('expected_steps')}")
    print(f"[trace-file] score={res['score']:.2f} passed={res['passed']} :: {res['detail']}")
    return 0


def _cmd_rubric(args):
    """N2：把项目级 Rubric 套用到某次运行，产出加权评分 + 盲评视图。"""
    import asyncio
    from .core.rubric import Rubric, score_case_with_rubric, blind_anonymize
    rubric = Rubric.from_dict(json.load(open(args.rubric, encoding="utf-8")))
    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        run = asyncio.run(db.get_run(args.rubric_run))
        if run is None:
            print(f"[rubric] 找不到 run：{args.rubric_run}")
            return 2
        scored = []
        for c in run.get("cases", []):
            from .core.models import Case
            case = Case(id=c["case_id"], input=c.get("input", ""), gold=c.get("gold", ""),
                        category=c.get("category", ""), suite=c.get("suite", ""),
                        grader=c.get("grader", ""))
            r = score_case_with_rubric(c.get("response", ""), case, rubric)
            scored.append(r)
        n = len(scored)
        avg = (sum(s["rubric_score"] for s in scored) / n) if n else 0.0
        passes = sum(1 for s in scored if s["rubric_pass"])
        blind = blind_anonymize(run)
        print(f"[rubric] 套用「{rubric.name}」到 run {args.rubric_run}")
        print(f"  用例 {n} | 平均 rubric 分={avg:.2%} | 达标 {passes}/{n}")
        print(f"  盲评视图 provider={blind['provider']} model={blind['model_version']}（已匿名）")
        for s in scored[:5]:
            print(f"  · {s['case_id']}: rubric={s['rubric_score']:.2%} "
                  f"{'✓' if s['rubric_pass'] else '✗'}")
        return 0
    finally:
        asyncio.run(db.close())


# ---- O4：版本化资产 push/pull ----
def _cmd_asset(args):
    import asyncio
    from .core import assets as asset_mod

    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        spec = args.asset
        if spec.startswith("push:"):
            out = spec[len("push:"):]
            res = asyncio.run(asset_mod.export_bundle(db, out))
            print(f"[asset] 已导出 {res['count']} 个资产 → {out}")
            return 0
        if spec.startswith("pull:"):
            src = spec[len("pull:"):]
            res = asyncio.run(asset_mod.import_bundle(db, src))
            print(f"[asset] 导入完成：新增 {res['added']} / 跳过 {res['skipped']} / 共 {res['total']}")
            return 0
        if spec == "list":
            items = asyncio.run(asset_mod.list_assets(db))
            print(f"[asset] 共 {len(items)} 个资产：")
            for a in items:
                print(f"  · [{a.kind}] {a.name} v{a.version} (hash={a.content_hash})")
            return 0
        if spec.startswith("add:"):
            _, kind, name, file = spec.split(":", 3)
            with open(file, encoding="utf-8") as f:
                content = f.read()
            aid = asyncio.run(asset_mod.save_asset(db, kind, name, content))
            print(f"[asset] 已保存 {kind} 资产 {name}（id={aid}）")
            return 0
        if spec.startswith("get:"):
            rest = spec[len("get:"):]
            name, _, version = rest.partition(":")
            a = asyncio.run(asset_mod.get_asset(db, name, version or None))
            if a is None:
                print(f"[asset] 找不到资产：{name}（{version or 'latest'}）")
                return 2
            print(f"[asset] [{a.kind}] {a.name} v{a.version} hash={a.content_hash}")
            print(a.content)
            return 0
        print("[asset] 用法：push:out.json / pull:in.json / list / add:kind:name:file / get:name[:version]")
        return 2
    finally:
        asyncio.run(db.close())


# ---- O2：合规审计查看 ----
def _cmd_sso_audit(args):
    import asyncio
    db = Database(args.db_url)
    asyncio.run(db.init())
    try:
        rows = asyncio.run(db.list_audit(event_type=args.sso_audit or None, limit=500))
        print(f"[audit] 共 {len(rows)} 条审计事件" +
              (f"（event_type={args.sso_audit}）" if args.sso_audit else ""))
        for r in rows[:100]:
            flag = "✓" if r["ok"] else "✗"
            print(f"  {flag} {r['event_type']} principal={r['principal']} "
                  f"detail={r['detail']} ip={r['ip']}")
        return 0
    finally:
        asyncio.run(db.close())


# ---- O3：在线流评分 + 告警 ----
def _cmd_online(args):
    import asyncio
    import json as _json
    from .core.online import OnlineEvaluator
    from .core.alerting import AlertRule

    rules = []
    if args.alert_rules:
        with open(args.alert_rules, encoding="utf-8") as f:
            rules = [AlertRule.from_dict(d) for d in _json.load(f)]
    ev = OnlineEvaluator(default_grader=args.online_grader, rules=rules)

    # 输入来源：文件或 stdin
    if args.online_file:
        with open(args.online_file, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    else:
        import sys
        lines = [ln for ln in sys.stdin if ln.strip()]

    total_alerts = 0
    for ln in lines:
        try:
            item = _json.loads(ln)
        except _json.JSONDecodeError as e:
            print(f"[online] 跳过非法 JSON 行：{e}")
            continue
        res = ev.feed(
            item.get("input", ""), item.get("gold", ""), item.get("response", ""),
            case_id=item.get("case_id", ""), grader=item.get("grader"),
            latency_ms=float(item.get("latency_ms", 0.0)),
            cost_usd=float(item.get("cost_usd", 0.0)),
            category=item.get("category", "general"),
        )
        if res["alerts"]:
            total_alerts += len(res["alerts"])
            for a in res["alerts"]:
                print(f"  🚨 {a['message']}  (case={res['case_id']})")

    metrics = ev.snapshot()
    print(f"\n[online] 处理 {ev._count} 条 | 告警触发 {total_alerts} 次")
    print(f"[online] 指标：{_json.dumps(metrics, ensure_ascii=False)}")
    if args.online_out:
        with open(args.online_out, "w", encoding="utf-8") as f:
            _json.dump({"metrics": metrics, "alerts_total": total_alerts,
                        "rules": ev.alerts.rules_dict()}, f, ensure_ascii=False, indent=2)
        print(f"[online] 指标已导出 → {args.online_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
