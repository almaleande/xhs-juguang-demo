#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书聚光投放看板 · 局域网服务
=================================
把看板发布成内网可访问的服务，并在页面上直接完成授权、刷新。

页面地址
  http://<本机IP>:8867/              看板（支持 ?range=latest|prev|7d）
  http://<本机IP>:8867/?range=7d     近 7 日视图

接口
  GET  /api/dashboard?range=        原始数据 JSON
  GET  /api/status                  服务与数据源状态
  GET  /api/pending                 待授权主体清单 + 授权链接
  POST /api/auth                    {code, label} 提交 auth_code 完成授权
  POST /api/refresh                 触发重新拉数
  GET  /api/refresh                 同上（便于浏览器直接点）

启动
  python 看板服务.py                 # 默认 8867，每 10 分钟刷新
  python 看板服务.py 9000 5          # 自定义端口 / 刷新间隔(分钟)

环境变量
  KB_PORT / KB_REFRESH_MIN / KB_DAYS
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core import (authflow, collector, diagapi, diagrender, kwdata,  # noqa: E402
                   ranges, tokenhub, balances)
from core.paths import REPORTS_DIR  # noqa: E402
from core.privacy import DEMO_MODE, blocked  # noqa: E402


def _load_kanban():
    path = os.path.join(BASE_DIR, "看板生成.py")
    spec = importlib.util.spec_from_file_location("kanban_gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kanban = _load_kanban()

OUT_HTML = os.path.join(REPORTS_DIR, "小红书投放看板.html")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("KB_PORT", 8867))
REFRESH_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("KB_REFRESH_MIN", 10))
DAYS = int(os.environ.get("KB_DAYS", 1))
# 关键词日库每轮增量回看的天数（本地日库 + 任意区间本地聚合）
KW_PAGE_DAYS = int(os.environ.get("KB_KW_DAYS", "7"))
# 关键词日库首次回填天数（仅当本地日库为空时自动触发，之后只做增量）
KW_BOOTSTRAP_DAYS = int(os.environ.get("KB_KW_BOOTSTRAP", "30"))
# 渲染版本开关：KB_V2=0 切回旧版深色看板，默认新版浅色精致版
V2 = os.environ.get("KB_V2", "1") != "0"

LOCK = threading.RLock()
HTML_CACHE = {}          # range → html
KW_CACHE = {}            # range → (html, kw_snapshot)  关键词页缓存
# 缓存键必须含 rng：同一 (起,止) 用不同档位进入时，**高亮按钮不同**，
# 只按 (起,止) 缓存会把「近 7 日」的 HTML 发给「自定义」入口，导致高亮错位。
DIAG_CACHE = {}          # (start, end, rng) → (html, meta)   投放诊断页缓存
# 诊断采集进度（服务端只有一个采集任务，故用单例状态）
DIAG_STATE = {
    "running": False, "done": 0, "total": 0, "start": None, "end": None,
    "label": "", "started_at": None, "finished_at": None, "elapsed": 0,
    "plans": 0, "ok": 0, "error": None,
}
# 诊断「全部档位」批量采集：一次把 8 个档位依次备好，之后切档位全部秒开。
# 与单档采集共用 DIAG_STATE（同一时刻只允许一个采集任务），额外用下面的字段记录批量进度。
DIAG_BATCH = {
    "running": False, "total": 0, "done": 0,
    "current": "", "started_at": None, "finished_at": None,
    "elapsed": 0, "ok": [], "skip": [], "fail": [], "cancel": False,
}
NEED_REFRESH = threading.Event()   # 刷新排队标志：刷新期间来的请求不丢，本轮结束后补跑
STATE = {
    "started_at": time.time(),
    "last_build": None,
    "last_refresh": None,
    "refreshing": False,
    "last_error": None,
    "period": "",
    "summary": {},
    "snapshot": None,
    "build_sec": 0,
    "last_auth": None,
    "force_creative": False,
}

# 区间档位：三页共用单一来源（core/ranges.py）
# 总览 / 关键词 / 投放诊断 必须用同一套键与标签，否则切页会像换了产品。
RANGES = ranges.RANGES
RANGE_NAME = ranges.RANGE_NAME

# 这些区间能拉到真正的「创意明细」（含素材封面/笔记），走素材粒度视图。
# 判据：page_size 实测上限 1000，这些区间单户行数在 1~2 页内，成本可控。
# 更长的区间（季度/半年/一年）创意行数到万级，仍走账户粒度 + 代表封面。
CREATIVE_RANGES = ("3d", "7d", "month", "lastmonth")

# 诊断采集预筛：先用账户日库筛出「区间内真有消耗的户」再拉计划维度。
# 实测与全量采集零漏零多（09-22 单日 28/28、08 月 34/34），165 户 → 29 户，省 82% 请求，
# 单档耗时 103s → 约 18s。日库缺失/未覆盖区间时自动退化为全量，不会开天窗。
# 设 KB_DIAG_LIVE=0 可关闭（排查采集完整性问题时用）。
DIAG_LIVE_FILTER = os.environ.get("KB_DIAG_LIVE", "1") != "0"
# 诊断自动刷新：每日刷新时把「还没有的档位」预先采好，这样切任意档位都是秒开。
# KB_DIAG_AUTOREFRESH 取值：
#   full / 1（默认）→ 补齐 8 档里所有缺的（首次 8 档共约 2~3 分钟，日常只补「昨日」1 档 ≈18s）
#   latest          → 只预采「昨日」单档（最省）
#   0 / off         → 关闭
# 判据是「区间文件是否存在」，所以同一数据日无论刷新多少轮，只会真正采一次。
DIAG_AUTOREFRESH = os.environ.get("KB_DIAG_AUTOREFRESH", "1").lower() not in ("0", "off", "false", "no")
_dar_mode = os.environ.get("KB_DIAG_AUTOREFRESH", "1").lower()
DIAG_AUTOREFRESH_MODE = "latest" if _dar_mode == "latest" else "full"


# ---------------- 数据 ----------------

def daily_days():
    return sorted(d for d in kanban.list_snapshots() if len(d) == 10)


def range_bounds(key):
    """区间键 → (起, 止) 日期。实现已抽到 core/ranges.py，三页共用同一套口径。"""
    return ranges.bounds(key)


