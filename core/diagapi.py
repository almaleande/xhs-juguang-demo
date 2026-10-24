# -*- coding: utf-8 -*-
"""
计划级诊断数据层
================
把「配置接口」与「效果报表接口」在**计划层级**合并成一条完整记录，
供投放诊断看板使用。

链路：
    配置  cfgapi.fetch_campaigns  (campaign/list, 58 字段)
          cfgapi.fetch_units      (unit/list, 34 字段 + target_config 定向)
    效果  jgapi.fetch_dim_rows("campaign", ...)  (offline/campaign, 日期二分+去重)
    合并键 campaign_id

输出（落盘 data/diag/plans_<start>_<end>.json）：
    {
      "range": {"start":..., "end":...},
      "collected_at": ts,
      "stats": {"subjects":n,"advertisers":n,"ok":n,"fail":n,"plans":n},
      "plans": [ {subject, city, advertiser_id, campaign_id, campaign_name,
                  cfg:{...}, units:{...}, eff:{...}, derived:{...}} , ...]
    }
"""
import gzip
import json
import os
import threading
import time
from datetime import datetime

try:
    from . import cfgapi, jgapi, tokenhub
    from .collector import infer_city, owner_label, live_advertiser_ids
    from .paths import DATA_DIR
except ImportError:  # 直接脚本运行
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import cfgapi, jgapi, tokenhub
    from core.collector import infer_city, owner_label, live_advertiser_ids
    from core.paths import DATA_DIR

DIAG_DIR = os.path.join(DATA_DIR, "diag")

# 计划层要保留的配置字段
CFG_KEEP = [
    "campaign_id", "campaign_name", "campaign_enable", "not_available_status",
    "marketing_target", "placement", "delivery_mode", "optimize_target",
    "optimize_objective", "bidding_strategy", "deep_optimize_objective",
    "promotion_target", "constraint_type", "constraint_value",
    "budget_state", "limit_day_budget", "campaign_day_budget",
    "schedule_limit_budget", "start_time", "expire_time", "time_period",
    "time_period_type", "put_period_type", "search_bid_ratio", "explore_state",
    "spu_category", "conversion_channels", "pacing_mode", "platform",
    "campaign_create_time", "campaign_update_time", "campaign_filter_state",
]

# 效果层要累加的字段
EFF_SUM = [
    "fee", "impression", "click", "message", "message_user", "message_consult",
    "msg_leads_num", "initiative_message", "interaction",
    "like", "comment", "collect", "share", "follow",
]

# 效果层要保留的成本类字段（由整体派生更可靠，这里仅留原始成本做对照）
EFF_COST = ["msg_leads_cost", "message_consult_cpl"]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def iter_targets(accounts=None):
    """产出 (subject_key, subject_label, token, advertiser_id, advertiser_name)。
    只产出 token 有效的主体。"""
    accounts = accounts if accounts is not None else tokenhub.load_accounts()
    for key, rec in (accounts or {}).items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        if tokenhub.classify(rec) not in ("valid", "expiring", "refreshable"):
            continue
        label = owner_label(key, rec)
        for adv in rec.get("advertisers") or []:
            aid = adv.get("advertiser_id")
            if aid is None:
                continue
            yield key, label, tok, aid, adv.get("advertiser_name", "") or ""


def _agg_effect(rows):
    """把「同计划多天」的多行合并成一条效果记录。"""
    agg = {}
    for r in rows:
        cid = str(r.get("campaign_id") or "")
        if not cid:
            continue
        cur = agg.setdefault(cid, {f: 0.0 for f in EFF_SUM})
        for f in EFF_SUM:
            cur[f] += _f(r.get(f))
        for f in EFF_COST:
            v = r.get(f)
            if v not in (None, "", "0"):
                cur[f] = _f(v)
    return agg


