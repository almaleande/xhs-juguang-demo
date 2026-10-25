#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从聚光 API 拉取区间数据 → 生成快照
====================================
已改造为 core 模块驱动：

    旧版问题（2026-09-17 修复）
    --------------------------
    1. 直接读 access_token 就用，没有任何续期逻辑
       → 续期代码只存在于 _archive/聚光日报_合并版.py，重构时丢失
    2. 拉取失败只打日志就跳过，不计入统计
    3. 城市推断规则没有裸城市名，导致「东营婚纱照示例」这类户名落入「其他」
    4. 快照里没有覆盖率/失败清单字段，下游无从判断数据是否可信

保留 `build_month_report(month_label, start_date, end_date)` 的旧签名，
因为 `拉取单日.py` 与 `半年报.py` 仍在调用它。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import collector, tokenhub  # noqa: E402


def build_month_report(month_label, start_date, end_date):
    """拉取区间数据并落快照，返回快照文件路径（兼容旧调用）"""
    # 演示模式保护：账号库全部为 demo 占位时跳过拉数，避免覆盖本地演示数据
    try:
        accts = tokenhub.load_accounts()
        vals = [v or {} for v in accts.values()]
        if vals and all(str((v.get("access_token") or "")).startswith("demo_") for v in vals):
            print("演示模式：无有效授权，跳过拉数（使用本地演示数据）", flush=True)
            return None
    except Exception:
        pass
    snapshot, report = collector.collect(month_label, start_date, end_date, verbose=True)
    return report["snapshot_file"]


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 3:
        label, start, end = args[0], args[1], args[2]
    elif len(args) == 2:
        start, end = args
        label = start[:7] if len(start) == 10 else start
    elif len(args) == 1:
        label = start = end = args[0]
    else:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        label = start = end = yesterday

    path = build_month_report(label, start, end)
    print(f"\n✅ 快照已生成: {path}")