def prev_bounds(key):
    """区间键 → 上一周期 (起, 止)。用于 KPI 环比。"""
    s, e = range_bounds(key)
    n = (e - s).days + 1
    if key == "month":
        # 上月同期：上月 1 号起、同样天数
        prev_end0 = s.replace(day=1) - timedelta(days=1)
        prev_start = prev_end0.replace(day=1)
        return prev_start, min(prev_end0, prev_start + timedelta(days=n - 1))
    if key == "lastmonth":
        prev_end = s.replace(day=1) - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    if key == "quarter":
        qm = ((s.month - 1) // 3) * 3 + 1
        cur_start = s.replace(month=qm, day=1)
        prev_end = cur_start - timedelta(days=1)
        pq = ((prev_end.month - 1) // 3) * 3 + 1
        return prev_end.replace(month=pq, day=1), prev_end
    return s - timedelta(days=n), s - timedelta(days=1)


def mom_pair(rng):
    """
    KPI 环比用的两个总量。关键是**两边同口径**：
      - 昨日/今日：都用日快照（创意粒度），本地已有，零成本
      - 区间：两边都用账户日粒度缓存（账户口径）
    绝不拿「创意口径的本期」去比「账户口径的上期」。
    """
    days = daily_days()
    if rng in ("latest", "today"):
        if len(days) >= 2:
            try:
                cur = kanban.snapshot_totals(kanban.load_day(days[-1]))
                prev = kanban.snapshot_totals(kanban.load_day(days[-2]))
                if cur and prev:
                    return {"cur": cur, "prev": prev}
            except Exception as ex:
                print(f"[{datetime.now():%H:%M:%S}] 环比(日快照)失败: {ex}", flush=True)
        return None
    try:
        s, e = range_bounds(rng)
        ps, pe = prev_bounds(rng)
        cur = kanban.snapshot_totals(
            collector.snapshot_from_daily(s.isoformat(), e.isoformat(), ""))
        prev = kanban.snapshot_totals(
            collector.snapshot_from_daily(ps.isoformat(), pe.isoformat(), ""))
        if cur and prev:
            return {"cur": cur, "prev": prev}
    except Exception as ex:
        print(f"[{datetime.now():%H:%M:%S}] 环比(区间)失败: {ex}", flush=True)
    return None


def snapshot_for_range(rng, start=None, end=None):
    """返回 (snapshot, 标签文本)"""
    # 自定义区间：起止日期任意。账户级本地秒出；素材明细按需云端拉取（≤92天，避免过慢/限流）
    if rng == "custom" and start and end:
        label = f"{start} ~ {end}"
        base = collector.snapshot_from_daily(start, end, label)
        if base:
            try:
                d0 = datetime.fromisoformat(start)
                d1 = datetime.fromisoformat(end)
                if 0 <= (d1 - d0).days <= 92:
                    ck = f"custom_{start}_{end}"
                    try:
                        collector.collect_range_creative(ck, start, end, verbose=False)
                    except Exception as ex:
                        print(f"[{datetime.now():%H:%M:%S}] 自定义区间创意拉取失败: {ex}", flush=True)
                    snap = collector.snapshot_from_range_creative(ck, label, base=base)
                    if snap:
                        return snap, label
            except Exception as ex:
                print(f"[{datetime.now():%H:%M:%S}] 自定义区间处理异常: {ex}", flush=True)
            return base, label
    days = daily_days()
    if rng == "latest" and days:
        # 昨日优先用本地日快照（素材级明细最完整）
        return kanban.load_day(days[-1]), days[-1]
    if rng in RANGE_NAME:
        s, e = range_bounds(rng)
        base = collector.snapshot_from_daily(s.isoformat(), e.isoformat(),
                                             RANGE_NAME.get(rng, rng))
        # 短中区间优先用「创意明细」—— 这样区间视图同样能看到素材与封面。
        # 没有缓存时（比如刚部署还没刷新完）自动退回账户粒度，不会开天窗。
        if rng in CREATIVE_RANGES:
            snap = collector.snapshot_from_range_creative(
                rng, RANGE_NAME.get(rng, rng), base=base)
            if snap:
                return snap, f"{s} ~ {e}"
        if base:
            return base, f"{s} ~ {e}"
    if days:
        return kanban.load_day(days[-1]), days[-1]
    return None, ""


def html_for_range(rng, force=False, start=None, end=None):
    """返回 (html, snapshot)；缓存里也存成元组"""
    ok_ranges = [r[0] for r in RANGES]
    if not (rng in ok_ranges or (rng == "custom" and start and end)):
        rng = "latest"
    cache_key = rng if rng != "custom" else f"custom_{start}_{end}"
    with LOCK:
        if not force and cache_key in HTML_CACHE:
            return HTML_CACHE[cache_key]
    snap, label = snapshot_for_range(rng, start, end)
    if snap is None:
        return ("<h3 style='font-family:sans-serif;padding:40px'>还没有任何快照数据</h3>", None)
    avail = []
    days = daily_days()
    dcache = collector.load_account_daily()
    for key, name in RANGES:
        if key == "latest" and not days:
            continue
        if key != "latest" and not dcache:
            continue          # 还没有日粒度缓存时，只给「昨日」
        avail.append((key, name))
    if rng == "custom" and start and end:
        avail.append(("custom", f"自定义 {start}~{end}"))
    last_ref = (datetime.fromtimestamp(STATE["last_refresh"]).strftime("%Y-%m-%d %H:%M:%S")
                if STATE.get("last_refresh") else None)
    if V2:
        html = kanban.build_html_v2(snap, ranges=avail, current_range=rng,
                                    last_refresh=last_ref, start=start, end=end)
    else:
        html = kanban.build_html(snap, title_suffix="", ranges=avail,
                                 current_range=rng, mom=mom_pair(rng))
    with LOCK:
        HTML_CACHE[cache_key] = (html, snap)
        STATE["lastRange"] = rng
        # 只有最新视图才更新主状态，避免切到 7 日视图把 period 带偏
        if rng == "latest":
            STATE["snapshot"] = snap
            STATE["period"] = snap.get("date", "")
            STATE["summary"] = snap.get("summary", {})
    return html, snap


# ---------------- 关键词页 ----------------

def kw_bounds(rng, start=None, end=None):
    """关键词页的区间 → (起, 止, 标签)。与素材看板同一套区间语义。"""
    if rng == "custom" and start and end:
        return start, end, f"{start} ~ {end}"
    s, e = range_bounds(rng)
    return s.isoformat(), e.isoformat(), RANGE_NAME.get(rng, rng)


def kw_snapshot_for_range(rng, start=None, end=None, filt=None):
    s, e, label = kw_bounds(rng, start, end)
    f = filt or {}
    return kwdata.keyword_snapshot(s, e, label,
                                   city=f.get("city") or None,
                                   adv=f.get("adv") or None,
                                   owner=f.get("owner") or None)


def html_for_keyword(rng, force=False, start=None, end=None, filt=None):
    """关键词页渲染（本地日库聚合，零 API 请求，秒出）

    filt = {"city":…, "adv":…, "owner":…}：筛选后是**重新聚合**，不是前端过滤行，
    所以缓存键必须带上筛选条件 —— 否则切换筛选会拿到上一组筛选的页面。
    """
    ok_ranges = [r[0] for r in RANGES]
    if not (rng in ok_ranges or (rng == "custom" and start and end)):
        rng = "latest"
    f = filt or {}
    cache_key = rng if rng != "custom" else f"custom_{start}_{end}"
    fk = "|".join(f.get(k) or "" for k in ("city", "adv", "owner"))
    if fk.strip("|"):
        cache_key = f"{cache_key}|{fk}"
    with LOCK:
        if not force and cache_key in KW_CACHE:
            return KW_CACHE[cache_key]
    snap = kw_snapshot_for_range(rng, start, end, f)
    avail = [(k, n) for k, n in RANGES]
    if rng == "custom" and start and end:
        avail.append(("custom", f"自定义 {start}~{end}"))
    last_ref = (datetime.fromtimestamp(STATE["last_refresh"]).strftime("%Y-%m-%d %H:%M:%S")
                if STATE.get("last_refresh") else None)
    html = kanban.build_keyword_html(snap, ranges=avail, current_range=rng,
                                     start=start, end=end, last_refresh=last_ref)
    with LOCK:
        KW_CACHE[cache_key] = (html, snap)
    return html, snap


# ---------------- 投放诊断页（计划级） ----------------
# 与日报的根本差别：诊断数据是**按区间现采**的（配置接口 + 计划级离线报表），
# 单区间全量约 70~90s，不可能每次切区间都现拉。所以：
#   ① 区间档位比日报粗（计划级诊断需要更长区间才有统计意义）；
#   ② 磁盘即缓存：data/diag/plans_<start>_<end>.json 命中就秒出；
#   ③ 未命中 → 引导页 + 一键后台采集 + 进度轮询，采完自动刷新。

def diag_bounds(rng, start=None, end=None):
    """诊断区间 → (起, 止, 标签)。基准日同样是「昨天」（当日数据未结算）。

    支持三种形态：档位键（与总览/关键词**同一套**：latest/3d/7d/month/lastmonth/
    quarter/halfyear/year）、custom+start/end、已采集区间直连键 custom|起|止。
    另兼容诊断页早期用过的 d7/d14/d30（旧链接与已采数据文件仍可解析）。
    """
    if rng and rng.startswith("custom|"):
        parts = rng.split("|")
        if len(parts) == 3:
            return parts[1], parts[2], "%s ~ %s" % (parts[1], parts[2])
    if rng == "custom" and start and end:
        return start, end, "%s ~ %s" % (start, end)
    key = rng or ranges.DEFAULT_KEY
    if key not in ranges.RANGE_NAME and key not in ranges.LEGACY_DAYS:
        return None, None, ""
    s0, e0 = ranges.bounds(key)
    return s0.isoformat(), e0.isoformat(), ranges.label(key)


def diag_file(s, e):
    return os.path.join(diagapi.DIAG_DIR, "plans_%s_%s.json" % (s, e))


def diag_available():
    """本地已采集区间 [(start, end)]，按起始日倒序。"""
    out = []
    d = diagapi.DIAG_DIR
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if fn.startswith("plans_") and fn.endswith(".json"):
            core = fn[len("plans_"):-len(".json")]
            if len(core) == 21 and core[10] == "_":
                out.append((core[:10], core[11:]))
    out.sort(reverse=True)
    return out


def diag_range_list(cur_s=None, cur_e=None):
    """区间按钮：与投放总览/关键词**完全一致**的 8 档（「自定义」按钮在模板里）。

    早期会把「本地已采集但不在档位里的区间」也塞进按钮条 —— 那会让诊断页比另两页
    多出按钮，破坏一致性。这类区间改从两处进入：
      · 点「自定义▾」手选起止日期（会自动指向已采文件，秒开）
      · 未采集时引导页里的「本地已采集」快捷入口
    """
    return list(diagrender.RANGES)


def diag_collect_async(s, e, label=""):
    """后台采集指定区间（同一时刻只允许一个任务）。"""
    with LOCK:
        if DIAG_STATE["running"]:
            return False
        DIAG_STATE.update({"running": True, "done": 0, "total": 0, "start": s,
                           "end": e, "label": label, "started_at": time.time(),
                           "finished_at": None, "elapsed": 0, "plans": 0,
                           "ok": 0, "error": None})

    def _run():
        t0 = time.time()
        try:
            def cb(n, tot, lb):
                with LOCK:
                    DIAG_STATE["done"] = n
                    DIAG_STATE["total"] = tot
                    DIAG_STATE["label"] = lb
                    DIAG_STATE["elapsed"] = round(time.time() - t0, 1)

            payload, stats = diagapi.collect_plans(
                s, e, progress_cb=cb, use_live_filter=DIAG_LIVE_FILTER)
            n = len(payload.get("plans") or [])
            if n:
                diagapi.save_plans(payload)
                try:
                    with open(os.path.join(diagapi.DIAG_DIR, "latest.txt"),
                              "w", encoding="utf-8") as f:
                        f.write("plans_%s_%s.json" % (s, e))
                except Exception:
                    pass
            with LOCK:
                DIAG_STATE["plans"] = n
                DIAG_STATE["ok"] = stats.get("ok")
                if not n:
                    DIAG_STATE["error"] = "未取到任何计划（token 失效或该区间无投放）"
                DIAG_CACHE.clear()
            print("[%s] 诊断采集完成 %s~%s · %d 计划 · %.0fs"
                  % (datetime.now().strftime("%H:%M:%S"), s, e, n, time.time() - t0),
                  flush=True)
        except Exception as ex:
            traceback.print_exc()
            with LOCK:
                DIAG_STATE["error"] = "%s: %s" % (type(ex).__name__, ex)
        finally:
            with LOCK:
                DIAG_STATE["running"] = False
                DIAG_STATE["finished_at"] = time.time()
                DIAG_STATE["elapsed"] = round(time.time() - t0, 1)

    threading.Thread(target=_run, daemon=True).start()
    return True


def diag_plan_file(s, e):
    return os.path.join(diagapi.DIAG_DIR, "plans_%s_%s.json" % (s, e))


def diag_batch_plan(only_missing=True):
    """列出 8 个标准档位的采集计划：[(key, label, s, e, exists), ...]。"""
    out = []
    for k, lab in RANGES:
        s0, e0 = ranges.bounds(k)
        s, e = s0.strftime("%Y-%m-%d"), e0.strftime("%Y-%m-%d")
        ex = os.path.exists(diag_plan_file(s, e))
        if only_missing and ex:
            continue
        out.append((k, lab, s, e, ex))
    return out


def diag_collect_batch_async(keys=None, force=False):
    """把 8 个标准档位依次采集备好（已存在的默认跳过，除非 force）。

    设计要点：
      · 与单档采集**串行** —— 共用 DIAG_STATE，同一时刻只有一个采集任务，
        避免 8 档并发把 API 限流（410029）打满，也不会把 CPU 拉满；
      · 默认只补「缺的」档位 —— 已采过的区间直接跳过，所以日常点一次只补 0~3 档；
      · 单档失败不中断整体，记录到 fail 列表继续下一档；
      · 每采完一档清一次页面缓存，保证切过去就是新的。
    """
    with LOCK:
        if DIAG_STATE["running"]:
            return None
    # ⚠ 先用「全量档位」再按 keys 过滤，最后才按 only_missing 收窄 ——
    # 否则 keys 指定的档位会被「只补缺失」提前筛掉，导致指定重采失效。
    plan = diag_batch_plan(only_missing=False)
    if keys:
        want = set(keys)
        plan = [p for p in plan if p[0] in want]
    if not force:
        plan = [p for p in plan if not p[4]]     # 跳过已存在的
    if not plan:
        return []

    with LOCK:
        DIAG_BATCH.update({
            "running": True, "total": len(plan), "done": 0,
            "current": "", "started_at": time.time(), "finished_at": None,
            "elapsed": 0, "ok": [], "skip": [], "fail": [], "cancel": False,
        })

    def _run():
        t0 = time.time()
        try:
            for i, (k, lab, s, e, _ex) in enumerate(plan):
                with LOCK:
                    if DIAG_BATCH["cancel"]:
                        break
                    DIAG_BATCH["current"] = lab
                    DIAG_BATCH["elapsed"] = round(time.time() - t0, 1)
                try:
                    payload, stats = diagapi.collect_plans(
                        s, e, verbose=False, use_live_filter=DIAG_LIVE_FILTER)
                    n = len(payload.get("plans") or [])
                    if n:
                        diagapi.save_plans(payload)
                        try:
                            with open(os.path.join(diagapi.DIAG_DIR, "latest.txt"),
                                      "w", encoding="utf-8") as f:
                                f.write("plans_%s_%s.json" % (s, e))
                        except Exception:
                            pass
                        with LOCK:
                            DIAG_CACHE.clear()
                        print("[%s] 批量采集 [%d/%d] %s %s~%s · %d 计划 · %.0fs"
                              % (datetime.now().strftime("%H:%M:%S"), i + 1, len(plan),
                                 lab, s, e, n, time.time() - t0), flush=True)
                    else:
                        print("[%s] 批量采集 [%d/%d] %s 未取到计划，跳过"
                              % (datetime.now().strftime("%H:%M:%S"), i + 1, len(plan), lab),
                              flush=True)
                except Exception as ex:
                    with LOCK:
                        DIAG_BATCH["fail"].append({"label": lab, "msg": "%s: %s" % (type(ex).__name__, ex)})
                    print("[%s] 批量采集 %s 失败: %s" % (datetime.now().strftime("%H:%M:%S"), lab, ex),
                          flush=True)
                with LOCK:
                    DIAG_BATCH["done"] = i + 1
                    DIAG_BATCH["ok"].append(lab)
                    DIAG_BATCH["elapsed"] = round(time.time() - t0, 1)
        finally:
            with LOCK:
                DIAG_BATCH["running"] = False
                DIAG_BATCH["current"] = ""
                DIAG_BATCH["finished_at"] = time.time()
                DIAG_BATCH["elapsed"] = round(time.time() - t0, 1)
            print("[%s] 批量采集结束 · %d/%d 档 · %.0fs"
                  % (datetime.now().strftime("%H:%M:%S"),
                     len(DIAG_BATCH["ok"]), len(plan), time.time() - t0), flush=True)

    threading.Thread(target=_run, daemon=True).start()
    return plan


def _diag_css():
    """直接复用诊断模板的样式，保证引导页与正式页视觉一致。"""
    try:
        with open(diagrender.TEMPLATE_FILE, encoding="utf-8") as f:
            t = f.read()
        i, j = t.find("<style>"), t.find("</style>")
        if 0 <= i < j:
            from core import layout as _lay
            return t[i + 7:j].replace("__TOP_STABLE_CSS__", _lay.TOP_STABLE_CSS)
    except Exception:
        pass
    return ""


def _diag_pgtab():
    return ('<div class="pgtab">'
            '<a class="pt" href="/" title="素材投放总览">投放总览</a>'
            '<a class="pt" href="/keyword" title="关键词（搜索词）看板">关键词</a>'
            '<a class="pt active" href="/diag" title="计划级投放诊断">投放诊断</a>'
            "</div>")


def _diag_hero_side(s, e):
    """hero 右侧：页签 + 采集状态块（与总览/关键词的 hero-side 同构）。"""
    return (_diag_pgtab() +
            '<div class="refreshbar"><span class="dot busy"></span>'
            '<span class="rf-txt">区间 <b>%s ~ %s</b> · 待采集</span></div>' % (s, e))


def _diag_est_label(s, e):
    """把区间长度换算成「约 X 秒 / 约 X 分钟」的采集耗时提示。"""
    try:
        n = (datetime.strptime(e, "%Y-%m-%d").date()
             - datetime.strptime(s, "%Y-%m-%d").date()).days + 1
    except Exception:
        n = 7
    sec = ranges.estimate_from_days(max(1, n))
    if sec < 90:
        return "%d 秒" % sec
    return "%.1f 分钟" % (sec / 60.0)


def diag_guide_html(s, e, label, rng, start, end):
    """未采集区间的引导页：说明 + 已采区间捷径 + 一键采集 + 进度轮询。"""
    ranges = diag_range_list(s, e)
    rtabs = diagrender.range_html(ranges, rng,
                                 custom_range=(start, end) if rng == "custom" else None,
                                 base="/diag")
    avail = diag_available()
    if avail:
        links = "".join(
            '<a class="rtab" href="/diag?range=custom|%s|%s">%s ~ %s</a>' % (a, b, a, b)
            for a, b in avail[:6])
        avail_html = ('<div class="gline"><span class="gk">本地已采集</span>'
                      '<div class="rtabs g-rtabs">%s</div></div>' % links)
    else:
        avail_html = ('<div class="gline"><span class="gk">本地已采集</span>'
                      '<span class="gv">暂无 —— 点下方按钮采集第一个区间</span></div>')
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小红书聚光投放工作台-AI自动化分析决策系统</title>
<style>%s
.wrap{max-width:1440px;margin:0 auto;padding:0 32px 80px}
.gsec{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:26px 28px;
  margin-top:16px;box-shadow:var(--shadow)}
.gsec h2{font-size:17px;font-family:var(--serif);font-weight:400;margin-bottom:10px}
.gsub{font-size:13px;color:var(--ink2);line-height:1.9;margin-bottom:16px}
.gsub b{color:var(--ink)}
.gline{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.gk{font-size:12.5px;color:var(--ink3);flex:none}
.gv{font-size:12.5px;color:var(--ink2)}
.g-rtabs{margin-left:0}
.gbtns{display:flex;align-items:center;gap:12px;margin-top:18px;flex-wrap:wrap}
.big{font-size:14px;padding:10px 20px;border-radius:11px;border:1px solid var(--gold);
  background:var(--gold);color:#fff;font-family:inherit;cursor:pointer;font-weight:600}
.big:hover{background:var(--gold-deep);border-color:var(--gold-deep)}
.big[disabled]{opacity:.5;cursor:default}
.big.alt{background:transparent;color:var(--gold-deep);border:1px solid var(--gold)}
.big.alt:hover{background:rgba(200,169,106,.10)}
.big.alt em{font-style:normal;font-size:12px;opacity:.75;margin-left:6px}
.gtip{font-size:12px;color:var(--ink3)}
.prog{margin-top:18px;display:none}
.prog.on{display:block}
.pbar{height:9px;background:#F0EEE9;border-radius:6px;overflow:hidden}
.pfill{height:100%%;width:0;background:linear-gradient(90deg,var(--gold),#C7A65F);transition:width .3s}
.ptxt{font-size:12.5px;color:var(--ink2);margin-top:8px}
.perr{color:var(--red)}
</style></head>
<body><div class="wrap">
  <header class="hero"><div class="hero-row1">
    <div class="brand"><div><h1><span class="b-name">小红书聚光投放工作台</span><span class="b-desc">AI 驱动 · 自动分析与投放决策</span></h1></div></div>
    <div class="hero-side">%s</div>
  </div>
  <div class="kpis" id="kpis">__GUIDE_KPIS__</div>
</header>

  <nav class="secnav" id="secNav">
    <span class="nav-item active">计划诊断明细</span>
    <div class="rtabs nav-rtabs" id="navRtabs">%s
      <button class="rtab rtab-custom" id="btnCustomRange" type="button" title="自定义起止日期">自定义▾</button>
      <div class="rangepop" id="rangePop" hidden>
        <div class="rangepop-title">自定义区间</div>
        <div class="rangepop-quick">
          <button class="qchip" data-d="7" type="button">近7日</button>
          <button class="qchip" data-d="30" type="button">近30日</button>
          <button class="qchip" data-d="90" type="button">近90日</button>
          <button class="qchip" data-m="this" type="button">本月</button>
          <button class="qchip" data-m="last" type="button">上月</button>
        </div>
        <div class="rangepop-row">
          <label>起<input type="date" id="rangeStart" class="datein" value="%s"></label>
          <span class="crange-sep">→</span>
          <label>止<input type="date" id="rangeEnd" class="datein" value="%s"></label>
        </div>
        <div class="rangepop-actions">
          <span class="rangepop-hint" id="rangeHint"></span>
          <button class="btn-ghost" id="btnRangeClear" type="button">重置</button>
          <button class="btn-apply" id="btnApplyRange" type="button">应用</button>
        </div>
      </div>
    </div>
  </nav>

  <div class="gsec">
    <h2>该区间尚未采集</h2>
    <div class="gsub">
      区间 <b>%s ~ %s</b>（%s）在本地还没有数据。<br>
      诊断看板按「计划级」采集 —— 每户要拉计划配置 + 计划级离线报表，
      本区间全量约需 <b>__EST_SEC__</b>（已采集过的区间秒开，配置层当日缓存可复用；
      区间越长请求越多，近 7 日约 50 秒、本季度约 2.5 分钟）。
    </div>
    %s
    <div class="gbtns">
      <button class="big" id="btnGo">立即采集这个区间</button>
      <button class="big alt" id="btnAll" title="把 8 个档位里还没有的都采好，之后切档位全部秒开">一键备好全部档位<em id="allN"></em></button>
      <span class="gtip">采集在服务端后台跑，可以关掉本页稍后再来</span>
    </div>
    <div class="prog" id="bprog">
      <div class="pbar"><div class="pfill" id="bfill"></div></div>
      <div class="ptxt" id="btxt">准备中…</div>
    </div>
    <div class="prog" id="prog">
      <div class="pbar"><div class="pfill" id="pfill"></div></div>
      <div class="ptxt" id="ptxt">准备中…</div>
    </div>
  </div>
</div>
<script>
var RANGE = "%s";
(function(){
  var btn = document.getElementById("btnCustomRange"), pop = document.getElementById("rangePop");
  var si = document.getElementById("rangeStart"), ei = document.getElementById("rangeEnd");
  var hint = document.getElementById("rangeHint");
  // 当前就是自定义区间：「自定义」按钮高亮，并把起止回填
  if((RANGE === "custom" || RANGE.indexOf("custom|") === 0)){
    btn.classList.add("active");
    var pt = RANGE.split("|");
    if(pt.length === 3){ if(!si.value) si.value = pt[1]; if(!ei.value) ei.value = pt[2]; }
  }
  function fmt(dt){ function p(n){ return (n<10?"0":"")+n; } return dt.getFullYear()+"-"+p(dt.getMonth()+1)+"-"+p(dt.getDate()); }
  function upd(){
    if(!hint) return;
    if(!si.value || !ei.value){ hint.textContent = ""; return; }
    var n = Math.round((new Date(ei.value) - new Date(si.value))/86400000) + 1;
    hint.textContent = n > 0 ? ("共 " + n + " 天") : "起止日期有误";
  }
  btn.addEventListener("click", function(e){ e.stopPropagation(); pop.hidden = !pop.hidden; upd(); });
  document.addEventListener("click", function(){ pop.hidden = true; });
  pop.addEventListener("click", function(e){ e.stopPropagation(); });
  Array.prototype.forEach.call(pop.querySelectorAll(".qchip"), function(c){
    c.addEventListener("click", function(){
      var t = new Date(), y = new Date(t.getTime() - 86400000);
      if(c.getAttribute("data-d")){
        var n = parseInt(c.getAttribute("data-d"), 10);
        si.value = fmt(new Date(y.getTime() - (n-1)*86400000)); ei.value = fmt(y);
      } else if(c.getAttribute("data-m") === "this"){
        si.value = fmt(new Date(t.getFullYear(), t.getMonth(), 1)); ei.value = fmt(t);
      } else {
        si.value = fmt(new Date(t.getFullYear(), t.getMonth()-1, 1));
        ei.value = fmt(new Date(t.getFullYear(), t.getMonth(), 0));
      }
      upd();
    });
  });
  if(si) si.addEventListener("change", upd);
  if(ei) ei.addEventListener("change", upd);
  var clr = document.getElementById("btnRangeClear");
  if(clr) clr.addEventListener("click", function(){ si.value = ""; ei.value = ""; upd(); });
  document.getElementById("btnApplyRange").addEventListener("click", function(){
    var a = si.value, b = ei.value;
    if(!a || !b){ alert("请先选择起止日期"); return; }
    if(a > b){ var t = a; a = b; b = t; }
    location.href = "/diag?range=custom&start=" + a + "&end=" + b;
  });
  upd();

  // ---- 一键备好全部档位 ----
  var bAll = document.getElementById("btnAll"), bN = document.getElementById("allN"),
      bProg = document.getElementById("bprog"), bFill = document.getElementById("bfill"),
      bTxt = document.getElementById("btxt"), bTimer = null;
  function bPoll(){
    fetch("/api/diag/batch/status").then(function(r){ return r.json(); }).then(function(st){
      if(st.running){
        bProg.classList.add("on"); bAll.disabled = true;
        bAll.textContent = "备档中…";
        var p = st.total ? Math.round(st.done / st.total * 100) : 0;
        bFill.style.width = p + "%%";
        bTxt.textContent = "已备 " + st.done + " / " + st.total + " 档"
          + (st.current ? (" · 正在采 " + st.current) : "")
          + " · 已用 " + st.elapsed + "s";
      } else if(st.finished_at){
        bFill.style.width = "100%%";
        bTxt.textContent = "全部档位已备好（" + st.ok.length + " 档），正在刷新…";
        if(bTimer) clearInterval(bTimer);
        setTimeout(function(){ location.reload(); }, 900);
      }
    }).catch(function(){});
  }
  function bLoadPlan(){
    fetch("/api/diag/batch/plan").then(function(r){ return r.json(); }).then(function(j){
      if(bN) bN.textContent = j.n_missing > 0 ? ("（缺 " + j.n_missing + " 档）") : "（已齐备）";
      bAll.disabled = !!j.running;
      if(j.running){ bProg.classList.add("on"); bPoll(); if(bTimer) clearInterval(bTimer); bTimer = setInterval(bPoll, 1500); }
    }).catch(function(){});
  }
  if(bAll){
    bAll.addEventListener("click", function(){
      bAll.disabled = true; bTxt.textContent = "启动中…"; bProg.classList.add("on");
      fetch("/api/diag/batch/collect").then(function(r){ return r.json(); }).then(function(j){
        bTxt.textContent = j.msg || "已开始…";
        if(!j.ok){ bAll.disabled = false; }
        bPoll();
        if(bTimer) clearInterval(bTimer);
        bTimer = setInterval(bPoll, 1500);
      }).catch(function(){ bAll.disabled = false; bTxt.textContent = "启动失败，请刷新页面重试"; });
    });
    bLoadPlan();
  }

  var go = document.getElementById("btnGo"), prog = document.getElementById("prog"),
      fill = document.getElementById("pfill"), txt = document.getElementById("ptxt"), timer = null;
  function poll(){
    fetch("/api/diag/status").then(function(r){ return r.json(); }).then(function(st){
      if(st.running){
        prog.classList.add("on"); go.disabled = true; go.textContent = "采集中…";
        var p = st.total ? Math.round(st.done / st.total * 100) : 0;
        fill.style.width = p + "%%";
        txt.textContent = "已处理 " + st.done + " / " + st.total + " 个投放户 · 已用 "
          + st.elapsed + "s" + (st.label ? (" · 当前 " + st.label) : "");
      } else {
        if(st.error){
          prog.classList.add("on");
          txt.innerHTML = "<span class='perr'>采集失败：" + st.error + "</span>";
          go.disabled = false; go.textContent = "重试采集";
          if(timer) clearInterval(timer);
        } else if(st.finished_at && st.plans){
          fill.style.width = "100%%";
          txt.textContent = "采集完成：" + st.plans + " 条计划，正在刷新…";
          setTimeout(function(){ location.reload(); }, 800);
          if(timer) clearInterval(timer);
        }
      }
    }).catch(function(){});
  }
  go.addEventListener("click", function(){
    go.disabled = true; go.textContent = "启动中…";
    fetch("/api/diag/collect?range=" + encodeURIComponent(RANGE))
      .then(function(r){ return r.json(); }).then(function(j){
        prog.classList.add("on");
        txt.textContent = j.msg || "已开始采集…";
        if(!j.ok){ go.disabled = false; go.textContent = "立即采集这个区间"; }
        poll();
        if(timer) clearInterval(timer);
        timer = setInterval(poll, 1200);
      }).catch(function(){
        go.disabled = false; go.textContent = "立即采集这个区间";
        txt.textContent = "启动失败，请刷新页面重试";
        prog.classList.add("on");
      });
  });
  // 进入页面时若已有任务在跑，直接接管进度
  poll();
})();
</script>
</body></html>"""
    html = html % (_diag_css(), _diag_hero_side(s, e), rtabs, start or "", end or "",
                   s, e, label, avail_html, rng)
    # 顶部恒定：与正式页同结构的 6 张 KPI 骨架卡（保证 hero 高度一致，切页不跳动）
    # 骨架卡：标签与投放总览同 6 项（口径见 core/kpi_spec.py）；无数据一律中性灰
    _kpi_labels = ("消耗", "点击率", "开口率", "留资率", "客资成本", "点击成本")
    _skeleton = "".join(
        '<div class="kpi"><div class="kpi-label">%s</div>'
        '<div class="kpi-val">—</div>'
        '<div class="kpi-src">待采集</div>'
        '<div class="kpi-mom flat">—</div></div>' % lab for lab in _kpi_labels)
    return (html.replace("__EST_SEC__", _diag_est_label(s, e))
                .replace("__GUIDE_KPIS__", _skeleton))


def html_for_diag(rng, force=False, start=None, end=None):
    """返回 (html, meta)。meta.ok=False 表示该区间未采集（html 是引导页）。"""
    s, e, label = diag_bounds(rng, start, end)
    if not s:
        rng = diagrender.DEFAULT_RANGE
        s, e, label = diag_bounds(diagrender.DEFAULT_RANGE)
    with LOCK:
        ckey = (s, e, rng)
        if not force and ckey in DIAG_CACHE:
            return DIAG_CACHE[ckey]

    f = diag_file(s, e)
    if os.path.exists(f):
        payload = diagapi.load_plans(f)
        rows, summary = diagrender.diagnose_payload(payload)
        is_custom = (rng == "custom" or str(rng).startswith("custom|"))
        html = diagrender.build_diag_html(
            payload, ranges=diag_range_list(s, e), current_range=rng,
            start=(start or s) if is_custom else None,
            end=(end or e) if is_custom else None,
            rows=rows, summary=summary, base="/diag", secnav=True)
        meta = {"ok": True, "range": {"start": s, "end": e, "label": label},
                "plans": len(rows), "summary": summary,
                "stats": payload.get("stats") or {}}
    else:
        html = diag_guide_html(s, e, label, rng, start, end)
        meta = {"ok": False, "range": {"start": s, "end": e, "label": label},
                "avail": [{"start": a, "end": b} for a, b in diag_available()],
                "running": DIAG_STATE["running"]}

    with LOCK:
        DIAG_CACHE[ckey] = (html, meta)
    return html, meta


def write_static(html):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp = OUT_HTML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html.replace('"assets/', '"../assets/').replace("'assets/", "'../assets/"))
    os.replace(tmp, OUT_HTML)


def build_all(force=True):
    """重新生成所有范围的看板（数据变了之后调用）"""
    with LOCK:
        HTML_CACHE.clear()
        KW_CACHE.clear()
        DIAG_CACHE.clear()
    html, snap = html_for_range("latest", force=force)
    if snap:
        write_static(html)
        with LOCK:
            STATE["last_build"] = time.time()
    return html


def refresh_data(verbose=True, force_creative=False):
    """
    force_creative=True：区间创意明细强制重拉（授权成功后调用）。
    正常轮询用 False —— 靠「账号集合指纹」判失效即可，不会白拉。
    授权请求可能落在「正在刷新」期间，故 force 意图记在 STATE 里由下一轮消费。
    """
    if DEMO_MODE:
        build_all(force=True)
        return True
    with LOCK:
        if STATE["refreshing"]:
            return False
        force_creative = bool(force_creative or STATE.get("force_creative"))
        STATE["force_creative"] = False
        STATE["refreshing"] = True
    # 演示模式：账户库中没有任何真实 token 时跳过拉数，
    # 避免无授权拉取返回空数据覆盖本地演示快照。
    try:
        from core import tokenhub as _th_demo
        accts_demo = _th_demo.load_accounts()
        has_real = any(
            (v or {}).get("access_token", "").startswith("demo_") is False
            for v in accts_demo.values())
        if not has_real:
            if verbose:
                print(f"[{datetime.now():%H:%M:%S}] 演示模式：无有效授权，跳过拉数，使用本地演示数据", flush=True)
            with LOCK:
                STATE["last_refresh"] = time.time()
                STATE["refreshing"] = False
            return False
    except Exception:
        pass
    t0 = time.time()
    try:
        end = datetime.now() - timedelta(days=1)
        start = end - timedelta(days=max(0, DAYS - 1))
        label = end.strftime("%Y-%m-%d")
        s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] 开始拉取 {s} ~ {e} …", flush=True)
        snapshot, _ = collector.collect(label, s, e, verbose=False)
        # 多时间维度的数据底座：日粒度账户数据，拉一次覆盖近一年，之后任意区间本地聚合
        try:
            spans = [range_bounds(k) for k, _ in RANGES]
            ds = min(x[0] for x in spans).isoformat()
            de = max(x[1] for x in spans).isoformat()
            collector.collect_account_daily(ds, de, verbose=False, refresh_first=False)
            with LOCK:
                HTML_CACHE.clear()          # 区间视图必须重建
        except Exception as ex:
            print(f"[{datetime.now():%H:%M:%S}] 日粒度缓存失败: {ex}", flush=True)
        # 区间创意明细：让区间视图也能看到素材与封面。
        # 数据期没变时 collect_range_creative 会直接复用缓存，所以轮询成本很低。
        for rk in CREATIVE_RANGES:
            try:
                rs, re_ = range_bounds(rk)
                collector.collect_range_creative(
                    rk, rs.isoformat(), re_.isoformat(), verbose=False,
                    force=force_creative)
                with LOCK:
                    HTML_CACHE.pop(rk, None)
            except Exception as ex:
                print(f"[{datetime.now():%H:%M:%S}] 区间创意明细[{rk}]失败: {ex}", flush=True)
        # 笔记标题 / 首图：让素材榜显示真实笔记名而不是投放内部代号。
        # 失败（典型是应用级限流 410029）时保留旧数据并按冷却退避，不反复打接口。
        try:
            from core import notemeta
            notemeta.fetch(days=30, verbose=True)
        except Exception as ex:
            print(f"[{datetime.now():%H:%M:%S}] 笔记维度失败: {ex}", flush=True)
        # 关键词（搜索词）日库：本地按「户 × 天 × 词」累积，任意区间本地聚合。
        # 首跑本地日库为空时自动回填 KW_BOOTSTRAP_DAYS 天，之后只做增量。
        # ⚠ 绝不能用 force_creative 清空关键词日库：它是「合并累积库」，
        # force=True 会丢全部历史（曾致 90 天回填被授权后清成 7 天）。新主体
        # 靠 account_signature 检测进入 jobs，增量拉取自然包含，无需 force。
        try:
            have = bool((kwdata.load_daily() or {}).get("advertisers"))
            back = max(1, KW_BOOTSTRAP_DAYS if not have else KW_PAGE_DAYS)
            k_end = end.strftime("%Y-%m-%d")
            k_start = (end - timedelta(days=back - 1)).strftime("%Y-%m-%d")
            kwdata.collect_keyword_daily(k_start, k_end, verbose=True,
                                         refresh_first=False, force=False)
            with LOCK:
                KW_CACHE.clear()
        except Exception as ex:
            print(f"[{datetime.now():%H:%M:%S}] 关键词日库失败: {ex}", flush=True)
        # 诊断档位预采：让打开诊断页就有数据，不必手动点采集。
        # 放在 build_all() 之前 —— 这样本轮就能直接渲染出诊断页，不必等第二次刷新。
        # 失败绝不影响主流程（诊断是附属视图，主看板必须照常出）。
        #
        # 分两级（KB_DIAG_AUTOREFRESH）：
        #   1 或 full → 把 8 档里「还没有的」全补齐（首次会采 8 档、共约 2~3 分钟；日常只补昨日 1 档）
        #   latest    → 只预采「昨日」单档（最省，适合只关心昨日的场景）
        #   0/off     → 完全不预采
        # 因为判据是「文件是否存在」，同一数据日重复刷新 100+ 次也只在第一轮采一次。
        if DIAG_AUTOREFRESH:
            mode = DIAG_AUTOREFRESH_MODE
            try:
                # ⚠ 变量名必须与上面的 ds/de（已被复用为「账户日库起止字符串」）、
                #   本段的 s0/e0 区分开 —— 否则会把 date 覆盖成 str，strftime 直接炸。
                ymd = end.strftime("%Y-%m-%d")
                if mode == "latest":
                    pend = [("latest", "昨日", ymd, ymd)]
                else:
                    # diag_batch_plan 直接返回 (key, label, "YYYY-MM-DD", "YYYY-MM-DD", exists)
                    pend = [(k, lab, ps, pe)
                            for k, lab, ps, pe, _ex in diag_batch_plan(only_missing=True)]
                if not pend:
                    if verbose:
                        print(f"[{datetime.now():%H:%M:%S}] 诊断档位已齐备，跳过预采", flush=True)
                else:
                    print(f"[{datetime.now():%H:%M:%S}] 诊断预采 {len(pend)} 档："
                          + "、".join(x[1] for x in pend) + " …", flush=True)
                    t_diag = time.time()
                    done_lab = []
                    for k, lab, ps, pe in pend:
                        try:
                            payload, dstats = diagapi.collect_plans(
                                ps, pe, verbose=False, use_live_filter=DIAG_LIVE_FILTER)
                            if payload.get("plans"):
                                diagapi.save_plans(payload)
                                try:
                                    with open(os.path.join(diagapi.DIAG_DIR, "latest.txt"),
                                              "w", encoding="utf-8") as f:
                                        f.write("plans_%s_%s.json" % (ps, pe))
                                except Exception:
                                    pass
                                with LOCK:
                                    DIAG_CACHE.clear()
                                done_lab.append("%s(%d)" % (lab, len(payload["plans"])))
                                print(f"[{datetime.now():%H:%M:%S}]   ✓ {lab} {ps}~{pe} · "
                                      f"{len(payload['plans'])} 计划 · "
                                      f"查 {dstats.get('queried')}/{dstats.get('advertisers')} 户",
                                      flush=True)
                            else:
                                print(f"[{datetime.now():%H:%M:%S}]   - {lab} 未取到计划，跳过",
                                      flush=True)
                        except Exception as ex1:
                            print(f"[{datetime.now():%H:%M:%S}]   ✗ {lab} 预采失败: {ex1}",
                                  flush=True)
                    print(f"[{datetime.now():%H:%M:%S}] 诊断预采完成 · {len(done_lab)} 档 · "
                          f"{round(time.time() - t_diag, 1)}s", flush=True)
            except Exception as ex:
                print(f"[{datetime.now():%H:%M:%S}] 诊断预采失败（不影响主看板）: {ex}", flush=True)
        build_all()
        with LOCK:
            STATE["last_refresh"] = time.time()
            STATE["last_error"] = None
            STATE["build_sec"] = round(time.time() - t0, 1)
        if verbose:
            su = snapshot.get("summary", {})
            print(f"[{datetime.now():%H:%M:%S}] 刷新完成 ¥{su.get('total_fee',0):,.0f} · "
                  f"私信{su.get('total_msg',0)} · 留资{su.get('total_msg_leads',0)} · "
                  f"{STATE['build_sec']}s", flush=True)
        return True
    except Exception as ex:
        with LOCK:
            STATE["last_error"] = f"{type(ex).__name__}: {ex}"
        print(f"[{datetime.now():%H:%M:%S}] 刷新失败: {ex}", flush=True)
        traceback.print_exc()
        return False
    finally:
        with LOCK:
            STATE["refreshing"] = False
        # 刷新期间被挡下的刷新请求，在这里补跑一次
        if NEED_REFRESH.is_set():
            NEED_REFRESH.clear()
            threading.Thread(target=refresh_data, daemon=True).start()


def request_refresh(force_creative=False):
    """
    请求一次刷新。
    若当前正在刷新则只记个标记 —— 否则「授权成功触发的刷新」会被
    正在跑的定时刷新直接挡掉，用户要白等一整个轮询周期。
    force_creative=True 时区间创意明细强制重拉（授权成功后要让新主体立刻可见）。
    """
    with LOCK:
        busy = STATE["refreshing"]
        if force_creative:
            STATE["force_creative"] = True
    if busy:
        NEED_REFRESH.set()
        print(f"[{datetime.now():%H:%M:%S}] 正在刷新中，本次请求已排队", flush=True)
        return False
    threading.Thread(target=refresh_data, daemon=True).start()
    return True


def refresher_loop():
    # KB_NO_REFRESH=1：只服务本地快照、不自动拉数（调试/验证用，避免与另一实例并发拉数）
    if DEMO_MODE or os.environ.get("KB_NO_REFRESH") == "1":
        print("  ⚠ KB_NO_REFRESH=1：已禁用自动拉数，仅服务本地快照")
        return
    while True:
        try:
            refresh_data()
        except Exception:
            traceback.print_exc()
        time.sleep(max(60, REFRESH_MIN * 60))


# ---------------- 待授权 ----------------

def pending_payload():
    rows = tokenhub.pending_list()
    accounts = tokenhub.load_accounts()
    out = []
    for r in rows:
        rec = accounts.get(r["key"], {})
        advs = rec.get("advertisers") or []
        out.append({
            "key": r["key"],
            "status": r["status"],
            "statusText": {"pending": "未授权", "placeholder": "占位符污染",
                           "expired": "已过期"}.get(r["status"], r["status"]),
            "user_id": r.get("user_id", ""),
            "advertisers": len(advs),
            "sample": [a.get("advertiser_name", "") for a in advs[:3]],
        })
    ok_rows = []
    for k, rec in accounts.items():
        st = tokenhub.classify(rec)
        if st in ("valid", "expiring", "refreshable"):
            ok_rows.append({"key": k, "status": st,
                            "remaining": round(tokenhub.remaining_hours(rec), 1),
                            "advertisers": len(rec.get("advertisers") or [])})
    return {
        "pending": out,
        "healthy": ok_rows,
        "total": len(accounts),
        "auth_url": authflow.auth_url(),
    }


# ---------------- HTTP ----------------

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "JuguangBoard/2.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        p = urlparse(self.path)
        return p.path.rstrip("/") or "/", parse_qs(p.query)

    def do_GET(self):
        try:
            self._route_get()
        except Exception as ex:
            traceback.print_exc()
            try:
                self._json({"ok": False, "msg": f"服务端异常: {type(ex).__name__}: {ex}"}, 500)
            except Exception:
                pass

    def _serve_asset(self, sub):
        """托管 assets/ 目录下的静态文件（演示封面等），防路径穿越。"""
        import mimetypes
        sub = sub.lstrip("/")
        safe = os.path.normpath(sub)
        if safe.startswith("..") or os.path.isabs(safe):
            self.send_error(403); return
        fp = os.path.join(BASE_DIR, "assets", safe)
        if not os.path.isfile(fp):
            self.send_error(404); return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        try:
            data = open(fp, "rb").read()
        except Exception:
            self.send_error(500); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _route_get(self):
        path, q = self._query()

        if path.startswith("/assets/"):
            self._serve_asset(path[len("/assets/"):])
            return

        if path == "/":
            rng = (q.get("range") or [ranges.DEFAULT_KEY])[0]
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            html, _ = html_for_range(rng, start=start, end=end)
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/dashboard":
            rng = (q.get("range") or [ranges.DEFAULT_KEY])[0]
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            _, snap = html_for_range(rng, start=start, end=end)
            self._json(snap or {})
            return

        # ---- 关键词页（支持 city / adv / owner 三个筛选，筛选＝重新聚合） ----
        if path in ("/keyword", "/api/keyword"):
            rng = (q.get("range") or [ranges.DEFAULT_KEY])[0]
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            filt = {"city": (q.get("city") or [""])[0],
                    "adv": (q.get("adv") or [""])[0],
                    "owner": (q.get("owner") or [""])[0]}
            html, snap = html_for_keyword(rng, start=start, end=end, filt=filt)
            if path == "/keyword":
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._json(snap or {})
            return

        # ---- 投放诊断页（计划级；支持区间切换与采集引导） ----
        if path in ("/diag", "/api/diag"):
            rng = (q.get("range") or [ranges.DEFAULT_KEY])[0]
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            html, meta = html_for_diag(rng, start=start, end=end)
            if path == "/diag":
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._json(meta)
            return

        # 采集进度（引导页轮询）
        if path == "/api/diag/status":
            with LOCK:
                st = dict(DIAG_STATE)
            self._json(st)
            return

        # 触发后台采集该区间（同一时刻只允许一个任务）
        if path == "/api/diag/collect":
            rng = (q.get("range") or [ranges.DEFAULT_KEY])[0]
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            s0, e0, label = diag_bounds(rng, start, end)
            if not s0:
                self._json({"ok": False, "msg": "区间无法识别：%s" % rng}, 400)
                return
            ok = diag_collect_async(s0, e0, label)
            if ok:
                self._json({"ok": True, "msg": "已开始采集 %s ~ %s（约 1~2 分钟）" % (s0, e0),
                            "range": {"start": s0, "end": e0}})
            else:
                self._json({"ok": False, "running": True,
                            "msg": "已有采集任务在进行，请稍候"}, 409)
            return

        # 批量采集：列出 8 档里「还没有的」档位（前端按钮上显示待补数量）
        if path == "/api/diag/batch/plan":
            plan = diag_batch_plan(only_missing=True)
            allp = diag_batch_plan(only_missing=False)
            self._json({
                "missing": [{"key": k, "label": lab, "start": s, "end": e}
                             for k, lab, s, e, _ in plan],
                "all": [{"key": k, "label": lab, "start": s, "end": e,
                          "exists": ex} for k, lab, s, e, ex in allp],
                "n_missing": len(plan), "n_total": len(allp),
                "running": DIAG_BATCH["running"] or DIAG_STATE["running"],
            })
            return

        # 批量采集：把缺的档位依次补好（force=1 则全部重采）
        if path == "/api/diag/batch/collect":
            force = (q.get("force") or ["0"])[0] not in ("0", "", "false", "no")
            keys = (q.get("keys") or [None])[0]
            keys = [x for x in keys.split(",") if x] if keys else None
            with LOCK:
                busy = DIAG_STATE["running"]
            if busy:
                self._json({"ok": False, "running": True,
                            "msg": "已有采集任务在进行，请稍候"}, 409)
                return
            plan = diag_collect_batch_async(keys=keys, force=force)
            if plan is None:
                self._json({"ok": False, "running": True,
                            "msg": "已有采集任务在进行，请稍候"}, 409)
            elif not plan:
                self._json({"ok": True, "msg": "8 个档位都已备好，无需采集",
                            "n": 0, "labels": []})
            else:
                self._json({"ok": True,
                            "msg": "已开始批量采集 %d 个档位" % len(plan),
                            "n": len(plan),
                            "labels": [x[1] for x in plan],
                            "ranges": [{"key": x[0], "label": x[1],
                                         "start": x[2], "end": x[3]} for x in plan]})
            return

        # 批量采集进度
        if path == "/api/diag/batch/status":
            with LOCK:
                b = dict(DIAG_BATCH)
                b["ok"] = list(DIAG_BATCH["ok"])
                b["fail"] = list(DIAG_BATCH["fail"])
                b["elapsed"] = round(DIAG_BATCH["elapsed"], 1)
            self._json(b)
            return

        # 取消批量采集（当前这一档采完后停止）
        if path == "/api/diag/batch/cancel":
            with LOCK:
                DIAG_BATCH["cancel"] = True
            self._json({"ok": True, "msg": "将在当前档位采完后停止"})
            return

        if path == "/api/status":
            with LOCK:
                st = {
                    "startedAt": datetime.fromtimestamp(STATE["started_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "lastBuild": (datetime.fromtimestamp(STATE["last_build"]).strftime("%Y-%m-%d %H:%M:%S")
                                  if STATE["last_build"] else None),
                    "lastRefresh": (datetime.fromtimestamp(STATE["last_refresh"]).strftime("%Y-%m-%d %H:%M:%S")
                                    if STATE["last_refresh"] else None),
                    "refreshing": STATE["refreshing"],
                    "lastError": STATE["last_error"],
                    "lastAuth": STATE["last_auth"],
                    "period": STATE["period"],
                    "refreshIntervalMin": REFRESH_MIN,
                }
            self._json(st)
            return

        if path == "/api/pending":
            self._json(pending_payload())
            return

        # 账号授权总览：把内部主体键翻译成门店账号名
        if path == "/api/accounts":
            from core import accounts_meta
            self._json(accounts_meta.build())
            return

        # 短链：方便群发。点一下直接进小红书授权页
        if path in ("/go", "/auth"):
            if DEMO_MODE:
                self._json(blocked(), 403)
                return
            self.send_response(302)
            self.send_header("Location", authflow.auth_url())
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path == "/api/refresh":
            with LOCK:
                busy = STATE["refreshing"]
            if busy:
                NEED_REFRESH.set()
                self._json({"ok": True, "queued": True,
                            "msg": "已有刷新在进行，本次已排队"})
                return
            ok = refresh_data()
            self._json({"ok": ok, "error": STATE.get("last_error")}, 200 if ok else 500)
            return

        self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as ex:
            traceback.print_exc()
            try:
                self._json({"ok": False, "msg": f"服务端异常: {type(ex).__name__}: {ex}"}, 500)
            except Exception:
                pass

    def _route_post(self):
        path, _ = self._query()
        # 在读取请求正文前拒绝，授权码不解析、不记录、不入库。
        if DEMO_MODE and path in ("/api/auth", "/api/balances/refresh"):
            self.close_connection = True
            self._json(blocked(), 403)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            body = json.loads(raw or "{}")
        except Exception:
            body = {}

        if path == "/api/auth":
            code = body.get("code") or body.get("auth_code") or ""
            label = (body.get("label") or "").strip()
            if not code:
                self._json({"ok": False, "msg": "请粘贴回调地址或 auth_code"}, 400)
                return
            try:
                res = authflow.authorize(code, label)
            except Exception as ex:
                traceback.print_exc()
                res = {"ok": False, "msg": f"处理异常：{type(ex).__name__}: {ex}"}
            with LOCK:
                STATE["last_auth"] = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                      "ok": res.get("ok"), "corp": res.get("corp")}
            print(f"[{datetime.now():%H:%M:%S}] 授权提交: "
                  f"{'成功' if res.get('ok') else '失败'} {res.get('corp','')} {res.get('msg','')}",
                  flush=True)
            if res.get("ok"):
                # 授权成功 → 后台拉数把新主体纳入（排队机制保证不被定时刷新挡掉）。
                # force_creative：区间创意明细强制重拉，否则新主体会因为
                # 「数据期没变」命中缓存复用，在 近3日/近7日/本月/上月 里看不到。
                request_refresh(force_creative=True)
            self._json(res, 200 if res.get("ok") else 400)
            return

        if path == "/api/balances/refresh":
            # 拉取各投放户实时余额（约 165 户，逐户查询，预计 20~40s）。
            # 写 data/balances.json 后清缓存，让看板「可用余额」列立即生效。
            try:
                bs = balances.collect_all(verbose=True)
                balances.save_balances(bs)
                with LOCK:
                    HTML_CACHE.clear()
                    KW_CACHE.clear()
                summ = balances.summary(bs)
                self._json({"ok": True, "summary": summ,
                            "msg": f"已刷新 {summ['n']} 户，其中 {summ['n_with_balance']} 户有余额"})
            except Exception as ex:
                traceback.print_exc()
                self._json({"ok": False, "msg": f"余额刷新失败: {type(ex).__name__}: {ex}"}, 500)
            return

        if path == "/api/refresh":
            with LOCK:
                busy = STATE["refreshing"]
            if busy:
                NEED_REFRESH.set()
                self._json({"ok": True, "queued": True,
                            "msg": "已有刷新在进行，本次已排队"})
                return
            ok = refresh_data()
            self._json({"ok": ok, "error": STATE.get("last_error")}, 200 if ok else 500)
            return

        self._json({"ok": False, "msg": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def main():
    print("=" * 64)
    print("  小红书聚光投放看板 · 局域网服务")
    print("=" * 64)
    if daily_days():
        try:
            build_all()
            print(f"  已用现有快照出图：{STATE['period']}")
        except Exception:
            traceback.print_exc()
    else:
        print("  ⚠ 本地还没有快照，等待首次拉数")

    threading.Thread(target=refresher_loop, daemon=True).start()

    ip = local_ip()
    print(f"\n  本机访问    http://127.0.0.1:{PORT}/")
    print(f"  局域网访问  http://{ip}:{PORT}/")
    print(f"  近 7 日视图 http://{ip}:{PORT}/?range=7d")
    print(f"  状态接口    http://{ip}:{PORT}/api/status")
    print(f"\n  刷新间隔 {REFRESH_MIN} 分钟 · 拉取最近 {DAYS} 天 · Ctrl+C 停止\n")

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