def _unit_summary(units):
    """把单元的 target_config 定向等信息汇总成看板可读的摘要。"""
    if not units:
        return {"count": 0}
    ages, genders, cities, ttypes, note_ids, bids, lpt = set(), set(), [], set(), set(), [], set()
    for u in units:
        # 配置缓存里的单元是预解析精简版（_t / _nids）；实时采集的是原始体 → 两种都支持
        t = u.get("_t")
        if t is None:
            t = cfgapi.parse_target_config(u)
        if t.get("target_age") and t["target_age"] != "all":
            ages.add(t["target_age"])
        if t.get("target_gender") and t["target_gender"] != "all":
            genders.add(t["target_gender"])
        if t.get("target_city"):
            cities.append(str(t["target_city"]))
        ttypes.add(str(u.get("target_type")))
        lpt.add(str(u.get("landing_page_type")))
        b = cfgapi.bid_yuan(u)
        if b:
            bids.append(b)
        nids = u.get("_nids")
        if nids is None:
            nids = []
            sni = u.get("spu_note_info")
            if isinstance(sni, list):
                for item in sni:
                    for nid in (item or {}).get("note_ids") or []:
                        nids.append(nid)
        for nid in nids:
            note_ids.add(nid)
    # 年龄组合并展示
    age_all = "；".join(sorted(ages)) if ages else "不限"
    city_uniq = sorted(set(cities))
    return {
        "count": len(units),
        "target_type": sorted(ttypes),
        "age": age_all,
        "gender": ("不限" if not genders else "/".join(sorted(genders))),
        "city": ("不限" if not city_uniq else "/".join(city_uniq[:3]) + ("等" if len(city_uniq) > 3 else "")),
        "landing_page_type": sorted(lpt),
        "bid_min": min(bids) if bids else None,
        "bid_max": max(bids) if bids else None,
        "note_count": len(note_ids),
    }


def _derived(eff):
    """由累计效果派生各类成本与转化率。"""
    fee, imp, clk = eff["fee"], eff["impression"], eff["click"]
    msg = eff["message_user"] or eff["message_consult"]
    ml = eff["msg_leads_num"]
    return {
        "ctr": (clk / imp * 100) if imp else None,
        "acp": (fee / clk) if clk else None,
        "cpm": (fee / imp * 1000) if imp else None,
        "msg_cost": (fee / msg) if msg else None,        # 私信开口成本
        "ml_cost": (fee / ml) if ml else None,           # 留资成本
        "msg_rate": (msg / clk * 100) if clk else None,  # 点击→开口
        "ml_rate": (ml / msg * 100) if msg else None,    # 开口→留资
        "ml_rate_clk": (ml / clk * 100) if clk else None,
    }


# ---------------- 配置层缓存 ----------------
# campaign/list 与 unit/list 返回的是计划的「当前设置」（预算/出价/定向/优化目标/投放位置/模式），
# 与报表区间**无关**。所以同一份配置可以在不同区间反复复用，只有效果层才需要按区间重拉。
# 缓存文件按天命名（data/diag/cfg_<date>.json），跨天自动失效。

# 配置缓存里「单元」只保留这些字段（够 _unit_summary 用即可）。
# ⚠️ 曾直接存原始单元体 → 单元里 keyword_with_bids（关键词出价清单）单条 118KB，
# 100 户合计 113MB。裁剪 + 预解析 + gzip 后约 1MB。
UNIT_KEEP = ["campaign_id", "id", "target_type", "landing_page_type",
             "event_bid", "constraint_value"]


def _slim_unit(u):
    """单元 → 精简版：只留摘要所需字段，定向/笔记预解析成 _t / _nids。

    注意：对**已经精简过**的单元再调用会丢 _t（原体已无 target_config），
    故精简版带 _slim 标记，save 时不再二次裁剪。
    """
    t = cfgapi.parse_target_config(u) or {}
    nids = []
    sni = u.get("spu_note_info")
    if isinstance(sni, list):
        for item in sni:
            for nid in (item or {}).get("note_ids") or []:
                nids.append(nid)
    out = {f: u.get(f) for f in UNIT_KEEP if f in u}
    out["_t"] = {"target_age": t.get("target_age"),
                 "target_gender": t.get("target_gender"),
                 "target_city": t.get("target_city")}
    out["_nids"] = nids
    return out


