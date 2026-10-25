# -*- coding: utf-8 -*-
"""区间档位 · 三页共用单一来源
==============================
投放总览 / 关键词 / 投放诊断 三个页面必须用**同一套**区间档位
（同样的键、同样的标签、同样的顺序），否则切页时用户会以为换了产品。

历史坑：诊断页早期另写了一套（近7日/近14日/近30日/本月/上月），
与总览的 8 档对不上 —— 总览里根本没有「近 14 日 / 近 30 日」，
而诊断页没有「本季度 / 近半年 / 近一年」。现在只在这里定义一次。

基准日：一律以「昨天」为数据基准 —— 聚光当日数据通常未结算。
"""
from datetime import datetime, timedelta
import os
from core.privacy import DEMO_MODE, DEMO_REFERENCE_DATE

# 顺序即页面上的显示顺序
RANGES = [
    ("latest", "昨日"),
    ("3d", "近 3 日"),
    ("7d", "近 7 日"),
    ("month", "本月"),
    ("lastmonth", "上月"),
    ("quarter", "本季度"),
    ("halfyear", "近半年"),
    ("year", "近一年"),
]
RANGE_NAME = dict(RANGES)
# 三页（投放总览 / 关键词 / 投放诊断）共用的默认档位：一律「昨日」。
# 切页不换区间，顶部 6 卡的数字才可比。
# （诊断页原默认 7d，比总览粗一档 → 打开就像换了产品，已统一）
DEFAULT_KEY = "latest"

# 兼容诊断页早期用过的键（旧链接、已采数据文件仍能正确解析）
LEGACY_DAYS = {"today": 1, "prev": 1, "d7": 7, "d14": 14, "d30": 30}


def bounds(key, today=None):
    """区间键 → (起, 止) 的 date；未知键按「昨日」处理。

    today 可由调用方传入；未传时支持环境变量 KB_REF_DATE 固定「今日」基准
    （演示数据锚定在某个日期时，用它覆盖系统实时日期，避免错位取空）。
    """
    if today is None:
        _ref = os.environ.get("KB_REF_DATE") or (DEMO_REFERENCE_DATE if DEMO_MODE else None)
        today = (_ref and datetime.fromisoformat(_ref).date()
                 or datetime.now().date())
    t = today
    y = t - timedelta(days=1)
    if key in ("today", "latest", "prev"):
        return y, y
    if key in LEGACY_DAYS:
        n = LEGACY_DAYS[key]
        return y - timedelta(days=n - 1), y
    if key == "3d":
        return y - timedelta(days=2), y
    if key == "7d":
        return y - timedelta(days=6), y
    if key == "month":
        return min(t.replace(day=1), y), y
    if key == "lastmonth":
        le = t.replace(day=1) - timedelta(days=1)
        return le.replace(day=1), le
    if key == "quarter":
        qm = ((t.month - 1) // 3) * 3 + 1
        return min(t.replace(month=qm, day=1), y), y
    if key == "halfyear":
        return y - timedelta(days=182), y
    if key == "year":
        return y - timedelta(days=364), y
    return y, y


def days(key, today=None):
    s, e = bounds(key, today)
    return (e - s).days + 1


def label(key):
    """标签：标准档位取 RANGES 里的名字，legacy 键退化为「近 N 日」。"""
    if key in RANGE_NAME:
        return RANGE_NAME[key]
    if key in LEGACY_DAYS:
        n = LEGACY_DAYS[key]
        return "昨日" if n == 1 else "近 %d 日" % n
    return key or ""


# 实测定标（2026-09-24，预筛开启、配置层当日缓存命中）：
#   1 天 8.2s ｜ 3 天 7.8s ｜ 31 天(8月) 28s ｜ 85 天(本季度) 28s ｜ 365 天 147s
# 注意：请求量只随 log2(天数) 增长（fetch_dim_rows 在 total>page_size 时二分切段），
# 所以 3 天与 31 天耗时几乎相同；真正拉开差距的是「归档数据要不要回源」。
EST_SEC = {"latest": 10, "3d": 10, "7d": 12, "month": 30,
           "lastmonth": 30, "quarter": 30, "halfyear": 60, "year": 150}


def estimate_from_days(n):
    """按天数粗估诊断采集耗时（秒）。已按 2026-09-24 实测值重新标定。"""
    if n <= 3:
        return 10
    if n <= 7:
        return 12
    if n <= 31:
        return 30
    if n <= 92:
        return 30
    if n <= 190:
        return 60
    return 150


def estimate_by_key(key):
    """按档位键给耗时（秒）；未知键退回按天数估。"""
    if key in EST_SEC:
        return EST_SEC[key]
    return estimate_from_days(days(key))


def estimate_seconds(key, today=None):
    return estimate_from_days(days(key, today))
