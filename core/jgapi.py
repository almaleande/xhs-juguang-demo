#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聚光开放平台 API 封装
======================
统一处理：请求发送、错误码语义化、分页拉取、按创意聚合。
所有网络异常都转成结构化结果，绝不抛裸异常打断整轮任务。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

API_BASE = "https://adapi.xiaohongshu.com"
REPORT_PATH = "/api/open/jg/data/report/offline/creative"
TOKEN_PATH = "/api/open/oauth2/access_token"
REFRESH_PATH = "/api/open/oauth2/refresh_token"

# ---------- 显式不走代理 ----------
# 本机装有 Clash/mihomo，且**用户级环境变量** HTTP_PROXY/HTTPS_PROXY 长期指向本地端口
# （实测被指向已下线的 7897；Clash 实际已改到 7895）。裸 urllib.request.urlopen 会自动
# 读取这两个变量，一旦该端口挂掉，**所有** API 请求都会失败成 code=-1「网络异常」，
# 表现为「看板刷不出数据 / 全户失败 / 消耗归零」。
# 聚光是国内域名（adapi.xiaohongshu.com），直连即可，绝不该走代理 →
# 统一用一个「显式空代理」的 opener，彻底与系统代理环境解耦。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 错误码 → 语义 + 处置建议
ERROR_MAP = {
    410014: ("auth_code 错误/已被使用", "auth_code 一次性且时效极短，需立刻换取"),
    410016: ("refresh_token 错误或已过期", "续期通道关闭，必须重新授权"),
    410018: ("access_token 为空", "该主体尚未授权"),
    410019: ("access_token 错误", "token 无效，需重新授权或续期"),
    410022: ("无权限访问该广告主", "该投放户不在授权范围内"),
    400001: ("无该账号权限", "token 与广告主必须同主体，跨主体查询报此错"),
    410029: ("调用频率太快", "触发应用级限流，需退避重试"),
}

# ---------- 全局限流 + 重试 ----------
# 实测：同一接口 0.25s 间隔连打 8 次可过，但短时爆发会被 410029 拦。
# 这里做「全局最小间隔」节流，保证并发拉数时不会打爆应用配额。
MIN_INTERVAL = float(os.environ.get("JUGUANG_MIN_INTERVAL", "0.12"))
MAX_RETRY = int(os.environ.get("JUGUANG_MAX_RETRY", "3"))
RETRY_CODES = {410029, -1}          # 限流 + 网络异常，可安全重试

_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle():
    """全局节流：保证任意两次请求间隔不小于 MIN_INTERVAL"""
    with _throttle_lock:
        gap = time.time() - _last_call[0]
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        _last_call[0] = time.time()


# 需要累加的度量字段（接口返回蛇形命名，值为字符串）
SUM_FIELDS = [
    "fee", "impression", "click", "message_consult", "msg_leads_num",
    "like", "comment", "collect", "share", "follow", "interaction",
    "video_play_5s_cnt", "valid_leads", "reserve_pv", "seller_visit",
]

# 取最后一个非空值的维度字段
LAST_FIELDS = [
    "creativity_name", "creativity_image", "note_id", "unit_name",
    "campaign_name", "campaign_id", "unit_id", "note_user_id",
]