def _slim_entry(camps, units):
    return {"_slim": True,
            "camps": [{f: c.get(f) for f in CFG_KEEP if f in c} for c in camps],
            "units": [_slim_unit(u) for u in units]}


def cfg_cache_file(day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    return os.path.join(DIAG_DIR, "cfg_%s.json.gz" % day)


def load_cfg_cache(max_age_hours=20):
    """返回 {advertiser_id(str): {"camps": [...], "units": [...]}}；文件缺失/过期返回 {}。"""
    f = cfg_cache_file()
    legacy = f[:-3] if f.endswith(".gz") else f       # 兼容早期未压缩的 cfg_<date>.json
    if os.path.exists(f):
        src, opener = f, (lambda: gzip.open(f, "rt", encoding="utf-8"))
    elif os.path.exists(legacy):
        src, opener = legacy, (lambda: open(legacy, encoding="utf-8"))
    else:
        return {}
    try:
        if time.time() - os.path.getmtime(src) > max_age_hours * 3600:
            return {}
        with opener() as fh:
            obj = json.load(fh) or {}
        return obj.get("by_adv") or {}
    except Exception:
        return {}


def save_cfg_cache(by_adv):
    """合并写入当天缓存（多线程/多次采集共用一个文件，故先读再合并）。"""
    if not by_adv:
        return None
    os.makedirs(DIAG_DIR, exist_ok=True)
    f = cfg_cache_file()
    old = {}
    if os.path.exists(f):
        try:
            with open(f, encoding="utf-8") as fh:
                old = (json.load(fh) or {}).get("by_adv") or {}
        except Exception:
            old = {}
    for k, v in list((by_adv or {}).items()):
        # 精简版直接用；原始体才裁剪（二次裁剪会丢 _t）
        old[k] = v if v.get("_slim") else _slim_entry(v.get("camps") or [], v.get("units") or [])
    tmp = f + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"saved_at": int(time.time()), "n": len(old), "by_adv": old},
                  fh, ensure_ascii=False)
    os.replace(tmp, f)
    return f


def _is_demo_mode():
    """演示模式：账号库全部为 demo_ 占位授权（无真实 token）。"""
    try:
        accts = tokenhub.load_accounts()
        vals = [v or {} for v in accts.values()]
        if not vals:
            return False
        return all(str((v.get("access_token") or "")).startswith("demo_") for v in vals)
    except Exception:
        return False


