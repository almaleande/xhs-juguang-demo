# -*- coding: utf-8 -*-
"""
投放诊断规则引擎
================
读取 diagapi.collect_plans 产出的 plans_*.json，对每条计划在「配置 + 效果」上
做全维度诊断：打分（健康度）、贴标签（问题+动作）、横向对标（出价）。
输出 (enriched_plans, summary)，供 reports/聚光_投放诊断看板.html 使用。

健康度 health ∈ {danger 危, warn 警, ok 良, great 优, sleep 休眠, paused 暂停}
标签 level  ∈ {danger, warn, tip, info}（danger/warn 计入健康度，tip/info 仅提示）

阈值（元）取自婚纱摄影客资行业经验值，可按要求微调。
"""
from collections import Counter

from . import cfgapi

# ---------------------- 阈值（元） ----------------------
TH = {
    "ml_good": 150.0, "ml_warn": 300.0, "ml_bad": 500.0,   # 留资成本
    "msg_warn": 120.0, "msg_tip": 80.0,                    # 私信开口成本
    "acp_warn": 15.0, "acp_tip": 10.0,                     # 点击成本
    "ctr_bad": 0.3, "ctr_warn": 0.5,                       # CTR %
    "msg_rate_bad": 1.0, "msg_rate_tip": 2.0,              # 点击→开口 %
    "ml_rate_bad": 3.0, "ml_rate_tip": 5.0,                # 开口→留资 %
    "bid_low_ratio": 0.8,                                  # 低于同城同目标均值的比例 → 判偏低
    "min_peer": 3,                                         # 出价横向对标最少样本数
    "waste_fee": 300.0,                                    # 高耗无转化阈值（元）
}

# 最小样本量护栏：样本太小，比率类诊断无统计意义，不判 danger/warn
VOL = {"imp_min": 500, "clk_min": 20, "msg_min": 5}

HEALTH_ORDER = ["danger", "warn", "ok", "great", "sleep", "paused"]
HEALTH_LABEL = {"danger": "危", "warn": "警", "ok": "良", "great": "优",
                "sleep": "休眠", "paused": "暂停"}
_LEVEL_RANK = {"danger": 0, "warn": 1, "tip": 2, "info": 3}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _t(rule, level, title, detail, action):
    return {"rule": rule, "level": level, "title": title,
            "detail": detail, "action": action}


def _plan_bid_yuan(plan):
    """计划层目标成本（分→元）；否则取单元出价均值（元）。拿不到返回 None。"""
    cfg = plan.get("cfg") or {}
    b = cfgapi.bid_yuan_plan(cfg)
    if b and b > 0:
        return b
    u = plan.get("units") or {}
    lo, hi = u.get("bid_min"), u.get("bid_max")
    if lo and hi:
        return (lo + hi) / 2.0
    if lo:
        return lo
    return None


def _parse_budget_yuan(desc):
    import re
    m = re.search(r"([\d.]+)元", desc or "")
    return float(m.group(1)) if m else None


def _eff_tag(fee, ml, ml_cost, msg, msg_cost):
    """复用素材获客榜语义的效率标签。"""
    if ml > 0 and ml_cost is not None:
        if ml_cost <= TH["ml_good"]:
            return ("优秀", "tag-green")
        if ml_cost <= TH["ml_warn"]:
            return ("合格", "tag-gold")
        return ("成本偏高", "tag-red")
    if msg > 0 and msg_cost is not None:
        if msg_cost <= TH["msg_tip"]:
            return ("有开口", "tag-green")
        return ("开口偏贵", "tag-gold")
    if fee >= TH["waste_fee"]:
        return ("高耗无转化", "tag-red")
    return ("—", "tag-gray")


def _diagnose_one(p, days):
    cfg = p.get("cfg") or {}
    eff = p.get("eff") or {}
    dv = p.get("derived") or {}
    units = p.get("units") or {}

    fee = _f(eff.get("fee"))
    imp = _f(eff.get("impression"))
    clk = _f(eff.get("click"))
    msg = _f(eff.get("message_user")) or _f(eff.get("message_consult"))
    ml = _f(eff.get("msg_leads_num"))
    ctr = dv.get("ctr")
    acp = dv.get("acp")
    msg_cost = dv.get("msg_cost")
    ml_cost = dv.get("ml_cost")
    msg_rate = dv.get("msg_rate")
    ml_rate = dv.get("ml_rate")

    enable = int(cfg.get("campaign_enable") or 1)
    opt = cfgapi.label("optimize_target", cfg.get("optimize_target"))
    place = cfgapi.label("placement", cfg.get("placement"))
    dmode = cfgapi.label("delivery_mode", cfg.get("delivery_mode"))
    bid = _plan_bid_yuan(p)

    tags = []

    # —— 已暂停 / 无消耗（结构诊断）——
    if enable == 0:
        tags.append(_t("pause", "info", "计划已暂停",
                       "campaign_enable=0，当前不在投放",
                       "如仍需获客，重新启用并确认预算/定向/单元审核"))
        return _pack(p, "paused", tags, fee, imp, clk, msg, ml,
                     ctr, acp, msg_cost, ml_cost, msg_rate, ml_rate,
                     cfg, units, bid, opt, place, dmode)
    if fee <= 0:
        tags.append(_t("sleep", "info", "近区间无消耗",
                       "该计划在区间内无花费，可能未起量/预算未释放/新建未审",
                       "检查单元审核、出价是否过低、是否撞预算上限或定向过窄"))
        return _pack(p, "sleep", tags, fee, imp, clk, msg, ml,
                     ctr, acp, msg_cost, ml_cost, msg_rate, ml_rate,
                     cfg, units, bid, opt, place, dmode)

    # —— 成本类 ——
    if ml > 0 and ml_cost is not None:
        if ml_cost >= TH["ml_bad"]:
            tags.append(_t("cost_ml", "danger", "留资成本失控",
                           "留资成本 ¥%.0f，远高于行业优值 ¥150" % ml_cost,
                           "收紧定向/换高意向素材/强化落地页留资引导，或下调目标成本"))
        elif ml_cost >= TH["ml_warn"]:
            tags.append(_t("cost_ml", "warn", "留资成本偏高",
                           "留资成本 ¥%.0f（行业参考 ¥150~300）" % ml_cost,
                           "优化素材与定向，压低留资成本"))
    if msg > 0 and msg_cost is not None:
        if msg_cost >= TH["msg_warn"]:
            tags.append(_t("cost_msg", "warn", "私信开口偏贵",
                           "开口成本 ¥%.0f" % msg_cost,
                           "优化首图与标题，提升点击→私信意愿"))
        elif msg_cost >= TH["msg_tip"]:
            tags.append(_t("cost_msg", "tip", "开口成本略高",
                           "开口成本 ¥%.0f" % msg_cost, "观察，可优化私信话术"))
    if clk >= VOL["clk_min"] and acp is not None:
        if acp >= TH["acp_warn"]:
            tags.append(_t("cost_acp", "warn", "点击成本过高",
                           "点击成本 ¥%.1f" % acp, "换首图/优化定向，压低 CPC"))
        elif acp >= TH["acp_tip"]:
            tags.append(_t("cost_acp", "tip", "点击成本略高",
                           "点击成本 ¥%.1f" % acp, "可观察走势"))

    # —— 漏斗类（样本不足不判 danger/warn）——
    if imp >= VOL["imp_min"] and ctr is not None:
        if ctr < TH["ctr_bad"]:
            tags.append(_t("ctr", "danger", "CTR 过低",
                           "CTR %.2f%%，素材/定向吸引力弱" % ctr,
                           "重做首图与标题，或缩小定向人群"))
        elif ctr < TH["ctr_warn"]:
            tags.append(_t("ctr", "warn", "CTR 偏低",
                           "CTR %.2f%%" % ctr, "优化素材点击率"))
    if clk >= VOL["clk_min"] and msg_rate is not None:
        if msg_rate < TH["msg_rate_bad"]:
            tags.append(_t("funnel_open", "danger", "点击→开口转化弱",
                           "开口率 %.2f%%，落地页/私信引导不足" % msg_rate,
                           "强化私信自动回复与留资钩子"))
        elif msg_rate < TH["msg_rate_tip"]:
            tags.append(_t("funnel_open", "warn", "开口率偏低",
                           "开口率 %.2f%%" % msg_rate, "优化私信话术与组件"))
    if msg >= VOL["msg_min"] and ml_rate is not None:
        if ml_rate < TH["ml_rate_bad"]:
            tags.append(_t("funnel_ml", "danger", "开口→留资转化弱",
                           "留资率 %.1f%%，开口未有效转留资" % ml_rate,
                           "补齐留资组件、强化优惠钩子"))
        elif ml_rate < TH["ml_rate_tip"]:
            tags.append(_t("funnel_ml", "warn", "留资率偏低",
                           "留资率 %.1f%%" % ml_rate, "优化留资路径"))

    # 样本量过小：比率类诊断不可靠，仅作提示
    if fee > 0 and clk < VOL["clk_min"]:
        tags.append(_t("lowvol", "info", "样本量偏小",
                       "点击 %d 次，比率指标仅供参考" % int(clk),
                       "放量后复查 CTR / 开口率 / 留资率"))

    # —— 预算撞线（实测本批全为不限，规则保留）——
    budget = cfgapi.budget_desc(cfg)
    if budget != "不限":
        lim = _parse_budget_yuan(budget)
        if lim and days:
            daily = fee / days
            if daily >= lim * 0.9:
                tags.append(_t("budget", "warn", "预算撞线风险",
                               "日均消耗 ¥%.0f 接近日预算 ¥%.0f" % (daily, lim),
                               "上调日预算或错峰投放，避免提前停投"))

    # —— 投放位置建议（客资计划叠加搜索）——
    if opt in ("私信开口量", "私信留资量", "有效留资量") and place == "信息流推广":
        tags.append(_t("placement", "tip", "建议叠加搜索位置",
                       "当前仅信息流，搜索位意向更强",
                       "新增搜索推广单元，承接主动搜索流量"))
    # —— 投放模式 ——
    if dmode == "手动投放":
        tags.append(_t("mode", "tip", "手动投放",
                       "手动出价，依赖人工调优",
                       "效果不稳可切自动投放(稳定成本)降低操盘成本"))
    # —— 时段 ——
    tpd = cfgapi.time_period_desc(cfg)
    if "全天" not in tpd and "未设置" not in tpd:
        tags.append(_t("time", "tip", "时段未全覆盖",
                       "投放时段：" + tpd, "评估是否放开全天，避免错过流量"))
    # —— 素材单薄（仅当已知 note_count>0 且 <2；note_count=0 多为配置未返回，不判）——
    nc = units.get("note_count", 0) or 0
    if 0 < nc < 2:
        tags.append(_t("note", "tip", "素材偏单薄",
                       "关联笔记仅 %d 篇" % nc, "补充 2~3 篇不同风格笔记做 AB"))

    # —— 健康度 ——
    has_danger = any(t["level"] == "danger" for t in tags)
    has_warn = any(t["level"] == "warn" for t in tags)
    if has_danger:
        health = "danger"
    elif has_warn:
        health = "warn"
    elif (ml_cost is not None and ml_cost < TH["ml_good"]
          and (msg_rate or 0) > 4 and (ctr or 0) > 1):
        health = "great"
    else:
        health = "ok"

    return _pack(p, health, tags, fee, imp, clk, msg, ml,
                 ctr, acp, msg_cost, ml_cost, msg_rate, ml_rate,
                 cfg, units, bid, opt, place, dmode)


