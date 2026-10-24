#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聚光日报 · 一键流水线
======================
    token 续期自检 → 拉数 → 数据质量闸门 → 生成日报

用法:
  python run_daily.py                     # 拉昨天，全程带闸门
  python run_daily.py 2026-09-16          # 指定日期
  python run_daily.py 2026-09-01 2026-09-16  # 区间
  python run_daily.py --check             # 只体检，不拉数不出报
  python run_daily.py --pending           # 只看还有哪些主体待授权
  python run_daily.py --no-guard          # 跳过闸门（不推荐）
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import collector, guard, tokenhub  # noqa: E402
from core.paths import BASE_DIR, DATA_DIR  # noqa: E402


def banner(text):
    print(f"\n{'═' * 62}")
    print(f"  {text}")
    print(f"{'═' * 62}")


def show_pending():
    rows = tokenhub.pending_list()
    if not rows:
        print("  ✓ 所有主体授权正常，无待处理项")
        return rows
    print(f"  待授权 / 需重授权 {len(rows)} 个主体：\n")
    print(f"  {'状态':<12s} {'主体':<36s} {'投放户':>6s}")
    print(f"  {'─' * 12} {'─' * 36} {'─' * 6}")
    for r in rows:
        st = {"pending": "未授权", "placeholder": "占位符污染",
              "expired": "已过期"}.get(r["status"], r["status"])
        print(f"  {st:<12s} {r['key'][:36]:<36s} {r['advertisers']:>6d}")
    return rows


def main():
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("--")}
    pos = [a for a in argv if not a.startswith("--")]

    # ---- 只看待授权清单 ----
    if "--pending" in flags:
        banner("待授权主体清单")
        show_pending()
        return 0

    # ---- 只体检 ----
    if "--check" in flags:
        banner("Token 体检（不改动）")
        rep = tokenhub.ensure_valid(verbose=True, dry_run=True)
        banner("待授权主体清单")
        show_pending()
        if rep["need_reauth"]:
            print(f"\n  ⚠ 有 {len(rep['need_reauth'])} 个主体需要重新授权")
        return 0

    # ---- 正常流水线 ----
    if len(pos) >= 2:
        start, end = pos[0], pos[1]
        label = pos[2] if len(pos) > 2 else start
    elif len(pos) == 1:
        start = end = label = pos[0]
    else:
        d = datetime.now() - timedelta(days=1)
        start = end = label = d.strftime("%Y-%m-%d")

    # 演示模式保护：账号库全部为 demo 占位时跳过拉数，避免覆盖本地演示数据
    try:
        accts = tokenhub.load_accounts()
        vals = [v or {} for v in accts.values()]
        if vals and all(str((v.get("access_token") or "")).startswith("demo_") for v in vals):
            banner("演示模式：无有效授权，跳过拉数（使用本地演示数据）")
            return 0
    except Exception:
        pass

    t0 = time.time()
    banner(f"聚光日报流水线 · {label}")

    # ① 采集（内部会先做 token 续期）
    banner("① 拉取数据")
    snapshot, report = collector.collect(label, start, end, verbose=True)

    # ② 质量闸门
    banner("② 数据质量检查")
    prev = guard.load_previous(label)
    issues = guard.check(snapshot, prev)
    print(guard.format_issues(issues))

    if guard.has_fatal(issues) and "--no-guard" not in flags:
        banner("⛔ 已阻断")
        print("  数据存在致命问题，为避免产出误导性日报，本次不出报。")
        print("  请先处理上面标记为「阻断」的问题。")
        print(f"\n  待授权情况：")
        show_pending()
        return 2

    # ③ 生成日报
    banner("③ 生成日报")
    dg = os.path.join(BASE_DIR, "日报生成.py")
    if not os.path.exists(dg):
        print(f"  ✗ 找不到 {dg}")
        return 3
    r = subprocess.run([sys.executable, dg, label],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE_DIR)
    if r.returncode != 0:
        print(f"  ✗ 日报生成失败：\n{r.stdout}\n{r.stderr}")
        return 3
    print(r.stdout.strip() or "  ✓ 日报已生成")

    # ④ 收尾
    s = snapshot["summary"]
    banner("✅ 完成")
    print(f"  区间      {start} ~ {end}")
    print(f"  消耗      ¥{s['total_fee']:,.2f}")
    print(f"  私信/留资 {s['total_msg']} / {s['total_msg_leads']}")
    print(f"  覆盖      有数据 {s.get('advertiser_ok', 0)} 户 | "
          f"零消耗 {s.get('advertiser_empty', 0)} 户 | "
          f"失败 {s.get('advertiser_error', 0)} 户 | "
          f"未授权 {s.get('advertiser_skipped', 0)} 户")
    print(f"  总耗时    {round(time.time() - t0, 1)}s")

    pend = tokenhub.pending_list()
    if pend:
        print(f"\n  ⚠ 仍有 {len(pend)} 个主体待授权，日报覆盖不完整（跑 --pending 看清单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