def describe(code):
    """错误码 → (语义, 建议)"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return (f"未知错误码 {code}", "")
    return ERROR_MAP.get(c, (f"未登记错误码 {code}", ""))


def post(path, body, token=None, timeout=30, retries=None):
    """
    发送 POST，永不抛异常；失败时返回 {'success': False, 'code', 'msg'}。
    遇到限流/网络异常会自动退避重试。
    """
    attempts = MAX_RETRY if retries is None else retries
    last = {"success": False, "code": -1, "msg": "未执行"}
    for i in range(max(1, attempts)):
        _throttle()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Access-Token"] = token
        req = urllib.request.Request(
            API_BASE + path,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                last = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                last = json.loads(e.read().decode("utf-8"))
            except Exception:
                last = {"success": False, "code": e.code, "msg": f"HTTP {e.code}"}
        except Exception as e:
            last = {"success": False, "code": -1, "msg": f"网络异常: {e}"}

        if last.get("success") or last.get("code") not in RETRY_CODES:
            return last
        if i < attempts - 1:
            time.sleep(0.8 * (2 ** i))       # 0.8s → 1.6s → 3.2s
    return last


# ---------- 授权相关 ----------

def exchange_auth_code(app_id, app_secret, auth_code):
    resp = post(TOKEN_PATH, {
        "app_id": app_id, "secret": app_secret, "auth_code": auth_code})
    if resp.get("success"):
        resp["data"]["created_at"] = int(time.time())
    return resp


def refresh_token(app_id, app_secret, refresh_token_value):
    resp = post(REFRESH_PATH, {
        "app_id": app_id, "secret": app_secret,
        "refresh_token": refresh_token_value})
    if resp.get("success"):
        d = resp["data"]
        d["created_at"] = int(time.time())
        # 有的返回不带 refresh_token，沿用旧的
        if not d.get("refresh_token"):
            d["refresh_token"] = refresh_token_value
    return resp


# ---------- 数据报表 ----------

def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _aggregate(rows):
    """把「同创意多天」的多行合并成一条"""
    agg = {}
    for row in rows:
        cid = str(row.get("creativity_id") or row.get("creativityId") or "")
        if not cid:
            cid = f"__noid_{len(agg)}"
        cur = agg.get(cid)
        if cur is None:
            cur = {"creativity_id": cid}
            for f in SUM_FIELDS:
                cur[f] = 0.0
            for f in LAST_FIELDS:
                cur[f] = ""
            cur["_days"] = set()
            agg[cid] = cur
        for f in SUM_FIELDS:
            cur[f] += _to_float(row.get(f))
        for f in LAST_FIELDS:
            if row.get(f):
                cur[f] = row[f]
        if row.get("time"):
            cur["_days"].add(row["time"])

    out = []
    for cur in agg.values():
        days = len(cur.pop("_days") or ()) or 1
        imp = cur.get("impression", 0.0)
        clk = cur.get("click", 0.0)
        inter = sum(cur.get(f, 0.0) for f in
                    ("like", "comment", "collect", "share", "follow"))
        fee = cur.get("fee", 0.0)
        msg = cur.get("message_consult", 0.0)
        ml = cur.get("msg_leads_num", 0.0)
        cur["interaction"] = inter
        cur["ctr"] = f"{clk / imp * 100:.2f}%" if imp > 0 else "0.00%"
        cur["interaction_rate"] = f"{inter / imp * 100:.2f}%" if imp > 0 else "0.00%"
        cur["cpm"] = round(fee / imp * 1000, 2) if imp > 0 else 0.0
        cur["acp"] = round(fee / clk, 2) if clk > 0 else 0.0
        cur["msg_cost"] = round(fee / msg, 2) if msg > 0 else 0.0
        cur["ml_cost"] = round(fee / ml, 2) if ml > 0 else 0.0
        cur["days"] = days
        out.append(cur)
    return out


def _split_date(s, e):
    """取日期区间中点；区间不足 2 天时返回 None"""
    try:
        d1 = date.fromisoformat(s)
        d2 = date.fromisoformat(e)
    except Exception:
        return None
    if (d2 - d1).days < 1:
        return None
    return (d1 + timedelta(days=(d2 - d1).days // 2)).isoformat()


def _next_day(s):
    try:
        return (date.fromisoformat(s) + timedelta(days=1)).isoformat()
    except Exception:
        return s


def _fetch_span(token, advertiser_id, s, e, page_size):
    """拉一个日期段的第一页 → (rows, total_count, meta)"""
    resp = post(REPORT_PATH, {
        "advertiser_id": advertiser_id,
        "start_date": s, "end_date": e,
        "page": 1, "page_size": page_size,
    }, token=token)
    if not resp.get("success"):
        return None, 0, {"ok": False, "code": resp.get("code"),
                         "msg": resp.get("msg", "")}
    d = resp.get("data") or {}
    return (d.get("data_list") or []), int(d.get("total_count") or 0), {"ok": True}


def fetch_creatives(token, advertiser_id, start_date, end_date,
                    page_size=1000, max_pages=60):
    """
    拉取单个广告主的创意报表并聚合。
    返回 (creatives, meta)；meta 里带状态，便于区分「真没投放」和「拉取失败」。

    ⚠ 2026-09-17 实测：聚光报表的**翻页是坏的** —— page>=2 会原样重复返回第 1 页的内容。
      同一户同一区间（total=1001）：page_size=500 时 3 页共 1500 行、唯一仅 500 行；
      page_size=1000 时 2 页共 2000 行、唯一 1000 行。照翻页累加会让消耗直接翻倍。

    所以这里**不使用翻页**，改为：当某段行数超过 page_size 时，把日期区间二分后分别重拉，
    直到每段都能一页装下。这样拿到的数据才完整且不重复。page_size 上限实测为 1000。
    """
    meta = {"advertiser_id": advertiser_id, "ok": False, "code": 0, "msg": "",
            "rows": 0, "pages": 0, "total_count": 0, "spans": 0, "truncated": 0}
    out_rows, seen = [], set()
    queue = [(start_date, end_date)]
    truncated = 0

    while queue:
        s, e = queue.pop(0)
        rows, total, m = _fetch_span(token, advertiser_id, s, e, page_size)
        if not m.get("ok"):
            meta["code"] = m.get("code")
            meta["msg"] = m.get("msg", "")
            meta["hint"] = describe(m.get("code"))[1]
            # 第一段就失败 → 整户失败；否则保留已拿到的
            return ([] if not out_rows else _aggregate(out_rows)), meta

        meta["spans"] += 1
        meta["total_count"] += total

        if total > page_size:
            mid = _split_date(s, e)
            if mid:
                queue.append((s, mid))
                queue.append((_next_day(mid), e))
                continue
            truncated += 1          # 单日也超一页，只能取前 page_size 行

        for r in rows:
            sig = (str(r.get("time") or ""), str(r.get("creativity_id") or ""),
                   str(r.get("unit_id") or ""))
            if sig in seen:
                continue
            seen.add(sig)
            out_rows.append(r)

    meta["rows"] = len(out_rows)
    meta["pages"] = meta["spans"]
    meta["truncated"] = truncated
    meta["ok"] = True
    meta["msg"] = "成功"
    return _aggregate(out_rows), meta


# ---------- 关键词（搜索词）报表 ----------

KEYWORD_PATH = "/api/open/jg/data/report/offline/keyword"

# 关键词维度单行有 104 个字段，大头是电商/直播类噪音，这里只留有用的。
KEYWORD_KEEP = [
    "keyword", "keyword_id", "time",
    "unit_id", "unit_name", "campaign_id", "campaign_name",
    "placement", "optimize_target", "bidding_strategy", "word_avg_location",
    "fee", "impression", "click",
    "message_consult", "msg_leads_num", "message_user", "initiative_message",
    "valid_leads", "leads",
    "like", "comment", "collect", "share", "follow",
    "search_invoke_button_click_cnt", "search_invoke_button_click_cost",
    "action_button_click", "add_wechat_count", "add_wechat_suc_count",
    "wechat_talk_count", "wechat_talk_cost", "screenshot", "pic_save",
]

# ⚠ 关键词维度的 page_size 上限是 500，不是创意维度的 1000。
#   传 1000 会直接报错「关键词层级page_num需要大于0，page_size需要大于0且不能大于50」
#   —— 报错文案里的「50」是误导，实测 500 可用、1000 不可用。
KEYWORD_PAGE_SIZE = 500


def _fetch_kw_span(token, advertiser_id, s, e, page_size):
    """拉一个日期段的第一页 → (rows, total_count, meta)"""
    resp = post(KEYWORD_PATH, {
        "advertiser_id": advertiser_id,
        "start_date": s, "end_date": e,
        "page": 1, "page_size": page_size,
    }, token=token)
    if not resp.get("success"):
        return None, 0, {"ok": False, "code": resp.get("code"),
                         "msg": resp.get("msg", "")}
    d = resp.get("data") or {}
    return (d.get("data_list") or []), int(d.get("total_count") or 0), {"ok": True}


def fetch_keywords(token, advertiser_id, start_date, end_date,
                   page_size=KEYWORD_PAGE_SIZE, max_spans=500):
    """
    拉单个广告主的关键词报表，返回**精简后的原始行**（不聚合，交给上层按
    「户 × 天 × 词」汇总 —— 因为上层需要区分日期来存日库）。

    ⚠ 与创意维度完全相同的两个坑：
      1. **翻页是坏的**：page>=2 原样重复返回第 1 页 → 不翻页，段行数超 page_size
         就把日期区间二分后分别重拉（复用 _split_date / _next_day）。
      2. **page_size 上限 500**（不是 1000）。
    去重键 (time, keyword, unit_id, placement)：同一词同一天可能被多个单元/版位命中。
    返回 (rows, meta)。
    """
    meta = {"advertiser_id": advertiser_id, "ok": False, "code": 0, "msg": "",
            "rows": 0, "spans": 0, "total_count": 0, "truncated": 0}
    out, seen = [], set()
    queue = [(start_date, end_date)]

    while queue:
        if meta["spans"] >= max_spans:
            meta["truncated"] += 1
            break
        s, e = queue.pop(0)
        rows, total, m = _fetch_kw_span(token, advertiser_id, s, e, page_size)
        if not m.get("ok"):
            meta["code"] = m.get("code")
            meta["msg"] = m.get("msg", "")
            meta["hint"] = describe(m.get("code"))[1]
            # 第一段就失败 → 整户失败；否则保留已拿到的部分
            if not out:
                return [], meta
            break

        meta["spans"] += 1
        meta["total_count"] += total

        if total > page_size:
            mid = _split_date(s, e)
            if mid:
                queue.append((s, mid))
                queue.append((_next_day(mid), e))
                continue
            meta["truncated"] += 1      # 单日也超一页，只能取前 page_size 行

        for r in rows:
            kw = str(r.get("keyword") or "").strip()
            if not kw:
                continue
            sig = (str(r.get("time") or "")[:10], kw,
                   str(r.get("unit_id") or ""), str(r.get("placement") or ""))
            if sig in seen:
                continue
            seen.add(sig)
            out.append({f: r.get(f) for f in KEYWORD_KEEP if f in r})

    meta["rows"] = len(out)
    meta["ok"] = True
    meta["msg"] = "成功"
    return out, meta


# ---------- 通用维度报表（计划 / 单元 / 笔记 / 账户） ----------
# 2026-09-23 新增：诊断看板需要「计划层级」的效果数据，与配置接口(campaign/list)
# 按 campaign_id 关联。与创意维度同样的两个坑：翻页坏 + page_size 上限 1000。

DIM_KEYS = {
    "campaign": "campaign_id",
    "unit": "unit_id",
    "note": "note_id",
    "account": "advertiser_id",
    "creative": "creativity_id",
}

# 诊断看板只留需要的字段，避免携带 170 列（含大量电商/直播噪音）
DIAG_KEEP = [
    "time", "campaign_id", "campaign_name", "unit_id", "unit_name", "note_id",
    "placement", "marketing_target", "optimize_target", "bidding_strategy",
    "promotion_target", "deep_optimize_objective",
    "fee", "impression", "click", "acp", "ctr", "cpm",
    "message", "message_user", "message_consult", "msg_leads_num",
    "initiative_message", "msg_leads_cost", "message_consult_cpl",
    "search_first_show_imp_rate", "search_first_show_click_rate",
    "search_first_show_fee", "search_first_show_fee_rate",
    "like", "comment", "collect", "share", "follow", "interaction",
]


def _fetch_dim_span(token, advertiser_id, dim, s, e, page_size):
    """拉某维度某日期段的第一页 → (rows, total_count, meta)"""
    resp = post(f"/api/open/jg/data/report/offline/{dim}", {
        "advertiser_id": advertiser_id,
        "start_date": s, "end_date": e,
        "page": 1, "page_size": page_size,
    }, token=token)
    if not resp.get("success"):
        return None, 0, {"ok": False, "code": resp.get("code"),
                         "msg": resp.get("msg", "")}
    d = resp.get("data") or {}
    return (d.get("data_list") or []), int(d.get("total_count") or 0), {"ok": True}


def fetch_dim_rows(token, advertiser_id, dim, start_date, end_date,
                   page_size=1000, max_spans=200, keep=None):
    """
    通用维度报表拉取（**不聚合**，返回原始行，交给上层按需汇总）。
    dim ∈ {campaign, unit, note, account, creative}。
    去重键 (time, <维度key>) —— 同维度同天只保留一行。
    返回 (rows, meta)；meta 内含 ok/code/msg/rows/spans/truncated。
    """
    key = DIM_KEYS.get(dim)
    if not key:
        return [], {"ok": False, "code": -2, "msg": f"未知维度 {dim}", "rows": 0}
    keep = keep or DIAG_KEEP
    meta = {"advertiser_id": advertiser_id, "dim": dim, "ok": False, "code": 0,
            "msg": "", "rows": 0, "spans": 0, "total_count": 0, "truncated": 0}
    out, seen = [], set()
    queue = [(start_date, end_date)]

    while queue:
        if meta["spans"] >= max_spans:
            meta["truncated"] += 1
            break
        s, e = queue.pop(0)
        rows, total, m = _fetch_dim_span(token, advertiser_id, dim, s, e, page_size)
        if not m.get("ok"):
            meta["code"] = m.get("code")
            meta["msg"] = m.get("msg", "")
            meta["hint"] = describe(m.get("code"))[1]
            if not out:
                return [], meta
            break
        meta["spans"] += 1
        meta["total_count"] += total

        if total > page_size:
            mid = _split_date(s, e)
            if mid:
                queue.append((s, mid))
                queue.append((_next_day(mid), e))
                continue
            meta["truncated"] += 1

        for r in rows:
            sig = (str(r.get("time") or "")[:10], str(r.get(key) or ""))
            if sig in seen:
                continue
            seen.add(sig)
            out.append({f: r.get(f) for f in keep if f in r})

    meta["rows"] = len(out)
    meta["ok"] = True
    meta["msg"] = "成功"
    return out, meta