def fake_collect_plans(start_date, end_date):
    """演示模式：用本地虚构账户日数据生成「计划级」诊断数据。

    结构与真实 collect_plans 输出一致（plans_<start>_<end>.json 格式），
    数值完全虚构：按区间聚合每户消耗后拆分为若干计划，并混入
    高耗无转化 / 暂停 / 低 CTR 等样本，让诊断看板展示完整的规则标签。
    """
    import random
    rnd = random.Random(20261024)
    empty = {"range": {"start": start_date, "end": end_date},
             "collected_at": int(time.time()), "advertisers": [], "plans": [],
             "stats": {"subjects": 0, "advertisers": 0, "ok": 0, "fail": 0,
                       "plans": 0, "elapsed": 0.0, "workers": 0, "cfg_cache_hit": 0,
                       "cfg_cache_new": 0, "queried": 0, "live_filtered": 0},
             "fail": []}
    try:
        acc_daily = json.load(open(os.path.join(DATA_DIR, "account_daily.json"),
                                   encoding="utf-8"))
    except Exception:
        return empty, empty["stats"]
    days_advs = acc_daily.get("advertisers", {})
    plans, n_ok = [], 0
    for aid, it in days_advs.items():
        days = it.get("days") or {}
        in_range = {d: v for d, v in days.items() if start_date <= d <= end_date}
        if not in_range:
            continue
        agg = {"fee": 0.0, "impression": 0.0, "click": 0.0, "msg": 0, "ml": 0}
        for v in in_range.values():
            agg["fee"] += float(v.get("fee") or 0)
            agg["impression"] += float(v.get("impression") or 0)
            agg["click"] += float(v.get("click") or 0)
            agg["msg"] += int(v.get("message_consult") or 0)
            agg["ml"] += int(v.get("msg_leads_num") or 0)
        if agg["fee"] < 0.5:
            continue
        n_ok += 1
        city = it.get("city") or "其他"
        name = it.get("name") or "夏天婚纱摄影"
        key = it.get("owner_key") or "夏天婚纱摄影总部"
        n_camps = rnd.randint(2, 5)
        fee_each = agg["fee"] / n_camps
        for _j in range(n_camps):
            fee = fee_each * rnd.uniform(0.35, 1.8)
            if fee <= 0.5:
                continue
            acp = rnd.uniform(3.0, 11.0)
            clk = fee / acp
            ctr = rnd.uniform(0.02, 0.12)
            imp = clk / ctr
            if rnd.random() < 0.05:
                msg = ml = 0  # 高耗无转化样本
            else:
                msg_share = fee / agg["fee"] if agg["fee"] else 0.0
                msg = int(round(agg["msg"] * msg_share))
                ml = int(round(agg["ml"] * msg_share))
            like = int(rnd.uniform(0, imp * 0.01))
            comment = int(rnd.uniform(0, clk * 0.05))
            collect = int(rnd.uniform(0, clk * 0.08))
            share = int(rnd.uniform(0, clk * 0.02))
            follow = int(rnd.uniform(0, clk * 0.03))
            cid = str(rnd.randint(100000000, 999999999))
            cname = "%s-%s计划-%02d" % (city, rnd.choice(["获客", "转化", "品牌", "搜索", "种草"]),
                                       rnd.randint(1, 99))
            enable = 1 if rnd.random() > 0.06 else 0
            budget = rnd.randint(50, 3000) * 100
            eff = {"fee": round(fee, 2), "impression": round(imp, 1), "click": round(clk, 2),
                   "message": msg, "message_user": msg, "message_consult": msg,
                   "msg_leads_num": ml, "initiative_message": rnd.randint(0, 2),
                   "interaction": like + comment + collect, "like": like,
                   "comment": comment, "collect": collect, "share": share, "follow": follow}
            cfg = {"campaign_id": cid, "campaign_name": cname, "campaign_enable": enable,
                   "delivery_mode": rnd.choice([1, 0]),
                   "optimize_target": rnd.choice([13, 50, 13, 50, 5]),
                   "marketing_target": rnd.choice([9, 9, 9, 4]),
                   "placement": rnd.choice([4, 2, 1]),
                   "budget_state": 1, "limit_day_budget": budget,
                   "campaign_day_budget": budget, "start_time": 1790000000,
                   "expire_time": 1799999999, "search_bid_ratio": rnd.randint(50, 150),
                   "pacing_mode": rnd.choice(["均衡", "不限"])}
            bid_lo = round(rnd.uniform(3, 15), 1)
            units = {"count": rnd.randint(1, 6), "target_type": [2],
                     "age": rnd.choice(["不限", "18-30；31-40"]), "gender": "不限",
                     "city": rnd.choice(["不限", city]),
                     "landing_page_type": ["COMMERCE"], "bid_min": bid_lo,
                     "bid_max": round(bid_lo + rnd.uniform(0, 8), 1),
                     "note_count": rnd.randint(1, 8)}
            plans.append({"subject": name, "subject_key": key, "city": city,
                          "advertiser_id": aid, "advertiser_name": name,
                          "campaign_id": cid, "campaign_name": cname, "cfg": cfg,
                          "units": units, "eff": eff, "derived": _derived(eff)})
    payload = {"range": {"start": start_date, "end": end_date},
               "collected_at": int(time.time()),
               "advertisers": [{"advertiser_id": a, "name": it.get("name"),
                                "city": it.get("city"), "owner_key": it.get("owner_key")}
                               for a, it in days_advs.items() if it.get("days")],
               "plans": plans,
               "stats": {"subjects": n_ok, "advertisers": len(days_advs), "ok": n_ok,
                         "fail": 0, "plans": len(plans), "elapsed": 0.0, "workers": 0,
                         "cfg_cache_hit": 0, "cfg_cache_new": 0, "queried": 0,
                         "live_filtered": 0},
               "fail": []}
    return payload, payload["stats"]


