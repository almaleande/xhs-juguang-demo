# -*- coding: utf-8 -*-
"""
聚光「配置查询」层 —— 后台计划 / 单元设置页字段

2026-09-23 实测新发现的两个接口：
    POST /api/open/jg/campaign/list   → data.base_campaign_dtos[]  计划配置 58 字段
    POST /api/open/jg/unit/list       → data.unit_infos[]          单元配置 34 字段

与报表层（jgapi 的 /data/report/offline/<dim>）的关系：
    配置层：预算 / 时段 / 投放日期 / 投放模式 / 竞价策略 / 目标成本 / 定向（性别·年龄·地域）
    报表层：消费 / 展现 / 点击 / 私信 / 意向 / 搜索质量等全部效果数据
    两者字段不重叠，靠 campaign_id（计划）/ unit_id（单元）/ time 关联拼接。

参数差异（实测，别写错）：
    campaign/list：page 须传 **JSON 字符串** {"page_index":1,"page_size":20}；可传 campaign_ids 数组过滤
    unit/list    ：page / page_size 传 **普通整数**，page_size 上限 100；可传 campaign_id 过滤
    两者翻页均正常（不同于报表接口 page>=2 重复第 1 页的老问题）
"""
import json

from . import jgapi

CAMP_PATH = "/api/open/jg/campaign/list"
UNIT_PATH = "/api/open/jg/unit/list"

PAGE_SIZE_MAX = 100          # unit/list 硬上限，超出报 10002
DEFAULT_PAGE_SIZE = 100


def fetch_campaigns(advertiser_id, token, page_size=DEFAULT_PAGE_SIZE,
                    max_pages=50, campaign_ids=None):
    """
    拉取某广告主的全部计划配置。

    返回 list[dict]，每项为计划配置（含 campaign_id / campaign_name / 预算 / 时段 / 竞价等）。
    失败返回 []，并把原因写到返回列表的 .error 上（抱歉，list 无属性 —— 失败时另看 last_error）。
    """
    out, seen, last_err = [], set(), None
    page_index = 1
    while page_index <= max_pages:
        body = {"advertiser_id": advertiser_id,
                "page": json.dumps({"page_index": page_index,
                                    "page_size": page_size})}
        if campaign_ids:
            body["campaign_ids"] = campaign_ids
        r = jgapi.post(CAMP_PATH, body, token=token, timeout=30)
        if not r.get("success"):
            last_err = f"code={r.get('code')} msg={r.get('msg')}"
            break
        data = r.get("data") or {}
        rows = data.get("base_campaign_dtos") or []
        total = (data.get("page") or {}).get("total_count")
        new = 0
        for x in rows:
            cid = x.get("campaign_id")
            if cid in seen:          # 翻页重复保护
                continue
            seen.add(cid)
            out.append(x)
            new += 1
        # 终止条件：没有新行 / 已拿满 / 本页不满
        if new == 0 or len(rows) < page_size:
            break
        if total is not None and len(out) >= total:
            break
        page_index += 1
    fetch_campaigns.last_error = last_err
    return out


def fetch_units(advertiser_id, token, campaign_id=None,
                page_size=DEFAULT_PAGE_SIZE, max_pages=100):
    """
    拉取某广告主的全部单元配置（含 target_config 定向 JSON）。
    page_size 会被裁剪到 100 以内（接口硬上限）。
    """
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), PAGE_SIZE_MAX))
    out, seen, last_err = [], set(), None
    page = 1
    while page <= max_pages:
        body = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
        if campaign_id is not None:
            body["campaign_id"] = campaign_id
        r = jgapi.post(UNIT_PATH, body, token=token, timeout=30)
        if not r.get("success"):
            last_err = f"code={r.get('code')} msg={r.get('msg')}"
            break
        data = r.get("data") or {}
        rows = data.get("unit_infos") or []
        total = data.get("total_count")
        new = 0
        for x in rows:
            uid = x.get("id")
            if uid in seen:
                continue
            seen.add(uid)
            out.append(x)
            new += 1
        if new == 0 or len(rows) < page_size:
            break
        if total is not None and len(out) >= total:
            break
        page += 1
    fetch_units.last_error = last_err
    return out


