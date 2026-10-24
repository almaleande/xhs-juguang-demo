# -*- coding: utf-8 -*-
"""诊断看板渲染层
================
把「采集 payload」渲染成完整 HTML：

    payload → diagrules.diagnose → project（前端紧凑结构） → 看板模板_诊断.html

服务端（看板服务.py 的 /diag 路由）与独立生成器（tools/生成诊断看板.py）共用，
保证两处输出完全一致。
"""
import json
import os
import sys

try:
    from . import cfgapi, diagrules, kpi_spec, layout, ranges
    from .paths import BASE_DIR, REPORTS_DIR
except ImportError:  # 直接脚本运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import cfgapi, diagrules, layout, ranges
    from core.paths import BASE_DIR, REPORTS_DIR

TEMPLATE_FILE = os.path.join(BASE_DIR, "看板模板_诊断.html")
OUT_NAME = "聚光_投放诊断看板.html"

# 区间档位：与投放总览/关键词**共用同一份定义**（core/ranges.py）
# 早期诊断页自写了一套（近7/近14/近30/本月/上月），与总览的 8 档对不上，
# 切页时像换了产品 —— 现统一，勿在本文件另起炉灶。
RANGES = ranges.RANGES
RANGE_NAME = ranges.RANGE_NAME
DEFAULT_RANGE = ranges.DEFAULT_KEY


def _j(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def range_html(ranges, current, custom_range=None, base="/diag"):
    """区间按钮 HTML（.rtab 系列）。base = 页面路径（服务端 /diag；静态页传 None 则不渲染）"""
    if not ranges:
        return ""
    sep = "&" if "?" in (base or "") else "?"
    parts = []
    for k, n in ranges:
        active = " active" if k == current else ""
        href = "%s%srange=%s" % (base, sep, k)
        if k == "custom" and custom_range:
            s, e = custom_range
            href = "%s%srange=custom&start=%s&end=%s" % (base, sep, s, e)
        parts.append('<a class="rtab%s" href="%s">%s</a>' % (active, href, _esc(n)))
    return "".join(parts)


def project(p):
    """把 enriched plan 投影成前端需要的紧凑结构。"""
    cfg = p.get("cfg") or {}
    u = p.get("units") or {}
    return {
        "id": p.get("campaign_id"),
        "name": p.get("campaign_name"),
        "city": p.get("city"),
        "subject": p.get("subject"),
        "adv": p.get("advertiser_name"),
        "adv_id": p.get("advertiser_id"),
        "health": p["health"],
        "health_label": p["health_label"],
        "eff_tag": p["eff_tag"],
        "eff_cls": p["eff_cls"],
        "fee": p["d_fee"], "imp": p["d_imp"], "clk": p["d_clk"],
        "msg": p["d_msg"], "ml": p["d_ml"],
        "ctr": p["d_ctr"], "acp": p["d_acp"], "ml_cost": p["d_ml_cost"],
        "msg_rate": p["d_msg_rate"], "ml_rate": p["d_ml_rate"],
        "primary": p["primary_title"], "action": p["primary_action"],
        "opt": p["_opt_label"], "place": p["_place_label"], "dmode": p["_dmode"],
        "mkt": p["_mkt_label"],
        "bid": p.get("_bid"),
        "budget": cfgapi.budget_desc(cfg),
        "time": cfgapi.time_period_desc(cfg),
        "note_count": u.get("note_count", 0) or 0,
        "unit_count": u.get("count", 0) or 0,
        "age": u.get("age", "不限"), "gender": u.get("gender", "不限"),
        "city_target": u.get("city", "不限"),
        "tags": [{"level": t["level"], "title": t["title"],
                  "detail": t["detail"], "action": t["action"]} for t in p["tags"]],
    }


def diagnose_payload(payload, drop_zero_fee=True):
    """payload → (rows_for_frontend, summary)。collect → 诊断 → 投影 一条龙。"""
    enriched, summary = diagrules.diagnose(payload, drop_zero_fee=drop_zero_fee)
    rows = [project(p) for p in enriched]
    return rows, summary


def build_diag_html(payload, ranges=None, current_range="d7", start=None,
                    end=None, base="/diag", rows=None, summary=None, secnav=True):
    """渲染诊断页 HTML。

    secnav=True（服务端）：吸顶区间条 + 隐藏正文 h2；
    secnav=False（静态导出）：隐藏吸顶条（区间按钮指向服务端，file:// 下无意义）+ 显示正文 h2。
    """
    try:
        with open(TEMPLATE_FILE, encoding="utf-8") as f:
            tpl = f.read()
    except Exception as ex:
        print("⚠ 未找到诊断模板: %s" % ex, flush=True)
        return ("<h3 style='font-family:sans-serif;padding:40px'>"
                "诊断页模板缺失：看板模板_诊断.html</h3>")

    if rows is None or summary is None:
        rows, summary = diagnose_payload(payload)

    custom = (start, end) if (current_range == "custom" and start and end) else None
    repl = {
        "__TOP_STABLE_CSS__": layout.TOP_STABLE_CSS,
        "__DIAG_DATA__": _j(rows),
        "__TOP_KPI_JS__": kpi_spec.TOP_KPI_JS,
        "__DIAG_SUMMARY__": _j(summary),
        "__RANGES_HTML__": range_html(ranges, current_range, custom, base=base or "/diag"),
        "__RANGE_START__": start or "",
        "__RANGE_END__": end or "",
        "__DIAG_BASE__": base or "/diag",
        "__CUSTOM_ACTIVE__": (" active" if (current_range == "custom"
                                            or str(current_range).startswith("custom|")) else ""),
        "__SECNAV_STYLE__": "" if secnav else ' style="display:none"',
        "__H2_STYLE__": ' style="display:none"' if secnav else "",
    }
    missing = [k for k in repl if k not in tpl]
    if missing:
        print("⚠ 诊断模板缺失占位符: %s" % missing, flush=True)
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl
