#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书聚光投放看板 · 生成器
=============================
参考抖音看板的信息骨架，重构成小红书逻辑：

  抖音（本地推）          →   小红书（聚光）
  视频/直播间             →   笔记（带封面）
  转化数（巨量口径）       →   私信咨询 / 留资
  婚客数（金燕子CRM）      →   客资（未来接金燕子）
  转化成本                →   私信成本 / 留资成本

核心模块：素材获客榜（封面 + 效果指标 + 效率标签）
零依赖：不引任何 JS 库，图表用内联 SVG / CSS 画，离线可看。

用法:
  python 看板生成.py                    # 用最新快照
  python 看板生成.py 2026-06-19         # 指定日期
  python 看板生成.py 2026-06-13 2026-06-19   # 近7日合并
"""
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import kpi_spec  # noqa: E402
from core import layout  # noqa: E402
from core.paths import BASE_DIR, DATA_DIR, REPORTS_DIR, SNAPSHOT_DIR  # noqa: E402

# 金色分三档：装饰 / 文字 / 按钮底。
# 原单一金色 #C9A24B 在白底只有 2.40:1，连大字号 3:1 门槛都不过（投屏会糊）。
GOLD = "#C8A96A"      # 装饰金：图表填充、logo、浅底徽标 —— 不承载文字
GOLD_T = "#A8842F"    # 文字金：白底 3.50:1，过 WCAG 大字号门槛
GOLD_BG = "#8A6D22"   # 按钮底金：白字 4.90:1，过正文门槛
INK = "#182337"
MUT = "#55637A"       # 5.99:1
DIMT = "#5F6E86"      # 5.17:1，替代原浅灰（3.02:1，全页 148 处不达标）
GREEN = "#2FA36B"
RED = "#E0564E"
BLUE = "#3E6FF0"

# 效率阈值（抽成配置，卡头图例与徽标判定共用一处，避免硬编码漂移）
EFF_ML_GOOD = 100     # 留资成本 ≤100 优秀
EFF_ML_OK = 250       # ≤250 合格，>250 成本偏高
EFF_MSG_OK = 120      # 私信成本 ≤120 有开口
WASTE_FEE = 50        # 消耗 ≥50 且零私信零留资 → 进预警


# ---------------- 数据层 ----------------

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def list_snapshots():
    files = sorted(glob.glob(os.path.join(SNAPSHOT_DIR, "*.json")))
    out = []
    for f in files:
        base = os.path.basename(f).replace(".json", "")
        if base == "最新":
            continue
        out.append(base)
    return out


def latest_daily_snapshot():
    """最新的一份日快照"""
    days = [s for s in list_snapshots() if len(s) == 10]
    return days[-1] if days else None


def load_day(label):
    path = os.path.join(SNAPSHOT_DIR, f"{label}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_days(labels):
    """合并多天快照，按 creativity_id 聚合"""
    merged = None
    for lb in labels:
        try:
            snap = load_day(lb)
        except FileNotFoundError:
            continue
        if merged is None:
            merged = {
                "date": lb, "generated_at": snap.get("generated_at", 0),
                "range": {"start": lb, "end": lb},
                "summary": {"total_fee": 0, "total_msg": 0, "total_msg_leads": 0,
                            "cities": {}, "advertiser_count": 0},
                "advertisers": {},
            }
        merged["range"]["end"] = lb
        s = merged["summary"]
        ss = snap.get("summary", {})
        s["total_fee"] += ss.get("total_fee", 0) or 0
        s["total_msg"] += ss.get("total_msg", 0) or 0
        s["total_msg_leads"] += ss.get("total_msg_leads", 0) or 0
        for city, cd in (ss.get("cities") or {}).items():
            c = s["cities"].setdefault(city, {"fee": 0, "msg": 0, "msg_leads": 0, "creative_count": 0})
            for k in ("fee", "msg", "msg_leads", "creative_count"):
                c[k] += cd.get(k, 0) or 0

        for aid, adv in (snap.get("advertisers") or {}).items():
            tgt = merged["advertisers"].setdefault(aid, {
                "city": adv.get("city", "其他"), "name": adv.get("name", ""), "creatives": []})
            idx = {c.get("creativity_id"): c for c in tgt["creatives"]}
            for c in adv.get("creatives", []):
                cid = c.get("creativity_id")
                if cid and cid in idx:
                    ec = idx[cid]
                    for k in ("fee", "impression", "click", "message_consult", "msg_leads_num",
                              "like", "comment", "collect", "share", "follow", "interaction"):
                        ec[k] = _num(ec.get(k)) + _num(c.get(k))
                    for k in ("creativity_image", "note_id", "creativity_name", "unit_name"):
                        if not ec.get(k) and c.get(k):
                            ec[k] = c[k]
                else:
                    nc = dict(c)
                    for k in ("fee", "impression", "click", "message_consult", "msg_leads_num",
                              "like", "comment", "collect", "share", "follow", "interaction"):
                        nc[k] = _num(c.get(k))
                    tgt["creatives"].append(nc)
                    if cid:
                        idx[cid] = nc
        merged["summary"]["advertiser_count"] = len(
            [a for a in merged["advertisers"].values() if a["creatives"]])
    return merged


def uid_label_map():
    """user_id → 小红书账号可读名（来自账号库的主体别名）"""
    m = {}
    try:
        from core import collector as _c
        from core import tokenhub as _t
        for k, rec in _t.load_accounts().items():
            uid = (rec.get("user_id") or "").strip()
            if uid:
                m[uid] = _c.owner_label(k, rec)
    except Exception:
        pass
    return m


# 投放账户名里的噪声词：清掉后大致就是该账号的名字
_ADV_NOISE = ("1-总店", "-总店", "总店", "YG-", "bmh-tcqc-", "TC-", "13-", "2-", "1-")


def clean_adv_name(n):
    s = n or ""
    # 长词优先替换，避免 "1-总店" 被 "1-" 先吃掉
    for w in sorted(_ADV_NOISE, key=len, reverse=True):
        s = s.replace(w, "")
    return s.strip(" -_·、()（）") or (n or "")


def build_owner_map(snapshot):
    """
    返回 (uid_map, inferred)：
      uid_map   已授权账号：note_user_id → 门店名
      inferred  未授权账号：note_user_id → 用「投它最多的投放账户名」推断出的名字

    未授权账号仍能从报表里看到消耗（因为投放账户授权了），
    但名字缺失会显示成 ID 乱码。这里靠账户名反推一个可读名，
    并在看板上标「推断」，等真授权后会被 uid_map 覆盖。
    """
    uid_map = uid_label_map()
    tally = {}
    for aid, adv in (snapshot.get("advertisers") or {}).items():
        nm = adv.get("name", "") or f"广告主{aid}"
        for c in adv.get("creatives", []):
            nu = (c.get("note_user_id") or "").strip()
            if not nu or nu in uid_map:
                continue
            d = tally.setdefault(nu, {})
            d[nm] = d.get(nm, 0.0) + _num(c.get("fee"))
    inferred = {}
    for nu, advs in tally.items():
        best = max(advs.items(), key=lambda x: x[1])[0]
        inferred[nu] = clean_adv_name(best)
    return uid_map, inferred


def flatten(snapshot, uid_map=None, inferred=None):
    """
    把嵌套结构摊平成素材列表，并带上三个分组维度：
      city   城市
      adv    投放账号（聚光广告主账户）
      owner  小红书账号（发笔记的账号，优先用 note_user_id 归因）
    """
    uid_map = uid_map or {}
    inferred = inferred or {}
    rows = []
    # 已知城市名集合：owner 归因 label 若只到城市粒度（如「青岛」），不算具体账号名
    city_set = {(a.get("city") or "").strip() for a in (snapshot.get("advertisers") or {}).values() if a.get("city")}
    for aid, adv in (snapshot.get("advertisers") or {}).items():
        adv_name = adv.get("name", "") or f"广告主{aid}"
        o_label = adv.get("owner_label") or ""
        o_uid = adv.get("owner_user_id") or ""
        for c in adv.get("creatives", []):
            imp = _num(c.get("impression"))
            clk = _num(c.get("click"))
            inter = _num(c.get("like")) + _num(c.get("comment")) + _num(c.get("collect")) \
                + _num(c.get("share")) + _num(c.get("follow"))
            fee = _num(c.get("fee"))
            msg = _num(c.get("message_consult"))
            ml = _num(c.get("msg_leads_num"))
            nuid = (c.get("note_user_id") or "").strip()
            city_fb = adv.get("city", "其他")
            # 小红书账号维度必须显示「具体账号名」：label 若只到城市名粒度（与 city 相同），
            # 降级为投放账户具体名，避免整组标签全是裸城市名
            def _spec_name(label):
                if not (label and label != city_fb and label not in city_set):
                    label = None
                o = o_label if (o_label and o_label != city_fb and o_label not in city_set) else None
                return label or o or clean_adv_name(adv_name) or city_fb
            if nuid and nuid in uid_map:
                owner = _spec_name(uid_map[nuid])
                owner_src = "known"
            elif nuid and nuid in inferred:
                owner = inferred[nuid]
                owner_src = "inferred"
            else:
                owner = _spec_name(o_label or clean_adv_name(adv_name))
                owner_src = "owner"
            rows.append({
                "aid": str(aid), "adv": adv_name, "city": adv.get("city", "其他"),
                "owner": owner, "uid": nuid or o_uid, "owner_src": owner_src,
                "cid": c.get("creativity_id", ""),
                "name": (c.get("creativity_name") or "").strip() or "(未命名素材)",
                "unit": (c.get("unit_name") or "").strip(),
                "img": c.get("creativity_image") or "",
                "note_id": c.get("note_id") or "",
                "fee": fee, "imp": imp, "clk": clk, "inter": inter,
                "msg": msg, "ml": ml,
                "ctr": (clk / imp * 100) if imp else 0.0,
                "ir": (inter / imp * 100) if imp else 0.0,
                "msg_cost": (fee / msg) if msg else None,
                "ml_cost": (fee / ml) if ml else None,
                "cpm": (fee / imp * 1000) if imp else 0.0,
                "pseudo": bool(c.get("is_pseudo")),
            })
    return rows


DIMS = [
    ("city", "城市", "城市"),
    ("adv", "投放账号", "投放账号（聚光广告主账户）"),
    ("owner", "小红书账号", "小红书账号（发布笔记的账号）"),
]

# 冗余词清洗：投放账号名常带公司全称，标签里显示太长
_NOISE_WORDS = (
    "青岛市市南区", "实业有限公司", "有限责任公司", "有限公司",
    "婚纱摄影", "摄影工作室", "文化传媒", "网络科技",
)


def short_name(n, limit=16):
    s = n or ""
    for w in _NOISE_WORDS:
        s = s.replace(w, "")
    s = s.strip(" -_·、")
    if not s:
        return (n or "")[:limit]
    return s[:limit]


def aggregate_by(rows, field):
    """按某维度聚合，返回 [(名称, {fee,msg,ml,n,imp,clk}), ...] 按消耗降序"""
    agg = {}
    for r in rows:
        k = r.get(field) or "其他"
        a = agg.setdefault(k, {"fee": 0.0, "msg": 0.0, "ml": 0.0, "n": 0,
                               "imp": 0.0, "clk": 0.0, "inter": 0.0})
        a["fee"] += r["fee"]
        a["msg"] += r["msg"]
        a["ml"] += r["ml"]
        a["n"] += 1
        a["imp"] += r["imp"]
        a["clk"] += r["clk"]
        a["inter"] += r["inter"]
    return sorted(agg.items(), key=lambda x: -x[1]["fee"])


# ---------------- 渲染层 ----------------

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def fmt_money(v, dec=0):
    return f"¥{v:,.{dec}f}"


def fmt_int(v):
    return f"{int(v):,}"


def bar_row(label, value, maxv, right="", color=GOLD, width_px=0):
    pct = (value / maxv * 100) if maxv else 0
    return (f'<div class="bar-row"><span class="bar-label" title="{esc(label)}">{esc(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%;'
            f'background:{color}"></span></span><span class="bar-val">{right}</span></div>')


def chip_label(n, limit=10):
    """维度色块的标签：**按头部截断**，返回 (文本, 是否推断名)。

    区分信息（城市 / 直营店 / 婚前影像 / 婚纱照）都在前半段，
    尾部「1-总店」「-总店」反而是多数账号共有的 —— 保留尾部、砍中段会让
    「夏天青岛直营店1-总店」和「青岛夏天婚前影像1-总店」长得一模一样。

    「（推断）」后缀占 4 个字太浪费，改成虚线下划线标记（见 `.dc-n.infer`）。
    """
    s = short_name(n, 999)
    infer = s.endswith("（推断）")
    if infer:
        s = s[:-4]
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s, infer


def dist_strip(items, dkey, top=6):
    """顶部常驻的「维度分布」紧凑色块条。

    items: [(name, {fee,msg,ml,n,...}), ...] 已按消耗降序。
    只出 Top N，其余合并成一个静态「其他 N 项」块 —— 保证色块金额之和 = 总消耗，
    不会出现"顶部和别处两个数字"。长尾明细仍在素材榜的下钻与「投放账户」卡片里。

    版式：名称独占一行（可用宽 ≈8 字），第二行「占比条 + 金额」——
    名称与金额挤在一行时，金额（nowrap）会把名称压到只剩 4~5 字，同城账号无法区分。
    """
    if not items:
        return '<span class="dc-empty">暂无数据</span>'
    total = sum(d["fee"] for _, d in items) or 1
    mx = (items[0][1]["fee"] or 1)
    head, rest = items[:top], items[top:]
    out = []

    def block(label, infer, fee, pct, tip):
        cls = "dc-n infer" if infer else "dc-n"
        return (f'<span class="{cls}" title="{esc(tip)}"><b>{esc(label)}</b></span>'
                f'<span class="dc-m"><span class="dc-b">'
                f'<i style="width:{fee / mx * 100:.1f}%"></i></span>'
                f'<em>¥{fee:,.0f} · {pct:.0f}%</em></span>')

    for n, d in head:
        pct = d["fee"] / total * 100
        lb, inf = chip_label(n)
        tip = (f"{n}｜消耗 ¥{d['fee']:,.0f}（占 {pct:.1f}%）· 私信 {int(d['msg'])}"
               f" · 留资 {int(d['ml'])} · {int(d['n'])} 条素材"
               + ("　（账号名由投放户推断，尚未授权）" if inf else "")
               + "　点击下钻到素材榜")
        out.append(
            f'<button class="dchip" data-dim="{dkey}" data-key="{esc(n)}" title="{esc(tip)}">'
            + block(lb, inf, d["fee"], pct, tip) + '</button>')
    if rest:
        rf = sum(d["fee"] for _, d in rest)
        rp = rf / total * 100
        rn = len(rest)
        names = "、".join(chip_label(n)[0] for n, _ in rest[:8])
        tip = (f"其余 {rn} 项：{names}{'…' if rn > 8 else ''}"
               f"｜合计 ¥{rf:,.0f}（占 {rp:.1f}%）")
        out.append(f'<span class="dchip dchip-rest" title="{esc(tip)}">'
                   + block(f"其他 {rn} 项", False, rf, rp, tip) + '</span>')
    return "".join(out)


def brief_num(v):
    """大数缩写：≥1 万用「万」，否则千分位。用于图表刻度，避免被裁切。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 10000:
        return f"{v / 10000:.1f}万"
    return f"{v:,.0f}"