def parse_target_config(unit):
    """
    解析单元的 target_config（返回体里可能是 JSON 字符串，也可能是 dict）。
    解析后关键键：
        target_gender  性别 all/male/female
        target_age     年龄 '#18-22#23-27' 形态，all=不限
        target_city    地域，多城市以 # 分隔，all=不限
        targetAreaCode 地域编码
        target_device / target_device_price / target_generalization_switch
        keywords / interest_keywords / have*Group 人群包开关
    """
    raw = (unit or {}).get("target_config")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def bid_yuan(unit):
    """单元目标成本（event_bid，单位=分）→ 元。拿不到返回 None。"""
    v = (unit or {}).get("event_bid")
    try:
        return int(v) / 100.0
    except (TypeError, ValueError):
        return None


def fetch_both(advertiser_id, token, **kw):
    """
    一次拿「计划 + 单元」，并顺手挂上级关系，便于直接拼看板行。
    返回 {'campaigns': [...], 'units': [...], 'units_by_campaign': {cid: [unit,...]}}
    """
    camps = fetch_campaigns(advertiser_id, token, **kw)
    units = fetch_units(advertiser_id, token, **kw)
    by = {}
    for u in units:
        by.setdefault(u.get("campaign_id"), []).append(u)
    return {"campaigns": camps, "units": units, "units_by_campaign": by}


# 计划层「是否长期投放」判定：expire_time 为 2919 年即长期
def is_long_term(campaign):
    exp = str((campaign or {}).get("expire_time") or "")
    return exp.startswith("2919")


# 计划层时段位串 → 可读摘要（168 位 = 7 天 × 24 小时）
def time_period_desc(campaign):
    tp = str((campaign or {}).get("time_period") or "")
    if len(tp) != 168:
        return f"非标准时段位串（{len(tp)} 位）"
    if set(tp) == {"1"}:
        return "周一~周日 全天"
    days = "一二三四五六日"
    parts = []
    for di in range(7):
        seg = tp[di * 24:(di + 1) * 24]
        hours = [h for h, c in enumerate(seg) if c == "1"]
        if len(hours) == 24:
            parts.append(f"周{days[di]} 全天")
        elif hours:
            parts.append(f"周{days[di]} {hours[0]:02d}-{hours[-1] + 1:02d}时")
    return "；".join(parts) if parts else "未设置（全 0）"


# ============================================================
# 枚举码字典（2026-09-23 由后台截图 × 实探双向确认）
# 用途：把配置接口返回的码值渲染为中文标签；供投放诊断看板使用。
# 标记 ✓=实探确认  ◐=有旁证/推断  ? =待补（换户实测可补）
# ============================================================
ENUM_MARKETING_TARGET = {9: "客资收集", 4: "产品种草", 3: "种草直达"}   # 后台截图确认诉求仅4项(产品种草/客资收集/应用推广/种草直达)。9✓客资(4333计划)、4✓产品种草(687计划,计划名清一色"门店种草/信息流种草")、3◐种草直达(2条计划名"全站开口+店铺访客"/"9.12商品销量试投",且截图显示种草直达的优化目标=店铺访问/店铺成交/店铺成交ROI,同为店铺口径→强旁证)。应用推广码未定(示例从未投APP)
ENUM_PLACEMENT = {4: "全站", 2: "搜索推广", 1: "信息流推广", 7: "视频流推广"}          # 全✓(7=视频流:33条中21条计划名含"视频流";原8为误记,全量无8)。⚠可选集随营销诉求变化(见 PLACEMENT_OPTIONS_BY_MKT):产品种草=4项含独立"视频流推广";客资收集/种草直达=3项(文案"信息流推广(含视频流)")。data印证:客资收集那6条placement=7全是2023-08~2025-02老计划且0消耗,产品种草27条跨2024~2026、近3条有消耗
ENUM_DELIVERY_MODE = {1: "自动投放", 0: "手动投放"}                                    # ✓(客资收集/种草直达提供此项;产品种草截图无此行,待核)
ENUM_OPTIMIZE_TARGET = {13: "私信开口量", 50: "私信留资量", 5: "有效留资量", 1: "互动量"}   # 1✓由"产品种草"截图选项表定死(第一项即互动量);13✓(126有效/开口191)、50✓(32有效/留资35)、5◐。完整选项集按诉求分见 OPT_OPTIONS_BY_MKT
ENUM_PROMOTION_TARGET = {1: "笔记"}                                                    # 1✓ 笔直同投待补(仅客资收集下出现)
ENUM_BIDDING_STRATEGY = {7: "稳定成本"}                                                # 7✓ 最大转化待补
ENUM_DEEP_OPTIMIZE = {-1: "不启用"}                                                    # -1✓ 启用待补
ENUM_TARGET_TYPE = {2: "智能定向", 3: "指定定向", 0: "系统定向"}                        # 2✓ 3/0◐
ENUM_BUDGET_STATE = {1: "不限预算"}                                                    # 1✓

