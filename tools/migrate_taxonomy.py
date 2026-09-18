#!/usr/bin/env python
"""把受控分类（domain / capabilities）回填到 catalog.json 全部条目。

一次性迁移脚本，可重复执行（幂等）：已存在且合法的显式分类不会被覆盖，
只有缺失/非法的才按 datasets_taxonomy 的映射表补上。

用法：
    python tools/migrate_taxonomy.py --dry-run     # 只看会改什么
    python tools/migrate_taxonomy.py               # 落盘（自动备份 catalog.json）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_harness.core import datasets_admin as da  # noqa: E402
from eval_harness.core import datasets_catalog as dc  # noqa: E402
from eval_harness.core import datasets_taxonomy as tax  # noqa: E402

TAXONOMY_VERSION = "2026-09-18"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="连已存在的显式分类也按映射表重算")
    args = ap.parse_args()

    cat = da._load()
    ds = cat.get("datasets", [])
    changed: list[str] = []

    for d in ds:
        did = d.get("id")
        want_domain = tax.domain_of({**d, "domain": None}) if args.force else tax.domain_of(d)
        want_caps = tax.capabilities_of({**d, "capabilities": None}) if args.force else tax.capabilities_of(d)
        if not tax.is_domain(want_domain):
            want_domain = tax.domain_of(d)
        if d.get("domain") != want_domain or list(d.get("capabilities") or []) != list(want_caps):
            changed.append(f"{did:26} {str(d.get('domain')):18} -> {want_domain:18} {want_caps}")
            d["domain"] = want_domain
            d["capabilities"] = want_caps

    cat["taxonomy_version"] = TAXONOMY_VERSION
    cat["categories_note"] = (
        "分类以受控枚举为准：一级 domain（评测对象域，见 core/datasets_taxonomy.py::DOMAINS）、"
        "二级 capabilities（能力标签，同上 CAPABILITIES）。原 category 字段降级为「细分说明」，"
        "仅作展示，不参与分组与统计。"
    )

    print(f"条目 {len(ds)} · 需变更 {len(changed)}")
    for line in changed:
        print("  ", line)
    if not changed and not args.force:
        print("（无需变更，幂等）")

    # 分类闭合性自检
    bad = [d["id"] for d in ds if not tax.is_domain(str(d.get("domain") or ""))]
    nocap = [d["id"] for d in ds if not (d.get("capabilities") or [])]
    illegal = [(d["id"], c) for d in ds for c in (d.get("capabilities") or [])
               if not tax.is_capability(c)]
    print(f"\n自检：非法 domain {bad or 'none'} · 缺 capabilities {nocap or 'none'} · "
          f"非受控标签 {illegal or 'none'}")

    if args.dry_run:
        print("\n[dry-run] 未写盘。")
        return 0
    if bad or nocap or illegal:
        print("\n自检未通过，拒绝写盘。请先修 datasets_taxonomy.py 的映射表。")
        return 1

    bak = da._atomic_write(cat, backup=True)
    print(f"\n已写盘（备份 {bak}）")

    # 复查：重新加载确认分类真的落进去了
    dc.invalidate_cache()
    rep = da.taxonomy_report()
    print(f"复查：total={rep['total']} 使用中的域={rep['domains_used']}")
    print(f"     缺 domain={rep['missing_domain'] or 'none'} 缺 capabilities={rep['missing_capabilities'] or 'none'}")
    from collections import Counter
    print("     域分布：", dict(Counter(tax.domain_of(d) for d in dc.load_catalog()["datasets"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