def svg_line(points, labels, w=760, h=210, pad=(46, 18, 30, 12), color=GOLD, fill=True):
    """极简折线图（内联 SVG，零依赖）"""
    top, right, bottom, left = pad[0], pad[1], pad[2], pad[3]
    iw, ih = w - left - right, h - top - bottom
    if not points:
        return '<div class="empty">暂无数据</div>'
    mx = max(points) or 1
    n = len(points)
    step = iw / max(1, n - 1)
    coords = [(left + i * step, top + ih - (v / mx * ih)) for i, v in enumerate(points)]
    path = " ".join(("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords))
    area = path + f" L{coords[-1][0]:.1f} {top + ih:.1f} L{coords[0][0]:.1f} {top + ih:.1f} Z"
    grid = ""
    for i in range(5):
        gy = top + ih * i / 4
        val = mx * (1 - i / 4)
        grid += (f'<line x1="{left}" y1="{gy:.1f}" x2="{left + iw}" y2="{gy:.1f}" '
                 f'stroke="#EDF0F7" stroke-width="1"/>'
                 f'<text x="{left - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="{MUT}">{brief_num(val)}</text>')
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{color}"/>' for x, y in coords)
    xlab = ""
    show = labels if n <= 12 else [labels[i] if i % max(1, n // 8) == 0 else "" for i in range(n)]
    for i, (x, _) in enumerate(coords):
        if show[i]:
            xlab += (f'<text x="{x:.1f}" y="{top + ih + 16}" text-anchor="middle" '
                     f'font-size="10" fill="{MUT}">{esc(show[i])}</text>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'{grid}'
            + (f'<path d="{area}" fill="{color}" opacity="0.10"/>' if fill else "")
            + f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
              f'stroke-linejoin="round" stroke-linecap="round"/>{dots}{xlab}</svg>')


def svg_bar(items, w=760, h=210, pad=(46, 18, 34, 12), color=GOLD):
    """竖向柱状图（内联 SVG，零依赖）；items = [(label, value), ...]"""
    top, right, bottom, left = pad
    iw, ih = w - left - right, h - top - bottom
    if not items:
        return '<div class="empty">暂无数据</div>'
    mx = max(v for _, v in items) or 1
    n = len(items)
    slot = iw / n
    bw = min(slot * 0.58, 46)
    grid = ""
    for i in range(5):
        gy = top + ih * i / 4
        grid += (f'<line x1="{left}" y1="{gy:.1f}" x2="{left+iw}" y2="{gy:.1f}" '
                 f'stroke="#EDF0F7" stroke-width="1"/>'
                 f'<text x="{left-8}" y="{gy+4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="{MUT}">{mx*(1-i/4):,.0f}</text>')
    bars, labs = "", ""
    for i, (lb, v) in enumerate(items):
        bh = max(1.5, v / mx * ih)
        x = left + slot * i + (slot - bw) / 2
        y = top + ih - bh
        bars += (f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                 f'rx="3" fill="{color}" opacity="0.86"><title>{esc(lb)}: {v:,.0f}</title></rect>')
        labs += (f'<text x="{x+bw/2:.1f}" y="{top+ih+15}" text-anchor="middle" '
                 f'font-size="10" fill="{MUT}">{esc(lb[:5])}</text>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}">{grid}{bars}{labs}</svg>')


def eff_tag(row):
    """素材效率标签（阈值取自模块顶部配置，卡头图例用同一组数字）"""
    if row["ml"] > 0 and row["ml_cost"] is not None:
        c = row["ml_cost"]
        if c <= EFF_ML_GOOD:
            return ("优秀", "tag-green")
        if c <= EFF_ML_OK:
            return ("合格", "tag-gold")
        return ("成本偏高", "tag-red")
    if row["msg"] > 0 and row["msg_cost"] is not None:
        c = row["msg_cost"]
        if c <= EFF_MSG_OK:
            return ("有开口", "tag-green")
        return ("开口偏贵", "tag-gold")
    if row["fee"] >= WASTE_FEE:
        return ("高耗无转化", "tag-red")
    return ("—", "tag-gray")


# 徽标 → 下一步动作建议（P1-6：不只说"贵了"，要说"该干什么"）
EFF_ACTION = {
    "优秀": "可加投 / 复制同款",
    "合格": "维持，观察成本走势",
    "成本偏高": "降预算或换首图再测",
    "开口偏贵": "优化私信话术与落地页",
    "有开口": "补齐留资组件，把开口转成留资",
    "高耗无转化": "先停投，排查定向与落地页",
    "—": "数据量不足，继续观察",
}


def eff_action(tag):
    return EFF_ACTION.get(tag, "")


def note_url(nid, title=""):
    """
    笔记原文直达。
    ⚠ 实测：https://www.xiaohongshu.com/explore/<note_id> 未登录访问会失效
    （需要接口不提供的 xsec_token）。可行替代是「站内搜索笔记标题」，实测能命中。
    """
    if title:
        return ("https://www.xiaohongshu.com/search_result?keyword="
                + quote(title[:30]))
    if nid:
        return f"https://www.xiaohongshu.com/explore/{nid}"
    return ""


# ---------------- 环比与待办 ----------------

def snapshot_totals(snap):
    """从任意快照（创意级或账户级）抽出六个总量，用于环比计算"""
    if not snap:
        return None
    s = snap.get("summary", {}) or {}
    imp = clk = inter = 0.0
    for adv in (snap.get("advertisers") or {}).values():
        for c in adv.get("creatives") or []:
            imp += _num(c.get("impression"))
            clk += _num(c.get("click"))
            inter += sum(_num(c.get(k))
                         for k in ("like", "comment", "collect", "share", "follow"))
    return {
        "fee": _num(s.get("total_fee")), "imp": imp, "clk": clk, "inter": inter,
        "msg": _num(s.get("total_msg")), "ml": _num(s.get("total_msg_leads")),
    }


def mom_html(cur, prev, better_up=True, unit=""):
    """环比：颜色按「好/坏」而不是机械涨跌 —— 成本下降是好，应显示绿色"""
    if prev is None or cur is None:
        return '<div class="kpi-mom flat">—</div>'
    if prev == 0:
        # 上期为 0：可能是新增，也可能是上期不在本地缓存覆盖范围内 —— 不编造结论
        return '<div class="kpi-mom flat">— 无上期数据</div>'
    d = (cur - prev) / prev * 100
    if abs(d) < 0.05:
        return '<div class="kpi-mom flat">持平 <span>较上一周期</span></div>'
    arrow = "▲" if d > 0 else "▼"
    good = (d > 0) if better_up else (d < 0)
    cls = "good" if good else "bad"
    return (f'<div class="kpi-mom {cls}">{arrow} {abs(d):.1f}%'
            f' <span style="color:{DIMT}">较上一周期</span></div>')


def aggregate_notes(rows):
    """
    按 note_id 聚合：同一篇笔记投在多个单元 / 多个创意时合并成一行。
    没有 note_id 的（极少数）按创意 ID 单独成行，不合并。
    """
    groups, order = {}, []
    for r in rows:
        k = ("n:" + r["note_id"]) if r["note_id"] else ("c:" + (r["cid"] or r["name"]))
        g = groups.get(k)
        if g is None:
            g = dict(r)
            g["_n"] = 1
            g["_advs"] = {r["adv"]}
            g["_cities"] = {r["city"]}
            g["_names"] = [r["name"]]
            groups[k] = g
            order.append(k)
            continue
        g["_n"] += 1
        g["_advs"].add(r["adv"])
        g["_cities"].add(r["city"])
        if r["name"] not in g["_names"]:
            g["_names"].append(r["name"])
        for f in ("fee", "imp", "clk", "inter", "msg", "ml"):
            g[f] = _num(g.get(f)) + _num(r.get(f))
        if not g.get("img") and r.get("img"):
            g["img"] = r["img"]
    out = [groups[k] for k in order]
    for g in out:
        g["ctr"] = (g["clk"] / g["imp"] * 100) if g["imp"] else 0.0
        g["ir"] = (g["inter"] / g["imp"] * 100) if g["imp"] else 0.0
        g["msg_cost"] = (g["fee"] / g["msg"]) if g["msg"] else None
        g["ml_cost"] = (g["fee"] / g["ml"]) if g["ml"] else None
    out.sort(key=lambda r: -r["fee"])
    return out


def todo_stats(snapshot):
    """
    「今日待办」的四项：把「看数据」变成「给指令」。
    全部用本地已有数据算，不额外打接口。
    """
    try:
        from core import tokenhub
        accounts = tokenhub.load_accounts()
        pending = len(tokenhub.pending_list())
    except Exception:
        accounts, pending = {}, None

    live = set((snapshot.get("advertisers") or {}).keys())
    all_adv = set()
    for _k, rec in accounts.items():
        for a in rec.get("advertisers") or []:
            if a.get("advertiser_id") is not None:
                all_adv.add(str(a["advertiser_id"]))
    idle = len(all_adv - live) if all_adv else None
    return {"pending": pending, "idle": idle}


AGENCY_FILE = os.path.join(DATA_DIR, "代理商映射.json")


def load_agency_map():
    """人工维护的代理商映射（advertiser_id → 代理商名）。没有就返回空表。"""
    try:
        with open(AGENCY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items() if v}
    except Exception:
        return {}


def acct_label_map():
    """
    主体键 → 账号展示信息。
    账号名优先用**小红书专业号的真实名字**（账号清单里的名称，就是实际在投放的那个号），
    匹配不到才退回推断别名（如 滨州·主号），并打标提示。
    """
    try:
        from core import accounts_meta
        info = accounts_meta.build()
        out = {}
        for a in info["accounts"]:
            login = a.get("login") or []
            real = (login[0]["name"] if login else "").strip()
            out[a["key"]] = {
                "label": a["label"],
                "real": real,
                "login_account": login[0]["account"] if login else "",
                "login_more": [x["name"] for x in login[1:]],
            }
        return out
    except Exception:
        return {}


def _auth_dot(uid):
    """
    P1-5：专业号 ID 对运营是零信息量，改成「授权状态点」。
    原始 ID 保留在 title 里，需要排查时悬停即可看到。
    """
    if uid:
        return (f'<span class="tv-dot on" title="小红书专业号 {esc(uid)}"></span>'
                f'<span class="tv-meta" title="小红书专业号 {esc(uid)}">已授权</span>')
    return ('<span class="tv-dot off" title="未识别到专业号"></span>'
            '<span class="tv-meta">未识别</span>')


def build_tree(snapshot, uid_map=None, inferred=None, acct_labels=None):
    """
    「店铺 → 小红书账号 → 投放户 → 素材」四级折叠树。

    分组口径：**按账号归属**。广告主覆盖 ≥3 个城市的账号（如总号矩阵）单独归到
    「跨城矩阵」节点，避免把总店的跨城户算进某个城市、导致两个页面两个数字。
    """
    advs = snapshot.get("advertisers") or {}
    uid_map = uid_map or {}
    inferred = inferred or {}
    acct_labels = acct_labels or {}
    agency = load_agency_map()

    owner_cities = {}
    for aid, adv in advs.items():
        k = adv.get("owner_key") or ""
        owner_cities.setdefault(k, set()).add(adv.get("city") or "其他")
    cross = {k for k, v in owner_cities.items() if len(v) >= 3}

    groups = {}
    for aid, adv in advs.items():
        city = adv.get("city") or "其他"
        k = adv.get("owner_key") or ""
        grp = "跨城矩阵" if k in cross else city
        groups.setdefault(grp, {}).setdefault(k, []).append((aid, adv))

    def adv_fee(adv):
        return sum(_num(c.get("fee")) for c in adv.get("creatives") or [])

    def grp_sort(name):
        if name == "跨城矩阵":
            return (2, "", 0)
        return (0, name, 0)

    def label_of(key, adv, uid, is_cross=False):
        """
        账号展示名：优先小红书专业号的真实名字。
        跨城矩阵账号（如总号矩阵）不能用真实名 —— 它的投放户横跨多城，
        按户名匹配会误配到某个城市的账号，这里直接用别名。
        """
        info = acct_labels.get(key) or {}
        if is_cross and info.get("label"):
            return info["label"], True
        real = (info.get("real") or "").strip()
        if real:
            return real, True
        lb = info.get("label")
        if lb:
            return lb, False
        nuid = (adv.get("owner_user_id") or "").strip()
        return (uid_map.get(nuid) or inferred.get(nuid)
                or adv.get("owner_label") or "未识别账号"), False

    # 顺序：按该组消耗倒序
    grp_rows = []
    for grp, owners in groups.items():
        g_fee = sum(adv_fee(a) for lst in owners.values() for _, a in lst)
        grp_rows.append((grp, owners, g_fee))
    grp_rows.sort(key=lambda x: (x[0] == "跨城矩阵", -x[2]))

    html = []
    for grp, owners, g_fee in grp_rows:
        g_adv = sum(len(l) for l in owners.values())
        g_cr = sum(len(a.get("creatives") or []) for l in owners.values() for _, a in l)
        g_open = " open" if grp != "跨城矩阵" else ""
        html.append(f'<details class="tv-shop"{g_open}><summary>'
                    f'<span class="tv-caret"></span>'
                    f'<span class="tv-name">{esc(grp)}</span>'
                    f'<span class="tv-meta">{len(owners)} 个账号 · {g_adv} 个投放户 · '
                    f'{g_cr} 条素材</span>'
                    f'<span class="tv-fee">{fmt_money(g_fee)}</span>'
                    f'</summary><div class="tv-body">')

        o_rows = []
        for key, lst in owners.items():
            o_fee = sum(adv_fee(a) for _, a in lst)
            o_cr = sum(len(a.get("creatives") or []) for _, a in lst)
            uid = ""
            for _, a in lst:
                uid = (a.get("owner_user_id") or "").strip() or uid
            nm, is_real = label_of(key, lst[0][1], uid, key in cross)
            alias_tag = ("" if is_real
                         else '<span class="tv-alias">别名·非专业号名</span>')
            o_rows.append((o_fee, key, lst, uid, nm, o_cr, alias_tag))
        o_rows.sort(key=lambda x: -x[0])

        for o_fee, key, lst, uid, nm, o_cr, alias_tag in o_rows:
            html.append('<details class="tv-acc"><summary>'
                        '<span class="tv-caret"></span>'
                        f'<span class="tv-name">{esc(nm)}</span>'
                        f'{alias_tag}'
                        f'{_auth_dot(uid)}'
                        f'<span class="tv-meta">{len(lst)} 个投放户 · {o_cr} 条素材</span>'
                        f'<span class="tv-fee">{fmt_money(o_fee)}</span>'
                        '</summary><div class="tv-body">')
            a_rows = sorted(lst, key=lambda x: -adv_fee(x[1]))
            for aid, a in a_rows:
                cr = a.get("creatives") or []
                a_fee = adv_fee(a)
                ag = agency.get(str(aid), "")
                ag_html = (f'<span class="tv-ag">{esc(ag)}</span>' if ag
                           else '<span class="tv-ag tv-ag-empty">代理商未填</span>')
                html.append('<details class="tv-adv"><summary>'
                            '<span class="tv-caret"></span>'
                            f'<span class="tv-id2">{esc(str(aid))}</span>'
                            f'<span class="tv-name">{esc(a.get("name") or "")}</span>'
                            f'{ag_html}'
                            f'<span class="tv-meta">{len(cr)} 条素材</span>'
                            f'<span class="tv-fee">{fmt_money(a_fee)}</span>'
                            '</summary><div class="tv-body tv-creatives">')
                if not cr:
                    html.append('<div class="tv-empty">当日无投放素材</div>')
                for c in sorted(cr, key=lambda x: -_num(x.get("fee"))):
                    img = c.get("creativity_image") or ""
                    im = (f'<img class="tv-img" src="{esc(img)}" loading="lazy" '
                          f'referrerpolicy="no-referrer">' if img
                          else '<span class="tv-img tv-noimg"></span>')
                    fee = _num(c.get("fee"))
                    msg = _num(c.get("message_consult"))
                    ml = _num(c.get("msg_leads_num"))
                    mlc = (f'¥{fee / ml:,.0f}' if ml else "—")
                    cname = ("区间汇总（账户级）" if c.get("is_pseudo")
                             else (c.get("creativity_name") or "(未命名素材)"))
                    html.append('<div class="tv-cr">'
                                f'{im}'
                                f'<span class="tv-cr-name">{esc(cname[:34])}</span>'
                                f'<span class="tv-cr-num">{fmt_money(fee)}</span>'
                                f'<span class="tv-cr-num">私信 {fmt_int(msg)}</span>'
                                f'<span class="tv-cr-num">留资 {fmt_int(ml)}</span>'
                                f'<span class="tv-cr-cost">{mlc}</span>'
                                '</div>')
                html.append('</div></details>')
            html.append('</div></details>')
        html.append('</div></details>')
    return "".join(html) or '<div class="empty">暂无数据</div>'


def build_html(snapshot, title_suffix="", source_note="",
               ranges=None, current_range="latest", mom=None):
    """
    mom = {"cur": {...}, "prev": {...}} —— 环比用的两个总量快照。
    必须来自**同一口径**：环比是比值，只要两边同口径就可信；
    因此区间视图统一用账户日粒度算两边，而不是拿创意口径的展示值去比账户口径的上期值。
    """
    s = snapshot.get("summary", {}) or {}
    # 投放户余额：把缓存的实时余额挂到 snapshot，供账户表「可用余额」列展示
    try:
        from core import balances
        balances.attach(snapshot)
    except Exception:
        pass
    _uid_map, _inferred = build_owner_map(snapshot)
    rows = flatten(snapshot, _uid_map, _inferred)
    active = [r for r in rows if r["fee"] > 0]
    active.sort(key=lambda r: -r["fee"])

    # 笔记标题 / 首图（来自笔记维度报表；没拉到时退化成内部创意名，不报错）
    try:
        from core import notemeta
        note_meta = notemeta.titles()
    except Exception:
        note_meta = {}

    # 三个分组维度各自的聚合结果
    dim_agg = {k: aggregate_by(active, k) for k, _, _ in DIMS}

    # 归属树（店铺 → 账号 → 投放户 → 素材）
    tree_html = build_tree(snapshot, _uid_map, _inferred, acct_label_map())
    tree_adv_n = len(snapshot.get("advertisers") or {})

    # ---------- 区间视图（账户粒度）----------
    gran = snapshot.get("granularity") or "creative"
    is_range_view = (gran == "advertiser")
    trend_rows = snapshot.get("trend") or []
    trend_html = ""
    if len(trend_rows) >= 2:
        trend_html = svg_line([float(t.get("fee") or 0) for t in trend_rows],
                              [str(t.get("date") or "")[5:] for t in trend_rows])
    days_cov = s.get("days_covered")
    # 封面列始终保留：账户粒度行也会带「代表封面」，不再留空
    cover_th = "<th>封面</th>"
    cover_th_row = cover_th  # 表头占位

    total_fee = s.get("total_fee", 0) or 0
    total_imp = sum(r["imp"] for r in rows)
    total_clk = sum(r["clk"] for r in rows)
    total_inter = sum(r["inter"] for r in rows)
    total_msg = s.get("total_msg", 0) or 0
    total_ml = s.get("total_msg_leads", 0) or 0
    ctr = (total_clk / total_imp * 100) if total_imp else 0
    ir = (total_inter / total_imp * 100) if total_imp else 0
    msg_cost = (total_fee / total_msg) if total_msg else None
    ml_cost = (total_fee / total_ml) if total_ml else None

    period = snapshot.get("date", "")
    rng = snapshot.get("range") or {}
    if rng and rng.get("start") != rng.get("end"):
        period_txt = f"{rng['start']} ~ {rng['end']}"
    else:
        period_txt = period

    # ---------- 时间范围切换（仅服务模式下可用） ----------
    range_html = ""
    if ranges:
        range_html = '<div class="rtabs">' + "".join(
            f'<a class="rtab{" active" if k == current_range else ""}" '
            f'href="?range={k}">{esc(n)}</a>' for k, n in ranges) + "</div>"

    # ---------- KPI（P0-3：每个指标带上一周期环比） ----------
    mc_ = (mom or {}).get("cur") or {}
    mp_ = (mom or {}).get("prev") or {}
    if not mc_:
        mc_ = {"fee": total_fee, "imp": total_imp, "clk": total_clk,
               "msg": total_msg, "ml": total_ml}
    _r = lambda d, k: (d[k] / d["imp"] * 100) if d.get("imp") else None      # noqa: E731
    _c = lambda d: (d["fee"] / d["ml"]) if d.get("ml") else None             # noqa: E731
    kpis = [
        ("消耗", fmt_money(total_fee), "账户口径" if is_range_view else "创意口径",
         GOLD_T, mc_.get("fee"), mp_.get("fee"), True),
        ("展现", fmt_int(total_imp), f"点击 {fmt_int(total_clk)}",
         INK, mc_.get("imp"), mp_.get("imp"), True),
        ("点击率", f"{ctr:.2f}%", f"互动率 {ir:.2f}%",
         INK, _r(mc_, "clk"), _r(mp_, "clk"), True),
        ("私信咨询", fmt_int(total_msg), "开口数",
         GREEN, mc_.get("msg"), mp_.get("msg"), True),
        ("留资", fmt_int(total_ml), "表单/客资",
         GREEN, mc_.get("ml"), mp_.get("ml"), True),
        ("留资成本", fmt_money(ml_cost) if ml_cost else "—", "留资 ÷ 消耗",
         RED, _c(mc_), _c(mp_), False),   # 成本上升是坏事，好坏方向取反
    ]
    # 深色头部下用 CSS 类控制数字颜色（gold/green/red），而非 inline style
    _kpi_color_cls = {GOLD_T: "gold", INK: "", GREEN: "green", RED: "red"}
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-top"><span class="kpi-label">{esc(l)}</span>'
        f'<span class="kpi-src">{esc(src)}</span></div>'
        f'<div class="kpi-val {_kpi_color_cls.get(color, "")}">{esc(val)}</div>'
        f'{mom_html(cv, pv, bu)}</div>'
        for l, val, src, color, cv, pv, bu in kpis)

    # ---------- 今日待办（P0-3：从"看数据"到"给指令"） ----------
    over_cost = [r for r in active
                 if r["ml_cost"] is not None and r["ml_cost"] > EFF_ML_OK]
    over_fee = sum(r["fee"] for r in over_cost)
    no_conv = [r for r in active
               if r["fee"] >= WASTE_FEE and r["msg"] == 0 and r["ml"] == 0]
    ts = todo_stats(snapshot)
    todo_items = [
        ("crit", "超成本线素材", f"{len(over_cost)} 条",
         f"留资成本 &gt; ¥{EFF_ML_OK}，已花 ¥{over_fee:,.0f} · 建议降预算或换首图"),
        ("crit", "高耗无转化", f"{len(no_conv)} 条",
         f"消耗 ≥ ¥{WASTE_FEE} 且零私信零留资 · 建议先停投再排查"),
    ]
    if ts.get("idle") is not None:
        todo_items.append(("warn", "有授权但零消耗", f"{ts['idle']} 个投放户",
                           "本区间没跑出数据 · 确认是否在投或授权是否失效"))
    if ts.get("pending") is not None:
        todo_items.append(("warn", "待授权主体", f"{ts['pending']} 个",
                           "未授权则拿不到笔记与素材数据 · 点右下角授权管理"))
    todo_html = ('<div class="todo">' + "".join(
        f'<div class="todo-item {cls}"><div class="tl">{esc(l)}</div>'
        f'<div class="tv2">{esc(v)}</div><div class="td">{d}</div></div>'
        for cls, l, v, d in todo_items) + "</div>")

    # ---------- 素材效率分布（留资成本区间） ----------
    buckets = [("≤100", 0, 100), ("100-200", 100, 200), ("200-400", 200, 400),
               ("400-800", 400, 800), (">800", 800, 1e9)]
    bucket_counts = defaultdict(int)
    bucket_fee = defaultdict(float)
    for r in active:
        if r["ml_cost"] is not None:
            for name, lo, hi in buckets:
                if lo <= r["ml_cost"] < hi:
                    bucket_counts[name] += 1
                    bucket_fee[name] += r["fee"]
                    break
    max_bc = max(bucket_counts.values()) if bucket_counts else 1
    bucket_html = "".join(
        bar_row(f"{n} 元", bucket_counts.get(n, 0), max_bc,
                f"{bucket_counts.get(n,0)} 条 · ¥{bucket_fee.get(n,0):,.0f}",
                GREEN if lo < 200 else (GOLD if lo < 400 else RED))
        for n, lo, hi in buckets)

    # ---------- 素材榜（P0-2：按笔记聚合，首列三段式） ----------
    mat_rows = aggregate_notes(active)[:60]
    mat_note_n = len(mat_rows)
    max_ctr = max((r["ctr"] for r in mat_rows), default=0) or 1

    def cr_row(i, r):
        tag, cls = eff_tag(r)
        meta = note_meta.get(r["note_id"]) or {}
        title = (meta.get("title") or "").strip() or r["name"]
        img_src = (meta.get("image") or "").strip() or r["img"]
        inner = " / ".join(r.get("_names") or [r["name"]])[:70]
        if r["img"] or img_src:
            src = img_src or r["img"]
            img = (f'<img class="cr-img" src="{esc(src)}" loading="lazy" '
                   f'referrerpolicy="no-referrer" onerror="this.style.visibility=\'hidden\'">')
            preview = (f'<div class="cr-preview"><img src="{esc(src)}" referrerpolicy="no-referrer">'
                       f'<div class="cr-pv-meta">{esc(title[:44])}</div></div>')
        else:
            img = ('<span class="cr-img cr-img-blank"></span>' if r.get("pseudo")
                   else '<span class="cr-img noimg">无图</span>')
            preview = ""
        # 封面点击率迷你条：让"这张封面强不强"在列里就能横向比
        ctr_bar = (f'<span class="ctr-bar"><i style="width:'
                   f'{min(100, r["ctr"] / max_ctr * 100):.0f}%"></i></span>')
        url = note_url(r["note_id"], title)
        link = (f'<a class="cr-link" href="{esc(url)}" target="_blank" rel="noopener" '
                f'title="在小红书站内搜索这条笔记">↗</a>' if url else "")
        n_cr = r.get("_n") or 1
        lc = fmt_money(r["ml_cost"]) if r["ml_cost"] else "—"
        belong = f'{esc(r["city"])} · {esc(short_name(r["adv"], 14))} · {esc(r["owner"][:12])}'
        sub2 = ""
        if title != r["name"] and inner:
            sub2 = (f'<div class="m-sub2" title="{esc(inner)}">'
                    f'{esc(inner[:52])}　·　{n_cr} 条创意</div>')
        elif n_cr > 1:
            sub2 = f'<div class="m-sub2">{n_cr} 条创意</div>'
        # 留资成本颜色：好=绿，差=红
        lc_cls = "num"
        if r["ml_cost"] is not None:
            c = r["ml_cost"]
            if c <= EFF_ML_GOOD:
                lc_cls = "num lc-good"
            elif c > EFF_ML_OK:
                lc_cls = "num lc-bad"
        # 留资0灰色
        ml_val = int(r["ml"])
        ml_cls = "num hi" if ml_val > 0 else "num zero"
        # CTR高亮
        ctr_cls = "num ctr-hi" if r["ctr"] >= 8 else "num"
        return (f'<tr data-city="{esc(r["city"])}" data-adv="{esc(r["adv"])}" '
                f'data-owner="{esc(r["owner"])}">'
                f'<td class="cr-cell"><span class="cr-wrap">{img}{ctr_bar}</span>'
                f'{preview}{link}</td>'
                f'<td class="cr-note">'
                f'<div class="m-title" title="{esc(title)}">{esc(title[:34])}</div>'
                f'<div class="m-sub">{belong}</div>'
                f'{sub2}</td>'
                f'<td class="num fee-bold">{fmt_money(r["fee"])}</td>'
                f'<td class="num">{fmt_int(r["imp"])}</td>'
                f'<td class="num">{fmt_int(r["clk"])}</td>'
                f'<td class="{ctr_cls}">{r["ctr"]:.2f}%</td>'
                f'<td class="num">{fmt_int(r["inter"])}</td>'
                f'<td class="num">{int(r["msg"])}</td>'
                f'<td class="{ml_cls}">{ml_val}</td>'
                f'<td class="{lc_cls}">{lc}</td>'
                f'<td><span class="tag {cls}">{tag}</span>'
                f'<div class="act-tip">{esc(eff_action(tag))}</div></td></tr>')

    top_n = 60
    cr_rows = "".join(cr_row(i, r) for i, r in enumerate(mat_rows, 1))

    # 区间视图的标题与提示（区分「素材粒度」和「账户粒度」两种区间）
    is_creative_range = (gran == "creative" and (snapshot.get("source") == "range_creative"))
    name_th = "投放账户" if is_range_view else "笔记 / 归属"
    list_title = ("投放账户榜" if is_range_view
                  else ("区间素材榜" if is_creative_range else "素材获客榜"))
    list_chip = (("区间汇总 · 账户粒度") if is_range_view
                 else ("区间汇总 · 素材粒度" if is_creative_range
                       else f"按消耗排序 · 前 {min(top_n, len(active))} 条"))
    if is_range_view:
        list_hint = ("当前区间超过 31 天，聚光创意维度行数到万级（实测近一年单户 16,194 行），"
                     "所以这里展示<b>投放户粒度汇总</b>，封面取该户最近一次投放的代表素材。"
                     "想看素材明细请切到「近 7 日」「本月」或「上月」。")
    elif is_creative_range:
        list_hint = ("该区间的素材明细（同一素材跨天的数据已合并）。封面即素材本体，"
                     "鼠标悬停可看大图。留资成本 = 消耗 ÷ 留资，是判断素材值不值得继续投的核心指标。")
    else:
        list_hint = ("封面即素材本体，鼠标悬停可看大图。"
                     "留资成本 = 消耗 ÷ 留资，是判断素材值不值得继续投的核心指标")

    # ---------- 三维度：素材榜内的维度切换 + 下钻标签 ----------
    # 维度切换不再占顶部固定行（之前整行把素材榜高度挤扁，滚动只看 1-2 行）。
    # 改为放进素材榜卡片内：点「城市/投放账号/小红书账号」展开对应选项，点选项下钻。
    dim_tabs = {}
    dim_rests = {}
    dim_more = {}
    for dkey, dlabel, dhint in DIMS:
        items = [(n, d) for n, d in dim_agg[dkey] if d["fee"] > 0]
        allc = [(
            f'<button class="cbtn" data-dim="{dkey}" data-key="{esc(n)}" '
            f'title="{esc(n)} · {int(d["n"])} 条素材">'
            f'{esc(short_name(n, 10))} <span class="cbadge">{int(d["n"])}</span></button>'
        ) for n, d in items]
        dim_tabs[dkey] = (
            f'<button class="cbtn active" data-dim="{dkey}" data-key="all" '
            f'title="不限{dlabel}">全部 <span class="cbadge">{len(active)}</span></button>'
            + "".join(allc[:12 if dkey != "city" else len(allc)]))
        dim_rests[dkey] = "".join(allc[12:] if dkey != "city" else [])
        dim_more[dkey] = (len(allc) - 12) if dkey != "city" else 0

    cities = [(n, d) for n, d in dim_agg["city"] if d["fee"] > 0]

    # ---------- 高耗无转化预警（素材分析的核心价值：把钱花在哪了） ----------
    waste_all = [r for r in active
                 if r["fee"] >= WASTE_FEE and r["msg"] == 0 and r["ml"] == 0]
    waste = waste_all[:8]
    waste_fee = sum(r["fee"] for r in waste)
    waste_html = ""
    for r in waste:
        meta = note_meta.get(r["note_id"]) or {}
        title = (meta.get("title") or "").strip() or r["name"]
        img = (f'<img class="w-img" src="{esc(r["img"])}" loading="lazy" referrerpolicy="no-referrer" '
               f'onerror="this.style.visibility=\'hidden\'">' if r["img"]
               else '<span class="w-img noimg"></span>')
        url = note_url(r["note_id"], title)
        go = (f'<a class="w-go" href="{esc(url)}" target="_blank" rel="noopener">'
              f'查看笔记 ↗</a>' if url else "")
        # P1-4：数值紧贴信息右侧（右栏固定 186px），不再横跨整屏
        waste_html += (f'<div class="waste-row">'
                       f'<div class="waste-left">{img}'
                       f'<div class="w-info"><div class="w-name" title="{esc(title)}">'
                       f'{esc(title[:26])}</div>'
                       f'<div class="w-sub">{esc(r["city"])} · 展现 {fmt_int(r["imp"])} · '
                       f'点击 {fmt_int(r["clk"])} · CTR {r["ctr"]:.2f}%　{go}</div></div></div>'
                       f'<span class="w-lead"></span>'
                       f'<div class="w-nums"><span class="a">{fmt_money(r["fee"])}</span>'
                       f'<span class="b">零私信 · 零留资</span></div></div>')
    if not waste:
        waste_html = '<div class="empty">暂无高耗无转化素材，表现健康</div>'

    # ---------- 预警横幅（首屏可见，不藏在tab里） ----------
    waste_fee_total = sum(r["fee"] for r in waste_all)
    if waste_all:
        alert_strip_html = (
            f'<div class="alert-strip">'
            f'<div class="a-icon">⚠</div>'
            f'<div class="a-body">'
            f'<div class="a-title">{len(waste_all)} 条素材在烧钱，零转化</div>'
            f'<div class="a-desc">消耗 ≥ ¥{WASTE_FEE} 却零私信零留资 · '
            f'优先排查定向、落地页与素材钩子</div>'
            f'</div>'
            f'<div class="a-fee">¥{waste_fee_total:,.0f}</div>'
            f'</div>')
    else:
        alert_strip_html = (
            '<div class="alert-strip ok">'
            '<div class="a-icon">✓</div>'
            '<div class="a-body">'
            '<div class="a-title">暂无高耗无转化素材</div>'
            '<div class="a-desc">当前区间没有消耗 ≥ ¥' + str(WASTE_FEE) + ' 且零转化的素材</div>'
            '</div></div>')

    # ---------- 账户 ----------
    acct = []
    for aid, adv in (snapshot.get("advertisers") or {}).items():
        cs = adv.get("creatives", [])
        if not cs:
            continue
        f_ = sum(_num(c.get("fee")) for c in cs)
        m_ = sum(_num(c.get("message_consult")) for c in cs)
        l_ = sum(_num(c.get("msg_leads_num")) for c in cs)
        i_ = sum(_num(c.get("impression")) for c in cs)
        cl_ = sum(_num(c.get("click")) for c in cs)
        bal = adv.get("balance")
        bal_disp = fmt_money(bal["available_balance"]) if bal and bal.get("available_balance") else "—"
        acct.append({"name": adv.get("name", aid), "city": adv.get("city", "其他"),
                     "fee": f_, "msg": m_, "ml": l_, "imp": i_, "clk": cl_, "n": len(cs),
                     "balance": bal, "bal_disp": bal_disp})
    acct.sort(key=lambda x: -x["fee"])
    acct_rows = "".join(
        f'<tr><td class="aname" title="{esc(a["name"])}">{esc(a["name"][:30])}</td>'
        f'<td>{esc(a["city"])}</td>'
        f'<td class="num bal">{a["bal_disp"]}</td>'
        f'<td class="num">{fmt_money(a["fee"])}</td>'
        f'<td class="num">{fmt_int(a["imp"])}</td><td class="num">{fmt_int(a["clk"])}</td>'
        f'<td class="num">{a["n"]}</td><td class="num hi">{int(a["msg"])}</td>'
        f'<td class="num hi">{int(a["ml"])}</td>'
        f'<td class="num">{(fmt_money(a["fee"]/a["ml"])) if a["ml"] else "—"}</td></tr>'
        for a in acct)

    # ---------- 城市消耗柱状对比 ----------
    trend_svg = svg_bar([(c, d["fee"]) for c, d in cities[:12]])

    funnel = [
        ("展现", total_imp, 1.0),
        ("点击", total_clk, (total_clk / total_imp) if total_imp else 0),
        ("互动", total_inter, (total_inter / total_imp) if total_imp else 0),
        ("私信", total_msg, (total_msg / total_imp) if total_imp else 0),
        ("留资", total_ml, (total_ml / total_imp) if total_imp else 0),
    ]
    base = funnel[0][1] or 1
    funnel_html = "".join(
        f'<div class="fn-row"><span class="fn-l">{esc(n)}</span>'
        f'<span class="fn-track"><span class="fn-fill" style="width:{max(2, v/base*100):.1f}%"></span></span>'
        f'<span class="fn-v">{fmt_int(v)}</span>'
        f'<span class="fn-r">{("0.00%" if i==0 else f"{rate*100:.2f}%")}</span></div>'
        for i, (n, v, rate) in enumerate(funnel))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>夏天 · 小红书聚光投放看板</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"PingFang SC","Microsoft YaHei","Segoe UI",system-ui,sans-serif;
background:#F3F5FA;color:{INK};padding:0;-webkit-font-smoothing:antialiased;
font-size:14px;line-height:1.6;min-height:100vh;overflow-y:auto}}
.wrap{{max-width:1760px;width:100%;margin:0 auto}}
/* ===== 深色品牌头部 ===== */
.hero{{flex:none;background:linear-gradient(135deg,#111A2C 0%,#1A2740 55%,#223354 100%);
padding:0 24px;position:relative;overflow:hidden}}
.hero::before{{content:"";position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,transparent 0%,{GOLD} 30%,{GOLD} 70%,transparent 100%);opacity:.65}}
.hero-row1{{display:flex;align-items:center;gap:14px;padding:10px 0 8px;flex-wrap:wrap;position:relative;z-index:1}}
.brand{{display:flex;align-items:center;gap:11px;flex:none}}
.logo{{width:40px;height:40px;border-radius:10px;
background:linear-gradient(135deg,{GOLD} 0%,#E8CE8F 100%);
display:flex;align-items:center;justify-content:center;color:#1A2740;font-weight:700;font-size:17px;
box-shadow:0 3px 10px rgba(200,169,106,.3),inset 0 1px 0 rgba(255,255,255,.35)}}
h1{{font-size:19px;font-weight:600;letter-spacing:-.2px;color:#fff;white-space:nowrap}}
.brand .sub{{font-size:11.5px;color:rgba(255,255,255,.42);margin-top:1px;letter-spacing:.3px}}
.hero-actions{{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.src{{display:flex;align-items:center;gap:6px;font-size:11.5px;color:rgba(255,255,255,.45);flex:none;white-space:nowrap}}
.dot{{width:6px;height:6px;border-radius:50%;background:#4ECB8A;
box-shadow:0 0 0 2.5px rgba(78,203,138,.16)}}

/* KPI 数据条 */
.hero-row2{{display:grid;grid-template-columns:repeat(6,1fr);
padding:2px 0 12px;border-top:1px solid rgba(255,255,255,.07);position:relative;z-index:1}}
.cardtabs{{flex:none}}
.kpi{{padding:8px 16px 2px;border-right:1px solid rgba(255,255,255,.05)}}
.kpi:last-child{{border-right:none}}
.kpi-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;gap:6px}}
.kpi-label{{font-size:11.5px;color:rgba(255,255,255,.48);white-space:nowrap;font-weight:500}}
.kpi-src{{font-size:9.5px;color:rgba(255,255,255,.28);background:transparent;
padding:0;white-space:nowrap;flex:none}}
.kpi-val{{font-size:23px;font-weight:700;letter-spacing:-.8px;font-variant-numeric:tabular-nums;
color:#fff;line-height:1.15}}
.kpi-val.gold{{color:#E8CE8F}}
.kpi-val.green{{color:#5FD99E}}
.kpi-val.red{{color:#FF7A72}}

/* ===== 预警横幅 ===== */
.alert-strip{{display:flex;align-items:center;gap:12px;border-radius:10px;padding:9px 14px;margin-bottom:10px;
font-size:13px;background:linear-gradient(135deg,#FFF5F5,#FFF0EE);border:1px solid #F3C6C2;
border-left:4px solid #E0564E;color:#8B3A35}}
.alert-strip.ok{{background:linear-gradient(135deg,#F0FAF4,#E8F8EF);border:1px solid #B8E0CC;
border-left:4px solid #2FA36B;color:#1F6B45}}
.alert-strip .a-icon{{font-size:16px;flex:none}}
.alert-strip .a-body{{flex:1;min-width:0}}
.alert-strip .a-title{{font-weight:600;font-size:13.5px}}
.alert-strip .a-desc{{font-size:12px;opacity:.75;margin-top:1px}}
.alert-strip .a-fee{{font-size:18px;font-weight:700;color:#E0564E;flex:none}}

/* ---------- 素材榜内：维度切换 + 下钻（并入工具栏，不再单独占一层） ---------- */
.dimtip{{font-size:12.5px;color:{DIMT};margin-right:2px;flex:none}}
.dimchips{{display:flex;flex-wrap:wrap;gap:5px;align-items:center;
flex:1 1 160px;min-width:120px;
-webkit-overflow-scrolling:touch;-webkit-mask-image:linear-gradient(90deg,#000 94%,transparent);
mask-image:linear-gradient(90deg,#000 94%,transparent)}}
.dimchips::-webkit-scrollbar{{display:none}}
.dbtn{{font-size:13.5px;padding:7px 15px;border-radius:9px;border:1px solid #E6EAF3;
background:#fff;color:{MUT};cursor:pointer;transition:.15s;font-weight:500;font-family:inherit}}
.dbtn:hover{{border-color:{GOLD_T};color:{GOLD_T}}}
.dbtn.active{{background:{GOLD_BG};border-color:{GOLD_BG};color:#fff}}
.cbadge{{font-size:10.5px;opacity:.6;margin-left:3px}}
/* (旧 .kpis/.kpi 白色卡片样式已迁移至 .hero-row2) */
.grid{{display:grid;gap:20px;margin-bottom:20px}}
.g2{{grid-template-columns:1.55fr 1fr}}
.g2b{{grid-template-columns:1fr 1fr}}

/* ---------- 卡片切换（页面不再纵向拉长） ---------- */
html{{}}
.cardtabs{{display:flex;gap:7px;background:#fff;border-radius:14px;padding:6px;
margin-bottom:14px;box-shadow:0 1px 3px rgba(24,35,55,.05);flex:none;overflow-x:auto}}
.cardtab{{font-size:15px;padding:8px 18px;border-radius:10px;border:none;background:transparent;
color:{MUT};cursor:pointer;font-family:inherit;font-weight:500;white-space:nowrap;
transition:.15s;display:flex;align-items:center;gap:9px}}
.cardtab:hover{{background:#F5F7FB;color:{INK}}}
.cardtab.active{{background:{GOLD_BG};color:#fff}}
.cardtab .n{{font-size:12.5px;background:#F0F2F7;color:{DIMT};border-radius:8px;padding:1.5px 8px}}
.cardtab.active .n{{background:rgba(255,255,255,.3);color:#fff}}
/* 单页布局：所有内容直接排列，不再用pane切换 */
.pane{{display:block}}

/* ---------- 归属树 ---------- */
.tv-shop,.tv-acc,.tv-adv{{border-radius:12px;margin-bottom:10px}}
.tv-shop{{background:#fff;border:1px solid #E6EAF3;padding:2px 6px}}
.tv-shop>summary{{font-size:15px;font-weight:600;color:{INK}}}
.tv-acc{{background:#FBFCFE;border:1px solid #EDF1F8}}
.tv-acc>summary{{font-size:14px;font-weight:500}}
.tv-adv{{background:#fff;border:1px solid #F0F2F7;margin-bottom:7px}}
.tv-adv>summary{{font-size:13.5px}}
.tv-shop>summary,.tv-acc>summary,.tv-adv>summary{{list-style:none}}
.tv-shop>summary::-webkit-details-marker,
.tv-acc>summary::-webkit-details-marker,
.tv-adv>summary::-webkit-details-marker{{display:none}}
.tv-shop>summary,.tv-acc>summary,.tv-adv>summary{{display:flex;align-items:center;gap:11px;
padding:11px 13px;cursor:pointer;border-radius:11px;user-select:none}}
.tv-shop>summary:hover,.tv-acc>summary:hover,.tv-adv>summary:hover{{background:rgba(201,162,75,.07)}}
.tv-caret{{width:0;height:0;border-left:6px solid {DIMT};border-top:5px solid transparent;
border-bottom:5px solid transparent;transition:transform .18s;flex:none}}
details[open]>summary .tv-caret{{transform:rotate(90deg)}}
.tv-name{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tv-id,.tv-id2{{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:{MUT};
white-space:nowrap;flex:none}}
.tv-dot{{width:8px;height:8px;border-radius:50%;flex:none}}
.tv-dot.on{{background:{GREEN};box-shadow:0 0 0 3px rgba(47,163,107,.16)}}
.tv-dot.off{{background:#CBD2DE;box-shadow:0 0 0 3px rgba(203,210,222,.2)}}
.tv-id2{{display:inline-block;min-width:82px}}
.tv-meta{{font-size:12.5px;color:{DIMT};white-space:nowrap;flex:none}}
.tv-fee{{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;
min-width:96px;text-align:right;flex:none}}
.tv-ag{{font-size:12px;padding:2px 8px;border-radius:6px;background:#F3EAD3;color:#8A6D22;
white-space:nowrap;flex:none}}
.tv-ag-empty{{background:#F2F4F8;color:#A8B2C2}}
.tv-alias{{font-size:11.5px;padding:2px 8px;border-radius:6px;background:#EEF1F7;color:#7A8598;
white-space:nowrap;flex:none}}
.tv-body{{padding:2px 6px 10px 22px}}
.tv-creatives{{padding:2px 4px 8px 18px}}
.tv-cr{{display:flex;align-items:center;gap:12px;padding:7px 9px;border-radius:9px;
border-bottom:1px solid #F5F7FB;font-size:13px}}
.tv-cr:last-child{{border-bottom:none}}
.tv-cr:hover{{background:#FAFBFE}}
.tv-img{{width:38px;height:49px;object-fit:cover;border-radius:6px;background:#F0F2F7;flex:none}}
.tv-noimg{{display:block}}
.tv-cr-name{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tv-cr-num{{width:92px;text-align:right;font-variant-numeric:tabular-nums;color:{MUT};flex:none}}
.tv-cr-cost{{width:86px;text-align:right;font-variant-numeric:tabular-nums;
font-weight:600;color:{GOLD_T};flex:none}}
.tv-empty{{font-size:13px;color:{DIMT};padding:9px 11px}}
.card{{background:#fff;border-radius:14px;padding:16px 18px;box-shadow:0 1px 3px rgba(24,35,55,.05)}}
.card h2{{font-size:18px;font-weight:600;margin-bottom:5px;display:flex;align-items:center;gap:10px}}
.card .hint{{font-size:13px;color:{MUT};margin-bottom:9px}}
.chip{{font-size:11.5px;font-weight:500;padding:3px 9px;border-radius:7px;background:#F3EAD3;color:#8A6D22}}
.chip-b{{background:#E8EEFE;color:#2F55B8}}
.chip-r{{background:#FBE9E7;color:{RED}}}
.waste-row{{display:flex;align-items:center;gap:13px;padding:11px 0;border-bottom:1px solid #F5F7FB}}
.waste-row:last-child{{border-bottom:none}}
.w-img{{width:44px;height:58px;object-fit:cover;border-radius:7px;background:#F0F2F7;flex-shrink:0}}
.w-info{{flex:1;min-width:0}}
.w-name{{font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.w-sub{{font-size:12px;color:{DIMT};margin-top:3px}}
.w-fee{{font-size:16px;font-weight:700;color:{RED};flex-shrink:0}}
.w-go{{font-size:12px;color:{GOLD_T};text-decoration:none;white-space:nowrap}}
.w-go:hover{{text-decoration:underline}}
.ctabs{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
.cbtn{{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid #E8ECF4;background:#FAFBFD;
color:{MUT};cursor:pointer;transition:.15s;flex:none;max-width:150px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-flex;align-items:center;gap:4px}}
.cbtn:hover{{border-color:{GOLD_T};color:{GOLD_T};background:#FFF8EC}}
.cbtn.active{{background:linear-gradient(135deg,{GOLD} 0%,#D4B87A 100%);border-color:transparent;color:#fff;font-weight:500;
box-shadow:0 1px 4px rgba(200,169,106,.25)}}
.cbtn .cbadge{{font-size:10px;background:rgba(0,0,0,.06);padding:1px 6px;border-radius:10px;margin-left:0;opacity:1}}
.cbtn.active .cbadge{{background:rgba(255,255,255,.25);color:#fff}}
.dim-wrap{{align-items:center}}
.dim-search{{flex:1 1 100%;margin:0 0 2px}}
.dim-search input{{width:100%;box-sizing:border-box;font-size:12.5px;padding:5px 10px;border:1px solid #E6EAF3;border-radius:8px;background:#FAFBFD;color:{INK};transition:.15s}}
.dim-search input:focus{{outline:none;border-color:{GOLD_T};background:#fff}}
.dim-search input::placeholder{{color:{DIMT}}}
.cbtn-more{{color:{GOLD_T};border-style:dashed;border-color:#E3D3B0;background:#FFFCF4}}
.cbtn-more:hover{{background:#FFF6E4}}
.dim-rest{{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:5px;margin-top:4px;padding-top:4px;border-top:1px dashed #EAEDF4}}
.cbtn.active .cbadge{{background:rgba(255,255,255,.25);color:#fff}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;font-weight:500;color:#55637A;font-size:13px;padding:11px 13px;
border-bottom:1.5px solid #E6EAF3;white-space:nowrap;position:sticky;top:0;background:#fff;z-index:3}}
.scroll-box{{max-height:760px;overflow:auto;border-radius:10px}}
/* 素材榜：整页滚动 */
.pane[data-card="materials"]>.card{{padding:14px 18px;margin-bottom:0}}
tbody tr:nth-child(even){{background:#FAFBFE}}
td{{padding:11px 13px;border-bottom:1px solid #F2F5FA;vertical-align:middle}}
tbody tr:hover{{background:#F1F5FD}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.hi{{font-weight:600;color:{GREEN}}}
.rank{{color:{DIMT};font-size:13px;text-align:center;width:34px}}
.cr-cell{{position:relative}}
.cr-img{{width:72px;height:96px;object-fit:cover;border-radius:8px;display:block;background:#F0F2F7}}
/* 封面点击率条：叠在封面底边，不额外占行高 */
.cr-wrap{{position:relative;display:block}}
/* 封面行不需要上下留白（内容本身 96px 高），压掉后单屏能多看一行 */
#crTable td{{padding:4px 13px}}
.ctr-bar{{position:absolute;left:0;right:0;bottom:0;height:4px;overflow:hidden;
background:rgba(20,30,50,.3);border-radius:0 0 8px 8px}}
.ctr-bar i{{display:block;height:100%;background:#F0D48A}}
.cr-note{{min-width:0;max-width:540px}}
/* 密度切换（P2-3）：紧凑模式收起指标条与次级行，单屏能多看 3~4 行 */
body.dense .cr-img{{width:40px;height:52px}}
body.dense .cr-cell{{width:46px}}
body.dense .m-sub2,body.dense .ctr-bar{{display:none}}
body.dense .m-title{{font-size:13px;max-width:280px}}
body.dense .m-sub{{font-size:11px}}
body.dense #crTable td{{padding:2px 8px}}
body.dense .num{{font-size:13px}} body.dense .act-tip{{font-size:10.5px}}
body.dense .waste-row{{padding:7px 0}}
.cr-img.noimg{{display:flex;align-items:center;justify-content:center;font-size:11px;color:{DIMT}}}
.cr-img-blank{{display:block;background:#F7F9FC}}
.cr-link{{position:absolute;right:2px;bottom:28px;font-size:12px;color:{GOLD_T};text-decoration:none;
background:rgba(255,255,255,.92);border-radius:5px;padding:1px 4px}}
.cr-img{{cursor:zoom-in}}
.cr-preview{{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
z-index:999;background:#fff;border-radius:16px;padding:9px;pointer-events:none;
box-shadow:0 24px 80px rgba(20,30,50,.44),0 0 0 1px rgba(0,0,0,.06)}}
.cr-preview img{{max-width:min(520px,70vw);max-height:74vh;display:block;border-radius:11px;background:#F0F2F7}}
.cr-pv-meta{{font-size:12px;color:{INK};padding:9px 4px 4px;text-align:center;max-width:520px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cr-cell:hover .cr-preview{{display:block}}

/* 工具条 */
.toolbar{{display:flex;flex-direction:column;gap:6px;margin-bottom:8px}}
.tb-main{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.tb-chips{{display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:wrap}}
.tb-chips .dimseg{{display:flex;gap:2px;background:#F0F2F7;border-radius:8px;padding:2px}}
.tb-chips .dimseg .dbtn{{padding:4px 12px;border-radius:6px}}
.tb-chips .dimchips{{flex:1;min-width:0}}
.tb-chips::-webkit-scrollbar{{display:none}}
.search{{padding:6px 12px;border:1px solid #E6EAF3;border-radius:8px;font-size:13px;
outline:none;color:{INK};width:200px;font-family:inherit}}
.search:focus{{border-color:{GOLD_T}}}
.rbtn{{font-size:12.5px;padding:6px 12px;border-radius:8px;border:1px solid #E6EAF3;
background:#fff;color:{MUT};cursor:pointer;font-family:inherit;transition:.15s;flex:none;white-space:nowrap}}
.rbtn:hover{{border-color:{GOLD_T};color:{GOLD_T}}}
/* 深色头部里的导出按钮 */
.hero .rbtn{{border-color:rgba(255,255,255,.15);background:rgba(255,255,255,.06);
color:rgba(255,255,255,.75)}}
.hero .rbtn:hover{{border-color:rgba(255,255,255,.3);color:#fff}}
.rbtn.busy{{opacity:.55;cursor:wait}}
.rtabs{{display:flex;gap:2px;background:rgba(255,255,255,.07);border-radius:9px;padding:3px;
backdrop-filter:blur(6px)}}
.rtab{{font-size:12.5px;padding:5px 11px;border-radius:6px;color:rgba(255,255,255,.55);
text-decoration:none;cursor:pointer;transition:.18s;white-space:nowrap;font-weight:500}}
.rtab:hover{{color:#fff;background:rgba(255,255,255,.08)}}
.rtab.active{{background:linear-gradient(135deg,{GOLD} 0%,#D9BB7A 100%);color:#1A2740;
font-weight:600;box-shadow:0 2px 8px rgba(200,169,106,.3)}}
th.sortable{{cursor:pointer;user-select:none}}
th.sortable:hover{{color:{GOLD_T}}}
th.sorted-asc::after{{content:" ▲";font-size:10px;color:{GOLD_T}}}
th.sorted-desc::after{{content:" ▼";font-size:10px;color:{GOLD_T}}}
.toast{{position:fixed;left:50%;bottom:34px;transform:translateX(-50%) translateY(70px);
background:{INK};color:#fff;font-size:14px;padding:13px 24px;border-radius:11px;
z-index:1200;opacity:0;transition:.28s;pointer-events:none;max-width:80vw}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
.toast.err{{background:{RED}}}
.toast.ok{{background:{GREEN}}}

/* 授权面板 */
.fab{{position:fixed;right:20px;bottom:20px;z-index:900;background:{GOLD_BG};opacity:.85;color:#fff;border:none;
border-radius:28px;padding:14px 24px;font-size:15px;font-weight:500;cursor:pointer;
box-shadow:0 9px 28px rgba(201,162,75,.44);display:flex;align-items:center;gap:9px;
font-family:inherit;transition:.15s}}
.fab:hover{{background:#6F5719}}
.fab .cnt{{background:rgba(255,255,255,.3);border-radius:12px;padding:1.5px 9px;font-size:13px}}
.fab.ok{{background:{GREEN};box-shadow:0 9px 28px rgba(47,163,107,.4)}}
.modal{{display:none;position:fixed;inset:0;background:rgba(18,26,40,.52);z-index:1000;
align-items:center;justify-content:center;padding:22px}}
.modal.show{{display:flex}}
.mbox{{background:#fff;border-radius:18px;width:min(900px,95vw);max-height:90vh;overflow:auto;
padding:26px 30px}}
.mbox h3{{font-size:19px;font-weight:600;margin-bottom:6px}}
.mbox .msub{{font-size:13.5px;color:{MUT};margin-bottom:20px;line-height:1.75}}
.mbox h4{{font-size:15px;font-weight:600;margin:22px 0 11px;padding-bottom:9px;
border-bottom:1px solid #EDF0F7;display:flex;align-items:center;gap:9px}}
.pend-row{{display:flex;align-items:center;gap:13px;padding:11px 0;border-bottom:1px solid #F5F7FB;font-size:14px}}
.pend-row:last-child{{border-bottom:none}}
.pend-key{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pend-n{{color:{MUT};font-size:12.5px;flex-shrink:0}}
.pend-go{{font-size:12.5px;padding:6px 14px;border-radius:8px;border:1px solid {GOLD_T};color:{GOLD_T};
background:#fff;cursor:pointer;text-decoration:none;white-space:nowrap}}
.pend-go:hover{{background:{GOLD_BG};color:#fff}}
.auth-input{{width:100%;padding:13px 15px;border:1px solid #E6EAF3;border-radius:11px;
font-size:14px;font-family:inherit;outline:none;resize:vertical;min-height:88px;color:{INK}}}
.auth-input:focus{{border-color:{GOLD_T}}}
.auth-actions{{display:flex;gap:11px;align-items:center;margin-top:13px}}
.btn-primary{{background:{GOLD_BG};color:#fff;border:none;border-radius:10px;padding:12px 26px;
font-size:15px;font-weight:500;cursor:pointer;font-family:inherit}}
.btn-primary:hover{{background:#6F5719}}
.btn-primary:disabled{{opacity:.5;cursor:wait}}
.btn-ghost{{background:#fff;color:{MUT};border:1px solid #E6EAF3;border-radius:10px;
padding:12px 21px;font-size:15px;cursor:pointer;font-family:inherit}}
.steps{{font-size:13.5px;color:{MUT};line-height:2.05;background:#FAFBFE;border-radius:11px;
padding:16px 19px;border:1px solid #F0F2F7}}
.steps b{{color:{INK}}}
.res-ok{{background:#E4F4EC;border:1px solid #B7E0CB;border-radius:11px;padding:15px 18px;
font-size:13.5px;line-height:1.85;color:#1D6B48}}
.res-err{{background:#FBE9E7;border:1px solid #F3C6C2;border-radius:11px;padding:15px 18px;
font-size:13.5px;line-height:1.85;color:#9E2F29}}
.acctbl{{width:100%;border-collapse:collapse;font-size:13.5px}}
.acctbl th{{text-align:left;font-weight:500;color:#55637A;font-size:12.5px;padding:9px 8px;
border-bottom:1.5px solid #E6EAF3;white-space:nowrap;background:#fff;position:static}}
.acctbl td{{padding:10px 8px;border-bottom:1px solid #F2F5FA;vertical-align:top}}
.acctbl .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.acc-name{{font-weight:500;min-width:112px}}
.acc-sub{{font-size:11.5px;color:{DIMT};margin-top:3px;max-width:235px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}}
.acc-login{{font-size:12.5px;color:{MUT};line-height:1.7;max-width:210px}}
.row-off{{opacity:.7}}
#accList{{max-height:420px;overflow:auto;border-radius:10px}}
.cr-name{{max-width:360px}}
.cr-name .nm{{font-weight:500;line-height:1.4}}
.cr-name .sub{{font-size:12px;color:{DIMT};margin-top:3px}}
.aname{{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tag{{font-size:12px;padding:3px 9px;border-radius:7px;white-space:nowrap}}
.tag-green{{background:#E4F4EC;color:{GREEN}}}
.tag-gold{{background:#F3EAD3;color:#8A6D22}}
.tag-red{{background:#FBE9E7;color:{RED}}}
.tag-gray{{background:#F2F4F8;color:{DIMT}}}
/* 素材榜数值颜色 */
.fee-bold{{font-weight:600}}
.lc-good{{color:{GREEN};font-weight:600}}
.lc-bad{{color:{RED};font-weight:600}}
.zero{{color:#C0C8D8}}
.ctr-hi{{color:{GOLD_T};font-weight:600}}
/* 行动建议小字 */
.act-tip{{font-size:11px;color:{DIMT};margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bar-row{{display:flex;align-items:center;gap:11px;margin-bottom:9px;font-size:13.5px}}
.bar-label{{width:82px;color:{MUT};text-align:right;flex-shrink:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}}
.bar-track{{flex:1;height:20px;background:#F4F6FA;border-radius:10px;overflow:hidden}}
.bar-fill{{display:block;height:100%;border-radius:10px;transition:width .4s}}
.bar-val{{width:150px;color:{INK};font-size:13px;text-align:right;flex-shrink:0}}
.fn-row{{display:flex;align-items:center;gap:12px;margin-bottom:11px;font-size:13.5px}}
.fn-l{{width:44px;color:{MUT}}}
.fn-track{{flex:1;height:26px;background:#F4F6FA;border-radius:7px;overflow:hidden}}
.fn-fill{{display:block;height:100%;background:linear-gradient(90deg,{GOLD},#E0BE74);border-radius:7px}}
.fn-v{{width:84px;text-align:right;font-weight:600}}
.fn-r{{width:72px;text-align:right;color:{MUT};font-size:12.5px}}
.chart{{width:100%;height:260px;display:block}}
.foot{{margin-top:20px;font-size:13px;color:{MUT};line-height:1.9;
background:#fff;border-radius:16px;padding:19px 23px}}
.foot b{{color:{INK};font-weight:600}}
.foot .srcb{{display:inline-block;background:#F3EAD3;color:#8A6D22;padding:2px 8px;
border-radius:6px;font-size:11.5px;margin-right:5px}}
.empty{{color:{DIMT};font-size:14px;text-align:center;padding:30px 0}}
/* ---------- 语义色图例 / 阈值说明 ---------- */
.legend{{font-size:12px;color:{DIMT};margin-left:auto;letter-spacing:.02em;white-space:nowrap;
display:flex;align-items:center;gap:14px;flex:none}}
.legend b{{font-weight:500;color:{GOLD_T}}}
.legend i{{font-style:normal;color:{GREEN}}}
.legend s{{text-decoration:none;color:{RED}}}
/* 口径说明默认收起，别占表格的高度 */
.thbox{{position:relative;font-size:12.5px}}
.thbox summary{{list-style:none;cursor:pointer;color:{MUT};border:1px solid #E6EAF3;
background:#fff;border-radius:8px;padding:5px 11px;white-space:nowrap;user-select:none;font-size:12.5px}}
.thbox summary::-webkit-details-marker{{display:none}}
.thbox summary:hover{{border-color:{GOLD_T};color:{GOLD_T}}}
.thbox[open] summary{{border-color:{GOLD_BG};color:{GOLD_T};background:#FFF8EC}}
.thresh{{position:absolute;right:0;top:40px;z-index:5;width:440px;max-width:82vw;
font-size:12px;color:{DIMT};padding:11px 14px;background:#fff;border:1px solid #E6EAF3;
border-radius:10px;line-height:1.8;box-shadow:0 6px 20px rgba(24,35,55,.11)}}
/* 当前下钻值：点一下回到全部 */
.dnow{{font-size:12px;padding:3px 12px;border-radius:20px;border:1px solid {GOLD_T};
background:#FFF8EC;color:{GOLD_T};cursor:pointer;font-family:inherit;white-space:nowrap;flex:none;font-weight:600}}
.dnow:hover{{border-color:{GOLD_T};color:{GOLD_T}}}
.dnow.on{{border-color:{GOLD_BG};background:#FFF8EC;color:{GOLD_T};font-weight:600}}

/* ---------- 素材榜：三段式首列 + 指标条 ---------- */
.m-title{{font-weight:600;font-size:14px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;max-width:430px}}
.m-sub{{font-size:11.5px;color:{MUT};margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.m-sub2{{font-size:11px;color:{DIMT};margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.m-sub,.metric-line,.m-sub2{{line-height:1.45}}
.metric-line{{font-size:11.5px;color:{DIMT};margin-top:5px;display:flex;gap:11px;flex-wrap:wrap;
font-variant-numeric:tabular-nums}}
.metric-line b{{color:{INK};font-weight:600}}
.cr-n{{font-size:11.5px;color:{DIMT};white-space:nowrap}}
.act-tip{{font-size:11.5px;color:{DIMT};margin-top:4px;white-space:nowrap}}
#crTable th:nth-child(1),#crTable td:nth-child(1){{width:80px}}
#crTable th:nth-child(3),#crTable td:nth-child(3){{width:88px}}
#crTable th:nth-child(4),#crTable td:nth-child(4){{width:88px}}
#crTable th:nth-child(5),#crTable td:nth-child(5){{width:76px}}
#crTable th:nth-child(6),#crTable td:nth-child(6){{width:68px}}
#crTable th:nth-child(7),#crTable td:nth-child(7){{width:68px}}
#crTable th:nth-child(8),#crTable td:nth-child(8){{width:68px}}
#crTable th:nth-child(9),#crTable td:nth-child(9){{width:68px}}
#crTable th:nth-child(10),#crTable td:nth-child(10){{width:88px}}
#crTable th:nth-child(11),#crTable td:nth-child(11){{width:130px}}

/* ---------- 预警：信息 · 虚线引线 · 数值组（把 800px 的割裂连起来） ---------- */
.waste-row{{display:grid;grid-template-columns:minmax(0,auto) 1fr auto;
gap:0 14px;align-items:center}}
.waste-left{{display:flex;align-items:center;gap:13px;min-width:0}}
.w-info{{max-width:520px}}
.w-lead{{align-self:center;height:0;min-width:20px;border-top:1px dotted #D8DEE9;
margin-top:6px}}
.w-nums{{display:flex;flex-direction:column;align-items:flex-end;gap:2px}}
.w-nums .a{{font-size:16px;font-weight:700;color:{RED};font-variant-numeric:tabular-nums}}
.w-nums .b{{font-size:12px;color:{DIMT};font-variant-numeric:tabular-nums}}

/* ---------- 今日待办 ---------- */
.todo{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}}
.todo-item{{flex:1 1 210px;background:#fff;border-radius:12px;padding:11px 14px;
box-shadow:0 1px 3px rgba(24,35,55,.05);border-left:3px solid {GREEN}}}
.todo-item.warn{{border-left-color:{GOLD_T}}}
.todo-item.crit{{border-left-color:{RED}}}
.todo-item .tl{{font-size:12.5px;color:{MUT}}}
.todo-item .tv2{{font-size:19px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}}
.todo-item .td{{font-size:12px;color:{DIMT};margin-top:3px;line-height:1.6}}

/* ---------- KPI 环比 ---------- */
.kpi-mom{{font-size:12px;margin-top:5px;display:flex;align-items:center;gap:5px;
font-variant-numeric:tabular-nums}}
.kpi-mom.good{{color:{GREEN}}}
.kpi-mom.bad{{color:{RED}}}
.kpi-mom.flat{{color:{DIMT}}}
.kpi-mom span{{color:{DIMT}}}

@media(max-width:1024px){{.kpis{{grid-template-columns:repeat(3,1fr)}}}}
@media(max-width:680px){{.kpis{{grid-template-columns:repeat(2,1fr)}}
.g2,.g2b{{grid-template-columns:1fr}}}}

/* ---------- P0-1 手机档：让内容回到容器内滚动，而不是被 body 裁掉 ---------- */
@media(max-width:768px){{
  html,body{{height:auto}}
  body{{padding:12px 12px 90px;overflow:visible;display:block;font-size:14.5px}}
  .wrap{{height:auto;display:block}}
  .hero{{padding:0 12px}}
  .hero-row1{{padding:8px 0 6px;gap:8px}}
  h1{{font-size:16px}}
  .brand .sub{{font-size:10.5px}}
  .brand .logo{{width:32px;height:32px;font-size:14px}}
  .src{{width:100%;font-size:10.5px}}
  .hero-row2{{grid-template-columns:repeat(3,1fr);padding-bottom:8px}}
  .kpi{{padding:6px 8px 2px;border-right:none;border-bottom:1px solid rgba(255,255,255,.05)}}
  .kpi-val{{font-size:18px}}
  .kpi-label{{font-size:10.5px}}
  .body-pad{{padding:8px 10px 80px}}

  /* 区间按钮：禁止逐字竖排 + 可横滑 + 右缘渐隐提示 */
  .rtabs{{overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch;
    padding-right:30px;-webkit-mask-image:linear-gradient(90deg,#000 88%,transparent);
    mask-image:linear-gradient(90deg,#000 88%,transparent)}}
  .rtabs::-webkit-scrollbar{{display:none}}
  .rtab{{white-space:nowrap;flex:none;min-width:58px;min-height:44px;display:inline-flex;
    align-items:center;justify-content:center}}

  /* 卡片页签：可横滑 + 触控 44px + 渐隐提示 */
  .cardtabs{{overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch;
    -webkit-mask-image:linear-gradient(90deg,#000 86%,transparent);
    mask-image:linear-gradient(90deg,#000 86%,transparent)}}
  .cardtabs::-webkit-scrollbar{{display:none}}
  .cardtab{{white-space:nowrap;flex:none;min-height:44px;display:inline-flex;align-items:center;gap:6px}}
  .legend{{display:none}}

  .kpis{{grid-template-columns:repeat(2,1fr);gap:12px}}
  .kpi{{padding:14px 15px;border-radius:13px}}
  .kpi-val{{font-size:26px}}
  .todo-item{{flex:1 1 100%}}

  /* 素材榜内维度切换：移动端按钮/标签横滑不换行，保证 44px 触控目标 */
  .dbtn{{font-size:13px;padding:9px 14px;min-height:44px;flex:0 0 auto}}
  .dimchips{{flex:1 1 100%;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;
    -webkit-mask-image:linear-gradient(90deg,#000 86%,transparent);mask-image:linear-gradient(90deg,#000 86%,transparent)}}
  .dimchips::-webkit-scrollbar{{display:none}}
  .cbtn{{min-height:44px;flex:0 0 auto}}
  .pane[data-card="materials"].active{{display:block}}
  .pane[data-card="materials"]>.card{{display:block}}
  .pane[data-card="materials"] .scroll-box{{flex:none}}
  .dnow,.thbox summary{{min-height:44px;display:inline-flex;align-items:center}}
  .thresh{{width:auto;left:0;right:auto;max-width:none}}
  .rbtn{{min-height:44px}}
  .cr-link{{padding:5px 7px;bottom:24px}}
  .toolbar{{flex-wrap:wrap;gap:8px}}
  .search{{width:100%;min-height:44px}}

  /* 窄屏整页滚动 */
  .card{{padding:16px 14px;border-radius:14px}}
  .row-charts{{grid-template-columns:1fr}}
  .scroll-box{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  .body-pad{{padding:12px 12px 80px}}
  #crTable{{min-width:900px;table-layout:fixed}}
  #crTable th:nth-child(2),#crTable td:nth-child(2){{width:180px}}
  #crTable th:nth-child(1),#crTable td:nth-child(1){{width:64px}}
  #crTable th:nth-child(3),#crTable td:nth-child(3){{width:72px}}
  #crTable th:nth-child(4),#crTable td:nth-child(4){{width:72px}}
  #crTable th:nth-child(5),#crTable td:nth-child(5){{width:62px}}
  #crTable th:nth-child(6),#crTable td:nth-child(6){{width:58px}}
  #crTable th:nth-child(7),#crTable td:nth-child(7){{width:58px}}
  #crTable th:nth-child(8),#crTable td:nth-child(8){{width:58px}}
  #crTable th:nth-child(9),#crTable td:nth-child(9){{width:58px}}
  #crTable th:nth-child(10),#crTable td:nth-child(10){{width:72px}}
  #crTable th:nth-child(11),#crTable td:nth-child(11){{width:100px}}
  .act-tip{{white-space:normal}}
  .acctbl{{min-width:520px}}
  th,td{{padding:9px 9px}}
  .cr-img{{width:56px;height:74px}}
  .cr-cell{{width:64px}}
  .m-title{{max-width:230px;font-size:13.5px}}
  .cr-preview{{display:none!important}}

  /* 预警行：窄屏改成上下两行，引线隐藏 */
  .waste-row{{grid-template-columns:1fr;gap:8px;padding:12px 0}}
  .w-lead{{display:none}}
  .w-info{{max-width:none}}
  .w-nums{{flex-direction:row;align-items:baseline;justify-content:flex-start;gap:12px;
    border-top:1px solid #EEF1F7;padding-top:8px}}

  /* 归属树：窄屏折行，专业号 ID 隐藏（零信息量） */
  .tv-shop>summary,.tv-acc>summary,.tv-adv>summary{{flex-wrap:wrap;gap:7px;padding:11px 9px}}
  .tv-id{{display:none}}
  .tv-id2{{min-width:0;font-size:11px}}
  .tv-fee{{min-width:74px;font-size:13.5px}}
  .tv-meta{{font-size:11.5px}}

  /* 浮动按钮：窄屏改回常规按钮，别压住表格右下角 */
  .fab{{position:static;margin:18px auto 4px;width:100%;max-width:420px;justify-content:center;
    min-height:48px}}
  .mbox{{width:96vw;padding:20px 16px;max-height:88vh}}
}}
@media(max-width:380px){{
  .hero-row2{{grid-template-columns:1fr 1fr}}
  .kpi-val{{font-size:23px}}
  .kpi-label{{font-size:13px}}
  #crTable{{min-width:860px}}
  .m-title{{max-width:180px}}
}}

/* ---------- P2-4 打印 / 导出：Ctrl+P 直接出完整 PDF ---------- */
@media print{{
  html,body{{height:auto;overflow:visible;background:#fff;padding:0}}
  .cardtabs,.fab,.modal,.toast,.rtabs,.legend,.hero-actions{{display:none!important}}
  .hero{{background:#fff!important;padding:10px 0}}
  .hero::before,.hero::after{{display:none!important}}
  h1{{color:#182337!important}}
  .brand .sub{{color:#55637A!important}}
  .kpi-label{{color:#55637A!important}}
  .kpi-src{{background:#F5F7FB!important;color:#5F6E86!important}}
  .kpi-val{{color:#182337!important}}
  .kpi-val.gold{{color:#A8842F!important}}
  .kpi-val.green{{color:#2FA36B!important}}
  .kpi-val.red{{color:#E0564E!important}}
  .hero-row2{{border-top:1px solid #E6EAF3}}
  .kpi{{border-right:1px solid #EDF1F8}}
  .kpi-mom span{{color:#5F6E86!important}}
  .kpi-mom.flat{{color:#5F6E86!important}}
  .pane{{display:block!important;height:auto!important;overflow:visible!important}}
  .scroll-box{{overflow:visible}}
  #crTable{{min-width:0}}
  .card{{box-shadow:none;border:1px solid #E6EAF3;page-break-inside:avoid;margin-bottom:14px}}
  .cr-preview{{display:none!important}}
}}
</style></head><body><div class="wrap">

<header class="hero">
  <div class="hero-row1">
    <div class="brand"><div class="logo">金</div>
      <div><h1>夏天 · 小红书聚光投放看板</h1>
      <div class="sub">14 城 · 素材获客分析{esc(title_suffix)}</div></div>
    </div>
    <div class="hero-actions">
      {range_html}
      <button class="rbtn" id="btnPrint" title="展开全部卡片，导出 PDF 或打印（截图发群 / 贴进汇报）">⤓ 导出</button>
      <div class="src"><span class="dot"></span>聚光开放平台 · {esc(period_txt)}</div>
    </div>
  </div>
  <div class="hero-row2">{kpi_html}</div>
</header>

<div class="body-pad">

{alert_strip_html}

{todo_html}

<!-- 素材榜提到最前面：首屏直接看素材 -->
<div class="card section-materials">
    <h2>{list_title} <span class="chip">{list_chip}</span></h2>
    <div class="toolbar">
      <div class="tb-main">
        <input class="search" id="crSearch" placeholder="搜索笔记 / 素材 / 账户…">
        <span class="hint" id="crCount" style="margin:0"></span>
        <details class="thbox"><summary>口径</summary>
          <div class="thresh">留资成本 ≤¥{EFF_ML_GOOD} 优秀 ｜ ¥{EFF_ML_GOOD}–{EFF_ML_OK} 合格 ｜
          &gt;¥{EFF_ML_OK} 成本偏高　·　私信成本 ≤¥{EFF_MSG_OK} 有开口　·　
          封面下方细条 = 封面点击率（相对本榜最高值）</div></details>
        <button class="rbtn" id="btnDense" title="切换行高：紧凑能多看几行，舒适适合讲解">≡ 紧凑</button>
        <button class="rbtn" id="btnRefresh" title="从聚光重新拉取数据">↻ 刷新</button>
      </div>
      <div class="tb-chips">
        <span class="dimtip">按维度</span>
        <div class="dimseg">
        <button class="dbtn active" data-dim="city">城市</button>
        <button class="dbtn" data-dim="adv">投放账号</button>
        <button class="dbtn" data-dim="owner">小红书账号</button>
        </div>
        <div class="dimchips" data-dim="city">{dim_tabs['city']}</div>
        <div class="dimchips dim-wrap" data-dim="adv" style="display:none">
          <div class="dim-search"><input type="text" placeholder="搜索投放账号" data-search="adv"></div>
          {dim_tabs['adv']}
          {f'<button class="cbtn cbtn-more" data-more="adv">更多 {dim_more["adv"]} 个 ▾</button><div class="dim-rest" data-rest="adv" hidden>{dim_rests["adv"]}</div>' if dim_more["adv"] > 0 else ''}
        </div>
        <div class="dimchips dim-wrap" data-dim="owner" style="display:none">
          <div class="dim-search"><input type="text" placeholder="搜索小红书账号" data-search="owner"></div>
          {dim_tabs['owner']}
          {f'<button class="cbtn cbtn-more" data-more="owner">更多 {dim_more["owner"]} 个 ▾</button><div class="dim-rest" data-rest="owner" hidden>{dim_rests["owner"]}</div>' if dim_more["owner"] > 0 else ''}
        </div>
        <button class="dnow" id="dimNow" title="点一下清除下钻，回到全部">全部</button>
      </div>
    </div>
    <div class="scroll-box">
    <table id="crTable">
      <thead><tr>{cover_th}<th>{name_th}</th>
      <th class="num">消耗</th><th class="num">展现</th><th class="num">点击</th>
      <th class="num">CTR</th><th class="num">互动</th><th class="num">私信</th>
      <th class="num">留资</th><th class="num">留资成本</th><th>效率</th></tr></thead>
      <tbody>{cr_rows}</tbody>
    </table></div>
  </div>

<div class="row-charts">
  <div class="card">
    <h2>消耗趋势 <span class="chip">账户维度 · 按天</span></h2>
    <div class="hint">区间内每日消耗（账户维度报表天然 1 行/天/户，任意区间都能画）</div>
    {trend_html or '<div class="empty">当前区间不足两天，暂无趋势<br><span style="font-size:13px">选择「近 7 日」「本月」「近半年」等区间即可看到趋势</span></div>'}
  </div>
  <div class="row-charts-side">
    <div class="card">
      <h2>素材转化漏斗 <span class="chip chip-b">全量素材</span></h2>
      <div class="hint">展现 → 点击 → 互动 → 私信 → 留资，右侧为相对展现的转化率</div>
      {funnel_html}
    </div>
    <div class="card">
      <h2>留资成本分布 <span class="chip">已产生留资的素材</span></h2>
      <div class="hint">按单条素材的留资成本分档，绿=健康 金=偏高 红=过贵</div>
      {bucket_html}
    </div>
  </div>
</div>

<details class="section-fold">
  <summary>店铺归属树 <span class="cnt">{tree_adv_n} 个投放户</span></summary>
  <div class="fold-body">
    <div class="card">
    <div class="hint">按账号归属分组：先看每店有几个账号、各带多少投放户，逐层展开到具体素材。
    覆盖 ≥3 城的账号（如总号矩阵）单独归入「跨城矩阵」，避免跨城户被重复计入某个城市。</div>
    {tree_html}
    </div>
  </div>
</details>

<!-- 预警详情（折叠，横幅只显示概要） -->
<details class="section-fold">
  <summary>高耗无转化素材 <span class="cnt">{len(waste_all)} 条 · ¥{sum(r["fee"] for r in waste_all):,.0f}</span></summary>
  <div class="fold-body">
    <div class="card">
    <div class="hint">消耗 ≥ ¥{WASTE_FEE} 却零私信零留资，优先排查定向、落地页与素材钩子。
    注意：这类素材点击率往往不低，"钩子有效但承接断了"是最常见的原因。</div>
    {waste_html}
    </div>
  </div>
</details>

<div class="card section-accounts">
    <h2>投放账户明细 <span class="chip">{len(acct)} 个有数据账户</span></h2>
    <div class="hint">账户 → 消耗 / 私信 / 留资，定位是哪些账户在花预算</div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>账户</th><th>城市</th><th>可用余额</th><th>消耗</th><th>展现</th><th>点击</th>
      <th>素材数</th><th>私信</th><th>留资</th><th>留资成本</th></tr></thead>
      <tbody id="acctBody">{acct_rows}</tbody>
    </table></div>
</div>

<details class="section-fold">
  <summary>口径说明</summary>
  <div class="fold-body">
  <div class="foot">
    <div><span class="srcb">数据源</span>消耗 / 展现 / 点击 / 互动 / 私信 / 留资 来自 <b>小红书聚光开放平台</b>
    （创意维度报表 <code>/jg/data/report/offline/creative</code>），按 <b>创意 ID</b> 聚合；城市按广告主名称推断。</div>
    <div><span class="srcb">口径</span>留资成本 = 消耗 ÷ 留资数；私信成本 = 消耗 ÷ 私信咨询数；
    互动 = 赞 + 藏 + 评 + 转 + 关注；点击率 = 点击 ÷ 展现。</div>
    <div><span class="srcb">⚠ 消耗口径差异</span>聚光各维度的消耗合计<b>并不相等</b>，实测同一户同一天：
    <b>账户 = 计划 &gt; 单元 &gt; 创意</b>（如 9737170：账户 ¥1,519.55 / 单元 ¥971.42 / 创意 ¥896.86）——
    有一部消耗不走单元和创意明细。所以「昨日」视图的消耗是<b>创意口径（偏小）</b>，
    区间视图的消耗是<b>账户口径（更接近真实花费）</b>。跨视图对比大数时请注意这一点。</div>
    <div><span class="srcb">笔记</span>素材榜<b>按 note_id 聚合</b>（同一篇笔记投在多个单元/创意时合并成一行），
    首列「笔记标题 / 城市·投放账号·小红书账号 / 内部创意名」三段式。标题与首图来自<b>笔记维度报表</b>
    <code>/jg/data/report/offline/note</code>；未拉到时退化为投放内部创意名，不影响其他数据。
    原文直达用<b>站内搜索标题</b>（<code>explore/&lt;note_id&gt;</code> 需登录态 xsec_token，接口不提供）。
    封面底边的细条 = <b>封面点击率</b>（相对本榜最高值，用来横向比哪张封面抓人）。</div>
    <div><span class="srcb">环比</span>六项 KPI 各带一行上一周期对比。为保证可比，
    <b>环比两边同口径</b>：昨日/今日 = 相邻两天日快照；区间 = 两段等长（或上月同期/上季度）的<b>账户日粒度</b>。
    颜色按“好坏”而非涨跌 —— <b>留资成本下降显示为绿色</b>。</div>
    <div><span class="srcb">归属</span>「店铺归属树」按<b>账号归属</b>分组（一个投放户只算在它所属的账号下）；
    覆盖多城的账号单独归入「跨城矩阵」。专业号原始 ID 收进悬停提示，不再占版面。</div>
    <div><span class="srcb">区间</span>「昨日」用本地日快照（含素材级明细）；其余区间由<b>账户维度日明细</b>在本地聚合
    （实测算一次覆盖近一年，之后切换区间无需再请求聚光接口，秒出）。
    区间 &gt; 31 天时列表降级为<b>账户粒度</b> —— 聚光创意维度近一年单户 16,194 行，全量拉取不可行。
    {f'本区间覆盖 <b>{days_cov} 天</b>。' if days_cov else ''}</div>
    <div><span class="srcb">待接</span>真实客资与成交数据需对接 <b>金燕子 CRM</b>，
    接入后可将「素材 → 私信 → 留资 → 客资 → 成交」串成完整获客链路。</div>
    <div><span class="srcb">生成</span>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    {esc(source_note)}</div>
  </div>
  </div>
</details>

</div><!-- /body-pad -->

</div>

<button class="fab" id="fabAuth" title="授权管理">＋ 授权管理 <span class="cnt" id="fabCnt">–</span></button>
<div class="toast" id="toast"></div>

<div class="modal" id="modalAuth">
  <div class="mbox">
    <h3>授权管理</h3>
    <div class="msub">小红书聚光的 auth_code 是<b>一次性</b>的，有效期只有几分钟，所以拿到后要立刻提交。<br>
    流程：点「打开授权页」→ 登录并确认授权 → 复制跳转后的地址栏 → 粘贴到下方提交。换 token、验证、入库全自动。</div>

    <h4>账号授权总览 <span class="chip" id="accSum">读取中…</span></h4>
    <div class="hint" style="margin:-4px 0 7px">同一城市有多个小红书账号时，用「·后缀」区分（主号 / 婚纱照 / 直营店 / 婚前影像…）。<br>
    每个主体都是一个<b>独立的小红书账号</b> —— 用 A 号授权不会顺带覆盖 B 号，必须各自登录授权。</div>
    <div id="accList"><div class="hint">正在读取账号库…</div></div>

    <h4>提交授权</h4>
    <div class="steps">
      <b>1</b>　点下方「打开授权页」<br>
      <b>2</b>　用对应的小红书账号登录（短信验证码）→ 点「确认授权」<br>
      <b>3</b>　页面跳到 httpbin.org 后，<b>立刻 Ctrl+A 全选地址栏并复制</b><br>
      <b>4</b>　粘贴到下面，点「提交授权」
    </div>
    <div class="auth-actions">
      <a class="pend-go" id="authLink" href="#" target="_blank" rel="noopener">打开授权页 ↗</a>
      <button class="rbtn" id="btnCopyUrl">复制授权链接</button>
    </div>
    <textarea class="auth-input" id="authCode" placeholder="把整条回调地址粘进来即可（形如 https://httpbin.org/get?auth_code=xxx&state=abcd），也可以只粘 auth_code"></textarea>
    <div class="auth-actions">
      <button class="btn-primary" id="btnSubmitAuth">提交授权</button>
      <button class="btn-ghost" id="btnCloseAuth">关闭</button>
    </div>
    <div id="authResult"></div>
  </div>
</div>

<script>
/* 单页布局：不再需要tab切换 */

var curDim='city', curKey='all';
var dimNames={{city:'城市',adv:'投放账号',owner:'小红书账号'}};
function applyFilter(){{
  var el=document.getElementById('crSearch');
  var q=el?el.value.trim().toLowerCase():'';
  var rows=document.querySelectorAll('#crTable tbody tr');
  var shown=0;
  rows.forEach(function(tr){{
    var okDim=(curKey==='all'||(tr.dataset[curDim]||'')===curKey);
    var okQ=(!q||tr.textContent.toLowerCase().indexOf(q)>=0);
    var show=okDim&&okQ;
    tr.style.display=show?'':'none';
    if(show)shown++;
  }});
  var c=document.getElementById('crCount');
  if(c)c.textContent='显示 '+shown+' / '+rows.length+' 条';
}}
/* 维度切换：切维度时，切换按钮高亮 + 对应下钻标签组显隐 */
function syncDim(d){{
  document.querySelectorAll('.dbtn').forEach(function(x){{
    x.classList.toggle('active', x.dataset.dim===d);
  }});
  document.querySelectorAll('.dimchips').forEach(function(x){{
    x.style.display=(x.dataset.dim===d)?'flex':'none';
  }});
}}
/* 更多/收起折叠的账号 chips */
document.querySelectorAll('.cbtn-more').forEach(function(b){{
  b.addEventListener('click',function(){{
    var d=this.dataset.more;
    var rest=document.querySelector('.dim-rest[data-rest="'+d+'"]');
    if(!rest) return;
    var hid=rest.hidden;
    rest.hidden=!hid;
    this.textContent=hid?('收起 ▴'):('更多 '+rest.querySelectorAll('.cbtn').length+' 个 ▾');
  }});
}});
/* 账号搜索过滤 */
document.querySelectorAll('.dim-search input').forEach(function(inp){{
  inp.addEventListener('input',function(){{
    var d=this.dataset.search;
    var set=document.querySelector('.dimchips[data-dim="'+d+'"]');
    if(!set) return;
    var kw=(this.value||'').trim();
    var rest=set.querySelector('.dim-rest[data-rest="'+d+'"]');
    if(kw && rest && rest.hidden){{ rest.hidden=false; var mb=set.querySelector('.cbtn-more'); if(mb) mb.textContent='收起 ▴'; }}
    set.querySelectorAll('.cbtn').forEach(function(x){{
      if(x.classList.contains('cbtn-more')) return;
      var show=!kw || (x.dataset.key||'').indexOf(kw)>=0 || (x.title||'').indexOf(kw)>=0;
      x.style.display=show?'':'none';
    }});
  }});
}});
/* 选中同步：下钻标签高亮 + 工具栏「当前下钻值」按钮 */
function syncChip(d,k){{
  var set=document.querySelector('.dimchips[data-dim="'+d+'"]');
  if(set) set.querySelectorAll('.cbtn').forEach(function(x){{
    x.classList.toggle('active', (x.dataset.key||'all')===k);
  }});
  var dn=document.getElementById('dimNow');
  if(!dn) return;
  if(k && k!=='all'){{
    dn.textContent=(dimNames[d]||d)+' · '+k+' ✕';
    dn.classList.add('on');
  }}else{{
    dn.textContent='全部'; dn.classList.remove('on');
  }}
}}
document.querySelectorAll('.dbtn').forEach(function(b){{
  b.addEventListener('click',function(){{
    curDim=this.dataset.dim; curKey='all';
    syncDim(curDim); syncChip(curDim,'all');
    applyFilter();
  }});
}});
/* 下钻标签：点一下按该维度筛选，再点同一个 = 回到全部 */
document.querySelectorAll('.dimchips .cbtn').forEach(function(b){{
  b.addEventListener('click',function(){{
    var d=this.dataset.dim, k=this.dataset.key||'all';
    curDim=d; curKey=(curKey===k)?'all':k;
    syncChip(d,curKey); applyFilter();
  }});
}});
var dnBtn=document.getElementById('dimNow');
if(dnBtn) dnBtn.addEventListener('click',function(){{
  curKey='all'; syncChip(curDim,'all'); applyFilter();
}});
syncDim(curDim); syncChip(curDim,curKey);

// ================= 新增交互 =================
var LIVE=(location.protocol==='http:'||location.protocol==='https:');
function toast(msg,kind){{
  var t=document.getElementById('toast'); if(!t)return;
  t.textContent=msg;
  t.className='toast show'+(kind?' '+kind:'');
  clearTimeout(window.__tt);
  window.__tt=setTimeout(function(){{t.className='toast';}},3400);
}}
// 等后台刷新真正跑完再重载，否则会看到旧数字（刷新通常要几秒）
function waitForRefresh(tries){{
  if(tries<=0){{ location.href='?range=latest'; return; }}
  fetch('/api/status').then(function(r){{return r.json()}}).then(function(s){{
    if(s.refreshing){{
      setTimeout(function(){{waitForRefresh(tries-1)}},1600);
    }}else{{
      setTimeout(function(){{location.href='?range=latest';}},600);
    }}
  }}).catch(function(){{ setTimeout(function(){{location.href='?range=latest';}},1500); }});
}}
if(!LIVE){{
  var c0=document.getElementById('crCount');
  if(c0)c0.textContent='离线查看模式 · 授权与刷新需通过服务地址访问';
  var rt=document.querySelector('.rtabs'); if(rt)rt.style.opacity='0.5';
}}

// 搜索
var se=document.getElementById('crSearch');
if(se)se.addEventListener('input',applyFilter);
applyFilter();

// 密度切换（紧凑 / 舒适）
var bd=document.getElementById('btnDense');
function setDense(d){{
  document.body.classList.toggle('dense',d);
  if(bd)bd.textContent=d?'≡ 舒适':'≡ 紧凑';
  try{{ localStorage.setItem('jg_dense',d?'1':'0'); }}catch(e){{}}
}}
if(bd)bd.addEventListener('click',function(){{ setDense(!document.body.classList.contains('dense')); }});
(function(){{
  // 默认紧凑：扫榜单屏可见行数最多（应对「滚动只看一两行」的痛点）；
  // 点 ≡ 舒适 可切回大封面模式用于讲解/截图。仅当用户显式存过「舒适」才用舒适。
  try{{ setDense(localStorage.getItem('jg_dense')!=='0'); }}catch(e){{}}
}})();

// 表头排序（跳过 封面 / 笔记 两列）
var ths=document.querySelectorAll('#crTable thead th');
ths.forEach(function(th,idx){{
  if(idx<2)return;
  th.classList.add('sortable');
  th.addEventListener('click',function(){{
    var tb=document.querySelector('#crTable tbody');
    var rows=Array.prototype.slice.call(tb.querySelectorAll('tr'));
    var desc=!th.classList.contains('sorted-desc');
    ths.forEach(function(x){{x.classList.remove('sorted-asc','sorted-desc')}});
    th.classList.add(desc?'sorted-desc':'sorted-asc');
    rows.sort(function(a,b){{
      var av=parseFloat((a.cells[idx].textContent||'').replace(/[¥,%,\\s]/g,''))||0;
      var bv=parseFloat((b.cells[idx].textContent||'').replace(/[¥,%,\\s]/g,''))||0;
      return desc?bv-av:av-bv;
    }});
    rows.forEach(function(r){{tb.appendChild(r)}});
    applyFilter();
  }});
}});

// 导出 / 打印（@media print 会展开全部卡片、去掉交互件）
var bp=document.getElementById('btnPrint');
if(bp)bp.addEventListener('click',function(){{
  toast('正在打开打印/导出，选择「另存为 PDF」即可');
  setTimeout(function(){{window.print();}},260);
}});

// 刷新数据
var br=document.getElementById('btnRefresh');
if(br)br.addEventListener('click',function(){{
  var b=this; b.disabled=true; b.textContent='↻ 拉取中…';
  toast('正在从聚光拉取最新数据，请稍候…');
  fetch('/api/refresh').then(function(r){{return r.json()}}).then(function(d){{
    if(!d.ok){{ toast('刷新失败：'+(d.error||'未知错误'),'err'); return; }}
    if(d.queued){{ toast('已有刷新在进行，排队等待…'); waitForRefresh(40); }}
    else{{ toast('数据已刷新，正在重载页面','ok'); setTimeout(function(){{location.reload()}},1100); }}
  }}).catch(function(e){{ toast('刷新失败：'+e,'err'); }})
  .then(function(){{ b.disabled=false; b.textContent='↻ 刷新数据'; }});
}});

// 授权面板
var modal=document.getElementById('modalAuth');
var fb=document.getElementById('fabAuth');
function openModal(){{ if(modal)modal.classList.add('show'); loadAccounts(); }}
function closeModal(){{ if(modal)modal.classList.remove('show'); }}
if(fb)fb.addEventListener('click',function(){{
  if(!LIVE){{ toast('离线查看模式：授权功能请通过服务地址访问','err'); return; }}
  openModal();
}});
var bc=document.getElementById('btnCloseAuth');
if(bc)bc.addEventListener('click',closeModal);
if(modal)modal.addEventListener('click',function(e){{ if(e.target===modal)closeModal(); }});

function loadAccounts(){{
  // 授权链接从 pending 接口取
  fetch('/api/pending').then(function(r){{return r.json()}}).then(function(d){{
    var link=document.getElementById('authLink');
    if(link)link.href=d.auth_url||'#';
    var n=(d.pending||[]).length;
    var cnt=document.getElementById('fabCnt');
    if(cnt)cnt.textContent=n;
    if(fb&&n===0)fb.classList.add('ok');
  }}).catch(function(){{}});
  // 账号授权总览
  fetch('/api/accounts').then(function(r){{return r.json()}}).then(function(d){{
    var sum=document.getElementById('accSum');
    if(sum)sum.textContent='已授权 '+d.authorized+' / 共 '+d.total+' 个主体';
    var el=document.getElementById('accList');
    if(!el)return;
    var h='<table class="acctbl"><thead><tr><th>小红书账号</th><th>状态</th>'
        +'<th>投放户</th><th>剩余</th><th>登录手机号</th></tr></thead><tbody>';
    (d.accounts||[]).forEach(function(a){{
      var rem=(a.remainingHours!=null)?(a.remainingHours+'h'):'—';
      var login='—';
      if(a.login&&a.login.length){{
        login=a.login.map(function(x){{
          return x.name+'<br><span style="color:{DIMT}">'+x.account+'</span>';
        }}).join('<br>');
      }}
      if(a.loginNote&&!a.authorized){{
        login='<span style="color:#C08A2E">'+a.loginNote+'</span>';
      }}
      var names=(a.advertiserNames||[]).slice(0,8).join('、');
      h+='<tr'+(a.authorized?'':' class="row-off"')+'>'
       +'<td class="acc-name" title="'+a.key+'">'+a.label
       +'<div class="acc-sub" title="'+names+'">'+(names||'（无投放户）')+'</div></td>'
       +'<td><span class="tag '+(a.authorized?'tag-green':'tag-red')+'">'+a.statusText+'</span></td>'
       +'<td class="num">'+a.advertisers+'</td>'
       +'<td class="num">'+rem+'</td>'
       +'<td class="acc-login">'+login+'</td></tr>';
    }});
    h+='</tbody></table>';
    el.innerHTML=h;
  }}).catch(function(e){{
    var el=document.getElementById('accList');
    if(el)el.innerHTML='<div class="hint">读取失败：'+e+'</div>';
  }});
}}

var bs=document.getElementById('btnSubmitAuth');
if(bs)bs.addEventListener('click',function(){{
  var ta=document.getElementById('authCode');
  var code=ta?ta.value.trim():'';
  if(!code){{ toast('请先粘贴回调地址或 auth_code','err'); return; }}
  var b=this; b.disabled=true; b.textContent='提交中…';
  var box=document.getElementById('authResult');
  if(box)box.innerHTML='';
  fetch('/api/auth',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{code:code}})}})
  .then(function(r){{return r.json()}})
  .then(function(d){{
    if(!box)return;
    if(d.ok){{
      var v=d.verified||{{}};
      box.innerHTML='<div class="res-ok" style="margin-top:12px"><b>✓ '+d.msg+'</b><br>'
        +'主体：'+d.corp+'<br>账号库条目：'+d.key+'（'+d.match+'）<br>'
        +'投放户 '+(d.advertisers||[]).length+' 个<br>'
        +'数据验证：'+(v.ok?('通过，近 3 日取到 '+v.creatives+' 条素材')
            :('未通过 '+(v.errors||[]).join('; ')))
        +'<br><span style="opacity:.75">正在等待后台刷新完成，完成后页面会自动重载</span></div>';
      toast('授权成功，等待数据刷新…','ok');
      if(ta)ta.value='';
      waitForRefresh(25);
    }}else{{
      box.innerHTML='<div class="res-err" style="margin-top:12px"><b>✗ 授权失败</b><br>'+d.msg+'</div>';
      toast('授权失败','err');
    }}
  }}).catch(function(e){{
    if(box)box.innerHTML='<div class="res-err" style="margin-top:12px"><b>✗ 请求失败</b><br>'+e+'</div>';
  }}).then(function(){{ b.disabled=false; b.textContent='提交授权'; }});
}});

var bu=document.getElementById('btnCopyUrl');
if(bu)bu.addEventListener('click',function(){{
  var l=document.getElementById('authLink');
  var url=l?l.href:'';
  if(navigator.clipboard&&url){{navigator.clipboard.writeText(url).then(
    function(){{toast('授权链接已复制','ok')}},
    function(){{toast('复制失败，请手动复制','err')}});}}
  else toast('请手动复制链接','err');
}});
(function(){{
  var tbody=document.getElementById('acctBody');
  if(!tbody) return;
  var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var SIZE=20, page=1, pages=Math.max(1,Math.ceil(rows.length/SIZE));
  var wrap=document.createElement('div');
  wrap.className='pager'; wrap.id='acctPagerS';
  tbody.parentNode.parentNode.appendChild(wrap);
  function render(){{
    rows.forEach(function(r,i){{ r.style.display=(i>=(page-1)*SIZE&&i<page*SIZE)?'':'none'; }});
    var s=(page-1)*SIZE+1, e=Math.min(page*SIZE,rows.length);
    var nums='';
    for(var i=1;i<=pages;i++) nums+='<button type="button" class="pg-num'+(i===page?' cur':'')+'" data-p="'+i+'">'+i+'</button>';
    wrap.innerHTML='<button type="button" class="pg-btn" data-p="'+(page>1?page-1:1)+'"'+(page<=1?' disabled':'')+'>‹ 上一页</button>'
      +'<span class="pg-info">第 '+s+'–'+e+' 条 · 共 '+rows.length+' 条</span>'+nums
      +'<button type="button" class="pg-btn" data-p="'+(page<pages?page+1:pages)+'"'+(page>=pages?' disabled':'')+'>下一页 ›</button>';
    wrap.querySelectorAll('.pg-num,.pg-btn').forEach(function(b){{
      b.onclick=function(){{ var p=parseInt(this.dataset.p,10); if(!p||p===page) return; page=p; render(); }};
    }});
  }}
  render();
}})();
</script>
</body></html>"""
    return html


def _parse_args(argv):
    """解析位置参数，跳过 --out <file> 这类带值的开关"""
    pos, i = [], 0
    while i < len(argv):
        if argv[i] == "--out":
            i += 2
            continue
        if argv[i].startswith("--"):
            i += 1
            continue
        pos.append(argv[i])
        i += 1
    return pos


def main():
    args = _parse_args(sys.argv[1:])
    if len(args) >= 2:
        start, end = args[0], args[1]
        days = sorted({s for s in list_snapshots()
                       if len(s) == 10 and start <= s <= end})
        snap = merge_days(days)
        suffix = f" · {start} ~ {end}"
    elif len(args) == 1:
        snap = load_day(args[0])
        suffix = ""
    else:
        lb = latest_daily_snapshot()
        if not lb:
            print("没有可用快照"); sys.exit(1)
        snap = load_day(lb)
        suffix = ""

    html = build_html(snap, title_suffix=suffix)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_name = "小红书投放看板.html"
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            out_name = sys.argv[i + 1]
    out = os.path.join(REPORTS_DIR, out_name)
    # 静态版与根目录 assets/covers 共用一套封面：HTML 位于 reports/ 下，
    # 相对路径统一改为 ../assets/（删除 reports/assets 副本后仍可离线展示）
    html = html.replace('src="assets/', 'src="../assets/')
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    s = snap.get("summary", {})
    print(f"✅ 看板已生成: {out}")
    print(f"   期间 {snap.get('date')} | {s.get('advertiser_count',0)} 户 | "
          f"¥{s.get('total_fee',0):,.0f} | 私信 {s.get('total_msg',0)} | 留资 {s.get('total_msg_leads',0)}")


# ===================================================================
# v2 模板渲染（浅色精致版）—— 旧 build_html 保留，出问题可一键切回
# 模板：看板模板_v2.html（由 reports/看板改版方案_v1.html 占位符化而来）
# ===================================================================

V2_TEMPLATE_FILE = os.path.join(BASE_DIR, "看板模板_v2.html")

# 留资成本分布 5 档（与 v1 视觉稿一致）
_BUCKET_EDGES = [("≤100", 0, 100), ("100–200", 100, 200), ("200–400", 200, 400),
                 ("400–800", 400, 800), (">800", 800, None)]


def _j(o):
    """JSON 序列化，并把 </ 转义成 <\\/ ，防止笔记标题里的 </script> 提前截断模板"""
    return json.dumps(o, ensure_ascii=False).replace("</", "<\\/")


def _v2_mats(active, note_meta):
    """素材榜：按笔记聚合 + 效率标签 + 笔记标题/首图覆盖"""
    out = []
    for r in aggregate_notes(active):
        tag, tcls = eff_tag(r)
        nid = r.get("note_id") or ""
        meta = note_meta.get(nid) or {}
        note_title = (meta.get("title") or "").strip()
        if note_title:
            title, title_src = note_title, "note"
        else:
            title, title_src = (r.get("name") or "(未命名素材)").strip(), "creative"
        img = meta.get("image") or r.get("img") or ""
        out.append({
            "title": title, "title_src": title_src, "img": img, "city": r.get("city", "其他"),
            "adv": r.get("adv", ""), "owner": r.get("owner", ""),
            "note_id": nid,
            "fee": round(_num(r.get("fee")), 2),
            "imp": int(_num(r.get("imp"))), "clk": int(_num(r.get("clk"))),
            "ctr": round(_num(r.get("ctr")), 2),
            "msg": int(_num(r.get("msg"))), "ml": int(_num(r.get("ml"))),
            "mlc": round(_num(r.get("ml_cost")), 2) if r.get("ml_cost") is not None else None,
            "n": int(r.get("_n") or 1),
            "tag": tag, "tcls": tcls, "act": eff_action(tag),
        })
    return out


def _v2_accts(snapshot):
    """投放账户明细：按投放户（advertiser）聚合，只留有消耗的户"""
    out = []
    for aid, adv in (snapshot.get("advertisers") or {}).items():
        crows = adv.get("creatives") or []
        if not crows:
            continue
        fee = sum(_num(c.get("fee")) for c in crows)
        if fee <= 0:
            continue
        msg = int(sum(_num(c.get("message_consult")) for c in crows))
        ml = int(sum(_num(c.get("msg_leads_num")) for c in crows))
        imp = int(sum(_num(c.get("impression")) for c in crows))
        clk = int(sum(_num(c.get("click")) for c in crows))
        out.append({
            "name": adv.get("name") or str(aid),
            "city": adv.get("city") or "其他",
            "fee": round(fee, 2), "msg": msg, "ml": ml,
            "imp": imp, "clk": clk, "n": len(crows),
            "mlc": round(fee / ml, 2) if ml else None,
            "balance": adv.get("balance"),
            # 顶层数值字段，供 makeGrid 按「可用余额」排序与合计（render 仍用 a.balance.available_balance）
            "bal_avail": round((adv.get("balance") or {}).get("available_balance") or 0, 2),
        })
    out.sort(key=lambda a: -a["fee"])
    return out


def _v2_waste(mats):
    """高耗无转化：素材榜里 消耗≥WASTE_FEE 且零私信零留资"""
    return [m for m in mats
            if m["fee"] >= WASTE_FEE and m["msg"] == 0 and m["ml"] == 0]


def _v2_summary(active):
    """KPI 六卡 + 漏斗：全部创意口径，与素材榜合计自洽"""
    fee = sum(r["fee"] for r in active)
    imp = sum(r["imp"] for r in active)
    clk = sum(r["clk"] for r in active)
    inter = sum(r["inter"] for r in active)
    msg = sum(r["msg"] for r in active)
    ml = sum(r["ml"] for r in active)
    return {
        "fee": round(fee, 2), "imp": int(imp), "clk": int(clk), "int": int(inter),
        "msg": int(msg), "ml": int(ml),
        "ctr": round(clk / imp * 100, 2) if imp else None,
        "mlc": round(fee / ml, 2) if ml else None,
        "cpc": round(fee / clk, 2) if clk else None,
    }


def _v2_trend(days=7):
    """近 N 日账户口径每日消耗（来自 data/account_daily.json），取最近有数据的 N 天"""
    try:
        from core import collector
        d = collector.load_account_daily()
    except Exception:
        d = None
    if not d:
        return []
    daily = defaultdict(float)
    for a in (d.get("advertisers") or {}).values():
        for dt, v in (a.get("days") or {}).items():
            daily[dt] += _num(v.get("fee"))
    dates = sorted(daily)[-days:]
    return [{"d": dt[5:], "v": round(daily[dt], 0)} for dt in dates]


def _v2_bucket(mats):
    """留资成本分布 5 档：对有留资的素材按留资成本分档"""
    out = []
    for label, lo, hi in _BUCKET_EDGES:
        n = fee = 0
        for m in mats:
            c = m.get("mlc")
            if c is None:
                continue
            if lo is not None and c <= lo:
                continue
            if hi is not None and c >= hi:
                continue
            n += 1
            fee += m["fee"]
        out.append({"label": label, "n": n, "fee": round(fee, 2)})
    return out


def _v2_auth():
    """授权统计：{pending, total, rows:[{name, st, ok, at_exp, rt_exp}]}；拿不到就返回 None"""
    try:
        from core import tokenhub
        accounts = tokenhub.load_accounts()
    except Exception:
        return None
    rows, bad, tok_bad = [], 0, 0
    try:
        from core import accounts_meta as _am
    except Exception:
        _am = None
    for k, rec in (accounts or {}).items():
        # 显示「专业号名称」：取账号名下第一个广告主名（清洗前缀即专业号名），
        # 优于 resolve_label 的纯地域名（主体键多为同一营业执照名，无法区分账号）
        name = ""
        for _a in (rec.get("advertisers") or []):
            _an = (_a.get("advertiser_name") or "").strip()
            if _an:
                name = clean_adv_name(_an)
                break
        if not name and _am is not None:
            try:
                name = _am.resolve_label(k, rec)
            except Exception:
                name = ""
        if not name:
            name = (rec.get("name") or rec.get("corporation_name") or k).strip()
        try:
            st = tokenhub.classify(rec)
        except Exception:
            st = "expired"
        ok = st in ("valid", "expiring", "refreshable")
        if not ok:
            bad += 1
        # token 本身是坏的（空/占位符）→ 名下投放户**永远**拉不到数据，
        # 必须显式告诉用户去重新授权，否则数据永远拉不齐。
        _tok = (rec.get("access_token") or "").strip()
        tok_ok = len(_tok) >= 20
        if not tok_ok:
            tok_bad += 1
        now = time.time()
        created = rec.get("created_at") or 0
        at_exp = (created + (rec.get("access_token_expires_in") or 86399)) if created else 0
        rt_exp = (created + (rec.get("refresh_token_expires_in") or 2591999)) if created else 0
        rows.append({
            "name": name, "st": st, "ok": ok, "tok_ok": tok_ok,
            "n_adv": len(rec.get("advertisers") or []),
            "at_exp": time.strftime("%m-%d %H:%M", time.localtime(at_exp)) if at_exp else "—",
            "rt_exp": time.strftime("%m-%d %H:%M", time.localtime(rt_exp)) if rt_exp else "—",
            "usr": rec.get("user_id") or "",
        })
    if not rows:
        return None
    # token 失效的排最前，用户打开授权管理一眼就能看到要处理谁
    rows.sort(key=lambda r: (r["tok_ok"], r["ok"], -r["n_adv"]))
    return {"pending": bad, "total": len(rows), "rows": rows,
            "tok_bad": tok_bad,
            "adv_lost": sum(r["n_adv"] for r in rows if not r["tok_ok"])}


def _auth_url():
    """生成授权页地址（供前端「打开授权页」跳转 / 复制）"""
    try:
        from core import authflow
        return authflow.auth_url()
    except Exception:
        return "#"


_AUTH_ST_TXT = {
    "valid": "已授权", "expiring": "即将过期", "refreshable": "可续期",
    "placeholder": "占位符污染", "expired": "已过期", "pending": "待授权",
}
_AUTH_ST_CLS = {
    "valid": "ok", "expiring": "warn", "refreshable": "ok",
    "placeholder": "bad", "expired": "bad", "pending": "bad",
}


def _v2_auth_html(auth):
    if not auth:
        return '<div class="acc-row"><span class="nm">— 暂无账号数据</span></div>'
    parts = []
    if auth.get("tok_bad"):
        parts.append(
            '<div class="acc-row" style="background:#FDF0EF;border-color:#F3C9C5">'
            '<span class="nm" style="color:#C2372E">⚠ %d 个主体 token 已失效（%d 个投放户拉不到数据）</span>'
            '<span class="st bad">需重新授权</span>'
            '<div class="acc-exp" style="color:#C2372E">'
            '这些主体名下的投放户在<b>任何</b>时间维度都不会出现。'
            '请用对应小红书账号重新授权一次，授权成功后会立即拉齐数据。'
            '</div></div>'
            % (auth["tok_bad"], auth.get("adv_lost") or 0))
    for r in auth["rows"]:
        parts.append(
            '<div class="acc-row">'
            '<span class="nm" title="user_id: %s">%s</span>'
            '<span class="st %s">%s</span>'
            '<div class="acc-exp"><span>访问 token 到期 %s</span>'
            '<span>refresh 到期 %s</span></div>'
            '</div>'
            % (esc(r["usr"] or "—"), esc(r["name"]),
               _AUTH_ST_CLS.get(r["st"], "bad"), _AUTH_ST_TXT.get(r["st"], r["st"]),
               esc(r["at_exp"]), esc(r["rt_exp"])))
    parts.append('<div class="acc-row" style="opacity:.55"><span class="nm">…</span>'
                 '<span class="st">共 %d 主体 · %d 待处理</span></div>'
                 % (auth["total"], auth["pending"]))
    return "".join(parts)


def _v2_range_html(ranges, current_range, custom_range=None):
    """区间按钮（不带外层 .rtabs 容器，模板已有 <div class="rtabs nav-rtabs">）"""
    parts = []
    for k, n in (ranges or []):
        active = " active" if k == current_range else ""
        href = f"?range={k}"
        if k == "custom" and custom_range:
            s, e = custom_range
            href = f"?range=custom&start={s}&end={e}"
        parts.append(f'<a class="rtab{active}" href="{href}">{esc(n)}</a>')
    return "".join(parts)


def _v2_footnote(snap, mats_total, trend):
    s = snap.get("summary", {}) or {}
    lines = [
        "数据来源：聚光开放平台离线报表（本地快照缓存）",
        "口径说明：KPI/素材榜/漏斗为创意口径（实测各维度消耗合计不相等，创意口径约占账户口径 85%）；"
        "消耗趋势为账户口径（比创意口径略高，属已知差异）",
        "「可用余额」为实时余额接口缓存（点「⇆ 刷新余额」更新），接口返回单位为「分」，已 ÷100 换算为「元」展示",
        f"快照期间 {snap.get('date') or '—'} · {s.get('advertiser_count', 0)} 户 · "
        f"素材 {mats_total} 条" + (f" · 趋势 {len(trend)} 日" if trend else ""),
    ]
    return " · ".join(lines)


def build_html_v2(snapshot, ranges=None, current_range="latest",
                  title_suffix="", source_note="", mom=None, last_refresh=None,
                  start=None, end=None):
    """浅色精致版渲染：读 v2 模板，注入 14 个占位符。
    模板缺失时回退旧版 build_html（服务不挂、可发现）。
    """
    try:
        with open(V2_TEMPLATE_FILE, encoding="utf-8") as f:
            tpl = f.read()
    except Exception:
        print("⚠ 未找到 v2 模板，回退旧版 build_html")
        return build_html(snapshot, title_suffix=title_suffix,
                          source_note=source_note, ranges=ranges,
                          current_range=current_range, mom=mom)

    uid_map, inferred = build_owner_map(snapshot)
    # 投放户余额：把缓存的实时余额挂到 snapshot，供账户明细表「可用余额」列展示
    try:
        from core import balances
        balances.attach(snapshot)
    except Exception:
        pass
    rows = flatten(snapshot, uid_map, inferred)
    active = [r for r in rows if r["fee"] > 0]
    active.sort(key=lambda r: -r["fee"])

    try:
        from core import notemeta
        note_meta = notemeta.titles()
    except Exception:
        note_meta = {}

    mats = _v2_mats(active, note_meta)
    accts = _v2_accts(snapshot)
    waste = _v2_waste(mats)
    summary = _v2_summary(active)
    trend = _v2_trend()
    bucket = _v2_bucket(mats)
    cities_full = [{"n": k, "v": round(v["fee"], 2)}
                   for k, v in aggregate_by(active, "city")]
    auth = _v2_auth()
    ref = last_refresh or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    repl = {
        "__TOP_STABLE_CSS__": layout.TOP_STABLE_CSS,
        "__SUMMARY__": _j(summary),
        "__TOP_KPI_JS__": kpi_spec.TOP_KPI_JS,
        "__TREND__": _j(trend),
        "__BUCKET__": _j(bucket),
        "__CITIES_FULL__": _j(cities_full),
        "__MATS__": _j(mats),
        "__ACCTS__": _j(accts),
        "__WASTE__": _j(waste),
        "__RANGES_HTML__": _v2_range_html(
            ranges, current_range,
            custom_range=(start, end) if (current_range == "custom" and start and end) else None),
        "__RANGE_START__": start or "",
        "__RANGE_END__": end or "",
        "__MAT_COUNT__": f"{len(mats)} 条",
        "__PENDING_N__": str(auth["pending"]) if auth else "0",
        "__AUTH_SUM__": (
            (f"可用 {auth['total'] - auth['pending']} / 共 {auth['total']} 个主体"
             + (f" · {auth['tok_bad']} 个 token 失效需重新授权" if auth.get("tok_bad") else ""))
            if auth else "—"),
        "__AUTH_ROWS__": _v2_auth_html(auth),
        "__AUTH_URL__": _auth_url(),
        "__LAST_REFRESH__": ref,
        "__FOOTNOTE__": _v2_footnote(snapshot, len(mats), trend),
    }
    missing = [k for k in repl if f"__{k.strip('_')}__" not in tpl]
    if missing:
        print("⚠ v2 模板缺失占位符:", missing)
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl.replace(title_suffix or "", title_suffix or "")


# ===================================================================
# 关键词页面（独立页 /keyword）
# 模板：看板模板_关键词.html；共享 CSS 从 看板模板_v2.html 的 <style> 里抽出来注入，
# 保证两页视觉同源（改 v2 的样式，关键词页自动跟着变）。
# ===================================================================

KW_TEMPLATE_FILE = os.path.join(BASE_DIR, "看板模板_关键词.html")


def _v2_shared_css():
    """从 v2 模板抽出 <style> 内容，供关键词页复用（单一 CSS 源）"""
    try:
        with open(V2_TEMPLATE_FILE, encoding="utf-8") as f:
            t = f.read()
        i = t.find("<style>")
        j = t.find("</style>")
        if i >= 0 and j > i:
            return t[i + len("<style>"):j]
    except Exception as ex:
        print(f"⚠ 抽取 v2 共享样式失败: {ex}")
    return ""


def _kw_footnote(ksnap):
    cov = ksnap.get("coverage") or {}
    rng = ksnap.get("range") or {}
    t = ksnap.get("totals") or {}
    lines = [
        "使用说明：一行一个词（跨账号同名词已合并）· 点列头排序 · "
        "下拉筛选条按 城市 / 投放账号 / 小红书号 下钻，筛选后所有指标都按命中账号重新汇总 · "
        "鼠标悬停关键词可看它投在哪些账号",
        "数据来源：聚光开放平台离线报表 · 关键词（搜索词）维度",
        "指标口径：开口 = 私信人数 · 留资 = 私信留资数 · 客资获取率 = 留资 ÷ 开口 · "
        "开口成本 = 消耗 ÷ 开口 · 留资成本 = 消耗 ÷ 留资（≤¥100 优秀 / ≤¥250 合格）",
        "⚠ 口径：关键词消耗是「搜索词口径」的子集 —— 同一次展现可能被多个词命中，"
        "实测占账户口径 14.6%~96.7%，不可与账户/素材口径相加或对比",
        "筛选：按所选 城市 / 投放账号 / 小红书号 只聚合命中的广告主（重新汇总，不是只过滤表格行）；"
        "小红书号取该广告主的授权主体（关键词维度无笔记作者字段）",
        "词标签来自「夏天_v6 推荐词库」的意图分类；库外词＝不在推荐词库内的投放词",
        f"区间 {rng.get('start')} ~ {rng.get('end')}（{cov.get('days', 0)} 天） · "
        f"{t.get('acct_n', 0)} 户有搜索词 · {t.get('kw_n', 0)} 个词",
        f"本地词库覆盖 {cov.get('first') or '—'} ~ {cov.get('last') or '—'}"
        f"（共 {cov.get('total_days', 0)} 天）",
    ]
    return " · ".join(lines)


def build_keyword_html(ksnap, ranges=None, current_range="latest",
                       start=None, end=None, last_refresh=None):
    """关键词页渲染。注入占位符；模板缺失时返回可读的错误页（服务不挂）。"""
    try:
        with open(KW_TEMPLATE_FILE, encoding="utf-8") as f:
            tpl = f.read()
    except Exception as ex:
        print(f"⚠ 未找到关键词模板: {ex}")
        return ("<h3 style='font-family:sans-serif;padding:40px'>"
                "关键词页模板缺失：看板模板_关键词.html</h3>")

    tot = (ksnap or {}).get("totals") or {}
    repl = {
        "__SHARED_CSS__": _v2_shared_css(),
        "__TOP_STABLE_CSS__": layout.TOP_STABLE_CSS,
        "__KWSNAP__": _j(ksnap or {}),
        "__TOP_KPI_JS__": kpi_spec.TOP_KPI_JS,
        "__RANGES_HTML__": _v2_range_html(
            ranges, current_range,
            custom_range=(start, end) if (current_range == "custom" and start and end) else None),
        "__RANGE_START__": start or "",
        "__RANGE_END__": end or "",
        "__KW_RANGE_LABEL__": esc((ksnap or {}).get("range", {}).get("label") or "—"),
        "__PENDING_N__": str((_v2_auth() or {}).get("pending", 0)),
        "__AUTH_SUM__": (lambda a: (
            (f"可用 {a['total'] - a['pending']} / 共 {a['total']} 个主体"
             + (f" · {a['tok_bad']} 个 token 失效需重新授权" if a.get("tok_bad") else ""))
            if a else "—"))(_v2_auth()),
        "__AUTH_ROWS__": _v2_auth_html(_v2_auth()),
        "__AUTH_URL__": _auth_url(),
        "__LAST_REFRESH__": last_refresh or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "__FOOTNOTE__": _kw_footnote(ksnap or {}),
    }
    missing = [k for k in repl if k not in tpl]
    if missing:
        print("⚠ 关键词模板缺失占位符:", missing)
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


if __name__ == "__main__":
    main()