def _pack(p, health, tags, fee, imp, clk, msg, ml, ctr, acp, msg_cost,
          ml_cost, msg_rate, ml_rate, cfg, units, bid, opt, place, dmode):
    et, ecls = _eff_tag(fee, ml, ml_cost, msg, msg_cost)
    tags_sorted = sorted(tags, key=lambda t: _LEVEL_RANK.get(t["level"], 9))
    primary = tags_sorted[0] if tags_sorted else None
    p2 = dict(p)
    p2["health"] = health
    p2["health_label"] = HEALTH_LABEL[health]
    p2["tags"] = tags_sorted
    p2["primary_title"] = primary["title"] if primary else ""
    p2["primary_action"] = primary["action"] if primary else ""
    p2["eff_tag"] = et
    p2["eff_cls"] = ecls
    p2["d_fee"] = fee
    p2["d_imp"] = imp
    p2["d_clk"] = clk
    p2["d_msg"] = msg
    p2["d_ml"] = ml
    p2["d_ctr"] = ctr
    p2["d_acp"] = acp
    p2["d_ml_cost"] = ml_cost
    p2["d_msg_rate"] = msg_rate
    p2["d_ml_rate"] = ml_rate
    p2["_bid"] = bid
    p2["_opt_label"] = opt
    p2["_place_label"] = place
    p2["_dmode"] = dmode
    p2["_mkt_label"] = cfgapi.label("marketing_target", cfg.get("marketing_target"))
    return p2