# ------------------------------------------------------------
# 各营销诉求下的完整选项集（2026-09-23 用户后台四张截图实测，一诉求一张）
# 说明：同一字段在不同诉求下的可选集不同——做诊断时必须按诉求判断，不能一概而论。
# ------------------------------------------------------------
# 优化目标：{诉求: [(后台选项名, API码 or None)]}，None=示例数据中未出现、码值待补
OPT_OPTIONS_BY_MKT = {
    "客资收集": [("私信进线量", None), ("私信开口量", 13), ("私信留资量", 50), ("有效留资量", 5)],
    "产品种草": [("互动量", 1), ("站外转化量", None), ("种草人群规模", None), ("深度种草人群规模", None)],
    "应用推广": [("APP下载点击", None), ("APP激活", None), ("APP注册", None), ("APP关键行为", None),
                  ("APP付费", None), ("APP预约下载", None), ("APP预约下载点击", None)],
    "种草直达": [("店铺访问", None), ("店铺成交", None), ("店铺成交ROI", None)],
}

# 投放位置：{诉求: [可选位置...]}（截图实测）
PLACEMENT_OPTIONS_BY_MKT = {
    "客资收集": ["全站", "搜索推广", "信息流推广（含视频流）"],
    "产品种草": ["全站", "搜索推广", "信息流推广", "视频流推广"],   # 唯一保留独立"视频流推广"的诉求
    "应用推广": ["全站"],                                              # 截图仅见全站
    "种草直达": ["全站", "搜索推广", "信息流推广（含视频流）"],
}                                                    # 1✓


def label(kind, value):
    """把枚举码渲染为中文；未知码原样返回 '码值(?)' 便于发现待补项。
    kind: marketing_target / placement / delivery_mode / optimize_target /
          promotion_target / bidding_strategy / deep_optimize_objective / target_type
    """
    table = {
        "marketing_target": ENUM_MARKETING_TARGET,
        "placement": ENUM_PLACEMENT,
        "delivery_mode": ENUM_DELIVERY_MODE,
        "optimize_target": ENUM_OPTIMIZE_TARGET,
        "promotion_target": ENUM_PROMOTION_TARGET,
        "bidding_strategy": ENUM_BIDDING_STRATEGY,
        "deep_optimize_objective": ENUM_DEEP_OPTIMIZE,
        "target_type": ENUM_TARGET_TYPE,
        "budget_state": ENUM_BUDGET_STATE,
    }.get(kind, {})
    try:
        k = int(value)
    except (TypeError, ValueError):
        return str(value)
    return table.get(k, f"{value}(?)")


def bid_yuan_plan(campaign):
    """计划层目标成本 constraint_value（单位=分）→ 元。"""
    v = (campaign or {}).get("constraint_value")
    try:
        return int(v) / 100.0
    except (TypeError, ValueError):
        return None


def budget_desc(campaign):
    """计划预算可读摘要。不限 → '不限'；指定 → 'N元/天'。"""
    c = campaign or {}
    if int(c.get("budget_state") or 0) == 1 or int(c.get("schedule_limit_budget") or 0) == -1:
        return "不限"
    for f in ("limit_day_budget", "campaign_day_budget"):
        v = c.get(f)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            return f"{iv / 100.0:.0f}元/天"
    return "不限"


def placement_desc(campaign):
    return label("placement", (campaign or {}).get("placement"))


def optimize_desc(campaign):
    return label("optimize_target", (campaign or {}).get("optimize_target"))