def collect_plans(start_date, end_date, accounts=None, verbose=False,
                  limit_advertisers=None, sleep_between=0.0, workers=None,
                  progress_cb=None, use_cfg_cache=True, use_live_filter=False):
    """
    拉取全部已授权投放户的计划配置 + 效果，合并成 plan 记录。
    返回 (payload_dict, stats)。

    并发：默认 JG_DIAG_WORKERS（5）个线程同时跑不同投放户 —— 单户内部仍是串行，
    瓶颈在 HTTP 往返；jgapi.post 自带全局节流 + 限流(410029)退避，故并发安全。
    配置层缓存：当日复用 cfg_<date>.json，换区间时只重拉效果层（省一半请求）。
    use_live_filter=True 时先用账户日库预筛「区间内真有消耗的户」
    （实测与诊断实测零漏零多；165 户 → 28 户，省 83% 请求）；日库不可用则自动退化为全量。
    ⚠ 过滤只作用在「户」这一层 —— 计划层必须保留全量：
    实测有 313 条计划「上月有消耗、本月已停」，它们在上月区间里必须可见。
    progress_cb(done, total, label)：采集进度回调，供服务端展示。
    """
    if _is_demo_mode():
        if verbose:
            print("[%s] 演示模式：生成虚构计划级诊断数据 %s~%s"
                  % (datetime.now().strftime("%H:%M:%S"), start_date, end_date), flush=True)
        return fake_collect_plans(start_date, end_date)
    t0 = time.time()
    targets = list(iter_targets(accounts))
    n_targets_all = len(targets)          # 预筛前（=stats.advertisers，语义不变）
    n_filtered = 0
    if use_live_filter:
        live = live_advertiser_ids(start_date, end_date, accounts)
        if live is not None:
            before = len(targets)
            targets = [t for t in targets if str(t[3]) in live]
            n_filtered = before - len(targets)
            if verbose:
                print("  ⚡ 预筛：区间内有消耗的户 %d / %d（省 %d 户，%.0f%%）"
                      % (len(targets), before, n_filtered,
                         n_filtered / before * 100 if before else 0), flush=True)
        elif verbose:
            print("  ⚠ 账户日库不可用（缺失或未覆盖本区间）→ 跳过预筛，按全量拉取",
                  flush=True)
    if limit_advertisers is not None:
        targets = targets[:limit_advertisers]
    total = len(targets)
    if workers is None:
        workers = int(os.environ.get("JG_DIAG_WORKERS", "5"))
    workers = max(1, min(int(workers), 12))

    cfg_cache = load_cfg_cache() if use_cfg_cache else {}
    new_cfg = {}
    lock = threading.Lock()
    done_n = [0]

    def _one(t):
        key, label, tok, aid, aname = t
        city = infer_city(aname)
        if str(aid) in cfg_cache:
            cf = cfg_cache[str(aid)]
            camps = cf.get("camps") or []
            units = cf.get("units") or []
            hit = True
        else:
            camps = cfgapi.fetch_campaigns(aid, tok)
            units = cfgapi.fetch_units(aid, tok)
            hit = False
            if camps:
                with lock:
                    new_cfg[str(aid)] = {"camps": camps, "units": units}
        if sleep_between:
            time.sleep(sleep_between)
        rows, meta = jgapi.fetch_dim_rows(tok, aid, "campaign", start_date, end_date)
        return {"t": t, "city": city, "camps": camps, "units": units,
                "rows": rows, "meta": meta, "cfg_hit": hit}

    res = {}
    if targets:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, t): i for i, t in enumerate(targets)}
            for fu in as_completed(futs):
                idx = futs[fu]
                try:
                    res[idx] = fu.result()
                except Exception as ex_:
                    res[idx] = {"t": targets[idx],
                                "err": "%s: %s" % (type(ex_).__name__, ex_)}
                with lock:
                    done_n[0] += 1
                    n = done_n[0]
                if progress_cb:
                    try:
                        progress_cb(n, total, res[idx]["t"][1])
                    except Exception:
                        pass

    payload = {"range": {"start": start_date, "end": end_date},
               "collected_at": int(time.time()),
               "advertisers": [], "plans": []}
    seen_subj, fail = set(), []
    n_adv = n_ok = n_hit = 0

    for idx in range(total):
        r = res.get(idx)
        if not r:
            continue
        key, label, tok, aid, aname = r["t"]
        n_adv += 1
        seen_subj.add(key)
        if r.get("err"):
            fail.append({"advertiser_id": aid, "name": aname,
                         "cfg_err": r["err"], "eff_err": None})
            continue
        camps, units, rows = r["camps"], r["units"], r["rows"]
        meta = r.get("meta") or {}
        if r.get("cfg_hit"):
            n_hit += 1
        if not camps and not rows:
            fail.append({"advertiser_id": aid, "name": aname,
                         "cfg_err": getattr(cfgapi.fetch_campaigns, "last_error", None),
                         "eff_err": meta.get("msg")})
            if verbose:
                print("  ✗ %s / %s 拉取失败" % (label, aname[:20]), flush=True)
            continue
        n_ok += 1

        city = r["city"]
        # 城市兜底：账户名认不出，就用计划名认
        if city == "其他":
            for c in camps:
                cc = infer_city(c.get("campaign_name", ""))
                if cc != "其他":
                    city = cc
                    break

        eff_map = _agg_effect(rows)
        units_by_c = {}
        for u in units:
            units_by_c.setdefault(str(u.get("campaign_id")), []).append(u)

        uall = units
        for c in camps:
            cid = str(c.get("campaign_id") or "")
            if not cid:
                continue
            cfg = {f: c.get(f) for f in CFG_KEEP if f in c}
            my_units = units_by_c.get(cid, [])
            eff = eff_map.get(cid)
            if eff is None:
                eff = {f: 0.0 for f in EFF_SUM}
            payload["plans"].append({
                "subject": label, "subject_key": key, "city": city,
                "advertiser_id": aid, "advertiser_name": aname,
                "campaign_id": cid,
                "campaign_name": c.get("campaign_name") or "计划%s" % cid,
                "cfg": cfg,
                "units": _unit_summary(my_units),
                "eff": {k: round(v, 4) for k, v in eff.items()},
                "derived": _derived(eff),
            })
        if verbose:
            print("  ✓ %s / %s 计划 %d · 单元 %d · 有效 %d"
                  % (label, aname[:20], len(camps), len(uall), len(eff_map)), flush=True)

    if new_cfg:
        try:
            save_cfg_cache(new_cfg)
        except Exception as ex_:
            print("⚠ 写配置缓存失败: %s" % ex_, flush=True)

    payload["stats"] = {
        "subjects": len(seen_subj), "advertisers": n_targets_all, "ok": n_ok,
        "fail": len(fail), "plans": len(payload["plans"]),
        "elapsed": round(time.time() - t0, 1),
        "workers": workers, "cfg_cache_hit": n_hit, "cfg_cache_new": len(new_cfg),
        "queried": total, "live_filtered": n_filtered,
    }
    payload["fail"] = fail[:50]
    return payload, payload["stats"]


def save_plans(payload, path=None):
    os.makedirs(DIAG_DIR, exist_ok=True)
    if path is None:
        r = payload["range"]
        path = os.path.join(DIAG_DIR, f"plans_{r['start']}_{r['end']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def load_plans(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