def _apply_bid_peer(enriched):
    """出价横向对标：同城 × 同优化目标，样本>=min_peer 时，
    低于组均值 bid_low_ratio 的计划判『出价偏低』。"""
    groups = {}
    for d in enriched:
        if d["health"] in ("sleep", "paused") or d["d_fee"] <= 0:
            continue
        if not d.get("_bid"):
            continue
        groups.setdefault((d["city"], d["_opt_label"]), []).append(d)
    for key, lst in groups.items():
        bids = [x["_bid"] for x in lst if x.get("_bid")]
        if len(bids) < TH["min_peer"]:
            continue
        mean = sum(bids) / len(bids)
        for x in lst:
            b = x.get("_bid")
            if b and b < mean * TH["bid_low_ratio"]:
                diff = (1 - b / mean) * 100
                x["tags"].append(_t("bid_low", "warn", "出价低于同城均值",
                                    "目标成本 ¥%.0f，低于同城「%s」均值 ¥%.0f 约 %.0f%%"
                                    % (b, key[1], mean, diff),
                                    "适当上调目标成本以抢量，或确认是否刻意控成本"))
                if x["health"] == "ok":
                    x["health"] = "warn"
                    x["health_label"] = HEALTH_LABEL["warn"]
                # 重新排序 tags & 主要问题
                x["tags"].sort(key=lambda t: _LEVEL_RANK.get(t["level"], 9))
                x["primary_title"] = x["tags"][0]["title"]
                x["primary_action"] = x["tags"][0]["action"]


def _summarize(enriched, payload, dropped_zero=0):
    health_cnt = Counter(d["health"] for d in enriched)
    rule_cnt = Counter()
    problem_rule_cnt = Counter()
    for d in enriched:
        for t in d["tags"]:
            rule_cnt[t["rule"]] += 1
            if t["level"] in ("danger", "warn"):
                problem_rule_cnt[t["rule"]] += 1
    active = [d for d in enriched if d["health"] not in ("sleep", "paused")]
    total_fee = sum(d["d_fee"] for d in enriched)
    ml_with = [d["d_ml_cost"] for d in active if d["d_ml_cost"] is not None]
    # 城市汇总
    city_map = {}
    for d in enriched:
        c = d["city"]
        e = city_map.setdefault(c, {"plans": 0, "active": 0, "fee": 0.0,
                                    "ml": 0.0, "danger": 0, "warn": 0,
                                    "ml_costs": []})
        e["plans"] += 1
        if d["health"] not in ("sleep", "paused"):
            e["active"] += 1
        e["fee"] += d["d_fee"]
        e["ml"] += d["d_ml"]
        if d["health"] == "danger":
            e["danger"] += 1
        if d["health"] == "warn":
            e["warn"] += 1
        if d["d_ml_cost"] is not None:
            e["ml_costs"].append(d["d_ml_cost"])
    city_rows = []
    for c, e in city_map.items():
        mc = e["ml_costs"]
        city_rows.append({
            "city": c, "plans": e["plans"], "active": e["active"],
            "fee": round(e["fee"], 1), "ml": round(e["ml"], 1),
            "danger": e["danger"], "warn": e["warn"],
            "ml_cost_avg": round(sum(mc) / len(mc), 1) if mc else None,
        })
    city_rows.sort(key=lambda x: -x["fee"])

    # 失败户按城市归并（名称含城市字样）→ 提示该城市消耗可能偏低
    fail_list = payload.get("fail", []) or []
    known_cities = list(city_map.keys())
    fail_by_city = {}
    for fl in fail_list:
        nm = str((fl.get("name") if isinstance(fl, dict) else fl) or "")
        for c in known_cities:
            if c in nm:
                fail_by_city[c] = fail_by_city.get(c, 0) + 1
                break

    stats = payload.get("stats", {})
    return {
        "range": payload.get("range", {}),
        "collected_at": payload.get("collected_at"),
        "total_plans": len(enriched),
        "active_plans": len(active),
        "sleep_plans": health_cnt.get("sleep", 0),
        "paused_plans": health_cnt.get("paused", 0),
        "dropped_zero": dropped_zero,
        "total_fee": round(total_fee, 1),
        "health_cnt": {k: health_cnt.get(k, 0) for k in HEALTH_ORDER},
        "rule_cnt": dict(rule_cnt.most_common()),
        "problem_rule_cnt": dict(problem_rule_cnt.most_common()),
        "ml_cost_avg_active": round(sum(ml_with) / len(ml_with), 1) if ml_with else None,
        "city_rows": city_rows,
        "coverage": {
            "subjects": stats.get("subjects"),
            "advertisers": stats.get("advertisers"),
            "ok": stats.get("ok"),
            "fail": stats.get("fail"),
            "plans": stats.get("plans"),
        },
        "fail_by_city": fail_by_city,
        "fail_list": payload.get("fail", [])[:50],
    }


def diagnose(payload, drop_zero_fee=False):
    """入口：payload(plans_*.json) → (enriched_plans, summary)

    drop_zero_fee=True 时，在采集侧剔除「区间 0 消耗」计划，
    看板只呈现有效投放计划（无消耗计划不参与健康度/汇总统计）。
    """
    plans = payload.get("plans", [])
    dropped_zero = 0
    if drop_zero_fee:
        kept = []
        for p in plans:
            fee = _f((p.get("eff") or {}).get("fee"))
            if fee > 0:
                kept.append(p)
            else:
                dropped_zero += 1
        plans = kept
    r = payload.get("range", {})
    try:
        from datetime import datetime
        d0 = datetime.strptime(r["start"], "%Y-%m-%d")
        d1 = datetime.strptime(r["end"], "%Y-%m-%d")
        days = max(1, (d1 - d0).days + 1)
    except Exception:
        days = 7
    enriched = [_diagnose_one(p, days) for p in plans]
    _apply_bid_peer(enriched)
    summary = _summarize(enriched, payload, dropped_zero=dropped_zero)
    return enriched, summary


# 规则中文名（看板图例用）
RULE_LABEL = {
    "cost_ml": "留资成本", "cost_msg": "开口成本", "cost_acp": "点击成本",
    "ctr": "CTR", "funnel_open": "点击→开口", "funnel_ml": "开口→留资",
    "budget": "预算撞线", "bid_low": "出价偏低", "placement": "投放位置",
    "mode": "投放模式", "time": "投放时段", "note": "素材单薄",
    "pause": "已暂停", "sleep": "无消耗", "lowvol": "样本偏小",
}
