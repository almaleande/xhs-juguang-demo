#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
关键词（搜索词）维度：取数 + 本地日库 + 区间聚合
================================================
给「关键词看板」提供数据底座。

关键事实（2026-09-21 全量实测，别凭直觉改）
  · 接口 /api/open/jg/data/report/offline/keyword，行粒度 = keyword × time × unit_id
  · 单行 104 字段；**没有 creativity_id / note_id** → 关键词**不能归因到素材/笔记**，
    只能归到 广告主(=投放账号) / 城市 / 计划 / 单元
  · 翻页是坏的（page>=2 重复第 1 页）→ 用日期二分；page_size 上限 500
  · **口径**：关键词消耗是搜索词口径的**子集**，实测占账户口径 14.6%~96.7%（均值 ~69%）
    → 绝不与账户/创意口径相加，页面必须标「词口径」
  · 伪词「智能搭建词包」= 平台自动词包，不是真实查询词，单独打标

存储 data/keyword_daily.json（按「户 × 天 × 词」聚合，单元已合并）
  { pulled_at, account_sig, pulls: [...],
    advertisers: { aid: { name, city, owner_key,
                          days: { "YYYY-MM-DD": { 关键词: {聚合指标} } } } },
    failed: [...], invalid_token_accounts: n }
  写入按 (户, 天) 覆盖 —— 重拉只替换那天，其余保留（合并写，避免某户某轮失败就整段消失）。
  任意区间都在本地聚合，不再请求 API（所以「近一年」也能秒出）。
"""
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

try:
    from . import jgapi, tokenhub
    from .collector import account_signature, infer_city, CITY_KEYWORDS
    from .paths import BASE_DIR, DATA_DIR
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi, tokenhub
    from core.collector import account_signature, infer_city, CITY_KEYWORDS
    from core.paths import BASE_DIR, DATA_DIR


KW_FILE = os.path.join(DATA_DIR, "keyword_daily.json")
KWLIB_CSV = os.path.join(BASE_DIR, "关键词", "夏天_V6_最终分类.csv")
KWLIB_FILE = os.path.join(DATA_DIR, "kwlib.json")

# 日库里累加的指标（蛇形原名，与接口一致，便于对照）
SUM_FIELDS = [
    "fee", "impression", "click",
    "message_consult", "msg_leads_num", "message_user", "initiative_message",
    "valid_leads", "leads",
    "like", "comment", "collect", "share", "follow",
    "search_invoke_button_click_cnt", "search_invoke_button_click_cost",
    "action_button_click", "add_wechat_count", "add_wechat_suc_count",
    "wechat_talk_count", "screenshot", "pic_save",
]

# 平台自动词包，不是真实查询词
PSEUDO_WORDS = ("智能搭建词包", "智能搭建", "系统自动", "自动扩量")

# 「高耗无开口」判定门槛
WASTE_MIN_FEE = 30.0

# 效率标签阈值 —— **必须与 看板生成.py 的 EFF_ML_GOOD / EFF_ML_OK / EFF_MSG_OK 保持一致**，
# 由 snapshot 下发给前端，避免两处硬编码漂移（素材看板与关键词页的徽标口径要能对上）。
EFF_GOOD = 100     # 留资成本 ≤100 优秀
EFF_OK = 250       # ≤250 合格，>250 成本偏高
EFF_OPEN_OK = 120  # 开口成本 ≤120 算健康


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- 词库（打标） ----------------

_STYLE_HINT = ("风格", "主题", "系列", "氛围", "色调", "构图", "场景", "拍法")
_CONSULT_HINT = (
    "多少钱", "价格", "费用", "报价", "推荐", "怎么样", "哪家", "排行", "排名",
    "攻略", "避坑", "注意", "流程", "准备", "区别", "对比", "测评", "评价",
    "口碑", "好不好", "值得", "贵吗", "预算", "划算", "坑",
)
# 城市词判定用的「通用词根」：把城市名和这些词根都剥掉后应当空空如也，
# 否则说明里面夹了品牌/机构名（如「青岛葛罗婚纱照」），不能算城市词。
_GENERIC_HINT = ("婚纱照", "婚纱", "婚纱摄影", "摄影", "写真", "旅拍", "婚庆",
                 "婚礼", "拍照", "相馆", "影楼", "工作室", "门店", "店")


def build_kwlib(force=False, verbose=False):
    """
    词库 CSV → data/kwlib.json（keyword → {lvl,intent,idx,bid,city}）。
    列：关键词,推荐理由,竞争指数,月搜索指数,市场出价,层级,意图分类,城市范围
    """
    if not force and os.path.exists(KWLIB_FILE):
        try:
            with open(KWLIB_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("map"):
                return d
        except Exception:
            pass
    if not os.path.exists(KWLIB_CSV):
        return {"built_at": int(time.time()), "source": None, "n": 0, "map": {}}
    mp = {}
    try:
        # 该 CSV 带 BOM，必须 utf-8-sig
        with open(KWLIB_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                kw = (row.get("关键词") or "").strip()
                if not kw:
                    continue
                mp[kw] = {
                    "lvl": (row.get("层级") or "").strip(),
                    "intent": (row.get("意图分类") or "").strip(),
                    "idx": int(_f(row.get("月搜索指数"))),
                    "bid": round(_f(row.get("市场出价")), 2),
                    "city": (row.get("城市范围") or "").strip(),
                }
    except Exception as ex:
        if verbose:
            print(f"  词库解析失败: {ex}")
        mp = {}
    out = {"built_at": int(time.time()), "source": os.path.basename(KWLIB_CSV),
           "n": len(mp), "map": mp}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(KWLIB_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    if verbose:
        print(f"  词库打标：{len(mp)} 词 → {KWLIB_FILE}")
    return out


_LIB_CACHE = {"map": None, "mtime": 0}


def kwlib_map():
    global _LIB_CACHE
    try:
        mt = os.path.getmtime(KWLIB_FILE)
    except OSError:
        mt = 0
    if _LIB_CACHE["map"] is None or mt != _LIB_CACHE["mtime"]:
        d = build_kwlib()
        _LIB_CACHE = {"map": d.get("map") or {}, "mtime": mt}
    return _LIB_CACHE["map"]


_OWNER_CACHE = {"map": None, "t": 0.0}


def owner_label_map():
    """
    授权主体键 → 小红书专业号展示名（带 10 分钟缓存）。
    关键词接口没有 note_user_id，所以「小红书号」维度只能用**广告主的授权主体**：
    一个投放户只挂在一个主体下，这个映射是准确的一对多。
    """
    global _OWNER_CACHE
    now = time.time()
    if _OWNER_CACHE["map"] is not None and now - _OWNER_CACHE["t"] < 600:
        return _OWNER_CACHE["map"]
    out = {}
    try:
        from . import accounts_meta
        info = accounts_meta.build()
        for a in info.get("accounts") or []:
            login = a.get("login") or []
            real = (login[0]["name"] if login else "").strip()
            out[a["key"]] = real or a.get("label") or a["key"]
    except Exception:
        out = {}
    _OWNER_CACHE = {"map": out, "t": now}
    return out


def is_pseudo(kw):
    return any(p in kw for p in PSEUDO_WORDS)


def tag_of(kw, lib=None):
    """
    词标签：优先用词库的「意图分类」，库外词按规则兜底。
    返回 (标签, 层级, 月搜索指数, 市场出价)
    """
    lib = lib if lib is not None else kwlib_map()
    if kw in lib:
        m = lib[kw]
        intent = m.get("intent") or ""
        if intent:
            return intent, m.get("lvl") or "", m.get("idx") or 0, m.get("bid") or 0
    # ---- 库外兜底（顺序有讲究：先咨询，再风格，最后才判城市词）----
    if is_pseudo(kw):
        return "自动词包", "", 0, 0
    if "夏天" in kw:
        return "品牌词", "", 0, 0
    if any(h in kw for h in _CONSULT_HINT):
        return "咨询词", "", 0, 0
    if any(h in kw for h in _STYLE_HINT) or ("感" in kw and "照" in kw):
        return "风格词", "", 0, 0
    for c in CITY_KEYWORDS:
        if c not in kw:
            continue
        rest = kw.replace(c, "")
        for g in sorted(_GENERIC_HINT, key=len, reverse=True):
            rest = rest.replace(g, "")
        if len(rest) <= 1:            # 只剩城市名 + 通用词根 → 确实是城市词
            return "城市词", "", 0, 0
        break                         # 夹了品牌/机构名 → 不妄判，落库外
    return "库外词", "", 0, 0


# ---------------- 日库读写 ----------------

def load_daily():
    try:
        with open(KW_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write(payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KW_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, KW_FILE)


def _blank():
    d = {f: 0.0 for f in SUM_FIELDS}
    d["loc_w"] = 0.0      # Σ(词均展现位置 × 展现)，用于算加权均位
    d["loc_i"] = 0.0      # Σ(展现)，仅统计有均位的行
    d["u"] = []           # 出现过的单元 ID（当日内去重）
    return d


def agg_keyword_rows(rows):
    """把某户某天的关键词明细行按关键词聚合（单元合并）。返回 {关键词: 聚合dict}"""
    out = {}
    for r in rows:
        kw = str(r.get("keyword") or "").strip()
        if not kw:
            continue
        a = out.get(kw)
        if a is None:
            a = _blank()
            out[kw] = a
        for f in SUM_FIELDS:
            a[f] += _f(r.get(f))
        imp = _f(r.get("impression"))
        loc = _f(r.get("word_avg_location"))
        if loc > 0:
            w = imp if imp > 0 else 1.0
            a["loc_w"] += loc * w
            a["loc_i"] += w
        uid = str(r.get("unit_id") or "")
        if uid and uid not in a["u"]:
            a["u"].append(uid)
    # 削掉全零字段与空单元，压缩体积
    for a in out.values():
        for f in list(SUM_FIELDS):
            if not a[f]:
                del a[f]
        if not a["loc_i"]:
            a.pop("loc_w", None)
            a.pop("loc_i", None)
        if not a["u"]:
            a.pop("u", None)
    return out


def _adv_jobs(accounts):
    """token 有效账号下的全部投放户 → [(aid, name, owner_key, token)]"""
    jobs, seen = [], set()
    for key, rec in (accounts or {}).items():
        if tokenhub.classify(rec) not in ("valid", "expiring", "refreshable"):
            continue
        tok = (rec.get("access_token") or "").strip()
        if len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        for adv in rec.get("advertisers") or []:
            aid = adv.get("advertiser_id")
            if aid is None or str(aid) in seen:
                continue
            seen.add(str(aid))
            jobs.append((str(aid),
                         adv.get("advertiser_name") or f"广告主{aid}",
                         key, tok))
    return jobs


def collect_keyword_daily(start_date, end_date, verbose=True,
                          refresh_first=True, force=False):
    """
    拉区间内全部投放户的关键词数据 → 合并写入 data/keyword_daily.json。
    实测成本：163 户 / 近 7 日 = 36s（并发 4）；30 日单户会超 page_size 触发二分，耗时更长。
    """
    t0 = time.time()
    # 聚光不接受 end_date = 今天
    today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    if end_date >= today:
        end_date = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")
    if start_date > end_date:
        start_date = end_date
    if refresh_first:
        tokenhub.ensure_valid(verbose=False, dry_run=False)

    accounts = tokenhub.load_accounts()
    jobs = _adv_jobs(accounts)
    sig, _ids = account_signature(accounts)

    old = load_daily() or {}
    advs_old = old.get("advertisers") or {}
    if force:
        # 强制模式：丢掉区间内旧数据再重拉（授权后要让新主体立刻可见）
        advs_old = {}

    n_workers = max(1, int(os.environ.get("JUGUANG_CONCURRENCY", "4")))

    def one(j):
        aid, name, key, tok = j
        rows, meta = jgapi.fetch_keywords(tok, aid, start_date, end_date)
        return aid, name, key, rows, meta

    advs = dict(advs_old)
    failed, ok_n = [], 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for fut in as_completed([pool.submit(one, j) for j in jobs]):
            aid, name, key, rows, meta = fut.result()
            if not meta.get("ok"):
                failed.append({"advertiser_id": aid, "name": name,
                               "code": meta.get("code"), "msg": meta.get("msg")})
                continue
            ok_n += 1
            by_day = {}
            for dt, kws in _group_by_day(rows).items():
                by_day[dt] = agg_keyword_rows(kws)
            if not by_day:
                continue
            rec = advs.get(aid) or {}
            days = dict(rec.get("days") or {})
            for dt, kws in by_day.items():
                if kws:
                    days[dt] = kws
                else:
                    days.pop(dt, None)
            advs[aid] = {
                "name": name,
                "owner_key": key,
                "city": rec.get("city") or infer_city(name),
                "days": days,
            }

    pulls = list(old.get("pulls") or [])[-11:]
    pulls.append({"start": start_date, "end": end_date, "at": int(time.time()),
                  "adv_ok": ok_n, "adv_fail": len(failed)})
    payload = {
        "pulled_at": int(time.time()),
        "account_sig": sig,
        "pulls": pulls,
        "advertisers": advs,
        "failed": failed,
        "invalid_token_accounts": len(_bad_token_keys(accounts)),
    }
    _write(payload)
    el = round(time.time() - t0, 1)
    if verbose:
        n_days = len(_all_days(payload))
        print(f"  关键词日库：{len(advs)} 户 / 覆盖 {n_days} 天 / "
              f"{len(failed)} 户失败 / 耗时 {el}s → {KW_FILE}")
    return payload


def _group_by_day(rows):
    out = {}
    for r in rows:
        dt = str(r.get("time") or "")[:10]
        if not dt:
            continue
        out.setdefault(dt, []).append(r)
    return out


def _bad_token_keys(accounts):
    out = []
    for k, rec in (accounts or {}).items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            out.append(k)
    return out


def _all_days(payload):
    s = set()
    for info in (payload.get("advertisers") or {}).values():
        s |= set((info.get("days") or {}).keys())
    return sorted(s)


# ---------------- 区间聚合 ----------------

def _acc():
    """区间聚合累加器"""
    return {"fee": 0.0, "impression": 0.0, "click": 0.0,
            "message_consult": 0.0, "msg_leads_num": 0.0,
            "valid_leads": 0.0, "leads": 0.0,
            "message_user": 0.0, "initiative_message": 0.0,
            "add_wechat_count": 0.0, "add_wechat_suc_count": 0.0,
            "wechat_talk_count": 0.0,
            "like": 0.0, "comment": 0.0, "collect": 0.0, "share": 0.0, "follow": 0.0,
            "search_invoke_button_click_cnt": 0.0,
            "search_invoke_button_click_cost": 0.0,
            "action_button_click": 0.0,
            "screenshot": 0.0, "pic_save": 0.0,
            "loc_w": 0.0, "loc_i": 0.0,
            "days": 0, "units": set(), "accts": set(), "cities": set(),
            "kws": set(), "dset": set()}


def _add(acc, a, adv_name, city, kw=None, dt=None):
    for f in SUM_FIELDS:
        acc[f] += _f(a.get(f))
    acc["loc_w"] += _f(a.get("loc_w"))
    acc["loc_i"] += _f(a.get("loc_i"))
    for u in (a.get("u") or []):
        acc["units"].add(u)
    acc["accts"].add(adv_name)
    acc["cities"].add(city)
    if kw:
        acc["kws"].add(kw)
    if dt:
        acc["dset"].add(dt)


def _fin(acc):
    """累加器 → 可直接展示的一行

    私信链路三层（2026-09-21 与用户确认的口径，别再改）：
      咨询 `msg`  = message_consult 私信咨询数
      开口 `open` = message_user    私信人数（真正聊起来的独立人数）
      留资 `ml`   = msg_leads_num   私信留资数（也是素材看板的「留资」，两页同源）
    表单留资（leads / valid_leads / leads_cvr）在关键词维度实测全为 0，不采用。
    """
    fee = acc["fee"]
    imp = acc["impression"]
    clk = acc["click"]
    msg = acc["message_consult"]
    opn = acc["message_user"]
    ml = acc["msg_leads_num"]
    d = {
        "fee": round(fee, 2),
        "imp": int(imp),
        "clk": int(clk),
        "ctr": round(clk / imp * 100, 2) if imp > 0 else None,
        "cpc": round(fee / clk, 2) if clk > 0 else None,
        "cpm": round(fee / imp * 1000, 2) if imp > 0 else None,
        "loc": round(acc["loc_w"] / acc["loc_i"], 2) if acc["loc_i"] > 0 else None,
        "msg": int(msg),
        "open": int(opn),
        "open_c": round(fee / opn, 2) if opn > 0 else None,
        "ml": int(ml),
        "mlc": round(fee / ml, 2) if ml > 0 else None,
        "rate": round(ml / opn * 100, 2) if opn > 0 else None,
        "msgc": round(fee / msg, 2) if msg > 0 else None,
        "valid_leads": int(acc["valid_leads"]),
        "wechat": int(acc["add_wechat_count"]),
        "wechat_suc": int(acc["add_wechat_suc_count"]),
        "interaction": int(acc["like"] + acc["comment"] + acc["collect"]
                           + acc["share"] + acc["follow"]),
        "btn_click": int(acc["search_invoke_button_click_cnt"]),
        "days": len(acc["dset"]) if acc["dset"] else acc["days"],
        "units": len(acc["units"]),
    }
    return d


def keyword_snapshot(start, end, label=None, city=None, adv=None, owner=None):
    """
    把日库聚合成关键词页面要用的结构（本地，零请求）。
    返回 {range, coverage, totals, keywords, accounts, cities, classes, waste, filters}

    city / adv / owner 三个可选筛选：命中后**只聚合命中的广告主**。
    注意不能在前端「过滤行」——那样筛完之后每行的消耗/留资仍是全量口径，会误导。
    filters 永远基于未筛选的全量，保证筛选器随时能切回来。
    """
    d = load_daily() or {}
    advs = d.get("advertisers") or {}
    lib = kwlib_map()
    olabels = owner_label_map()

    def _meta(info, aid):
        nm = info.get("name") or aid
        return nm, (info.get("city") or infer_city(nm)), (info.get("owner_key") or "")

    # ---------- 筛选器选项（始终基于全量） ----------
    f_city, f_adv, f_owner = {}, {}, {}

    def _bump(store, k, kw, fee, label=None):
        it = store.get(k)
        if it is None:
            it = {"k": k, "n": 0, "fee": 0.0, "s": set()}
            if label:
                it["l"] = label
            store[k] = it
        it["s"].add(kw)
        it["fee"] += fee

    for aid, info in advs.items():
        _nm, _ct, _ok = _meta(info, aid)
        for dt, kws in (info.get("days") or {}).items():
            if dt < start or dt > end:
                continue
            for kw, a in kws.items():
                fee = _f(a.get("fee"))
                if fee <= 0 and _f(a.get("impression")) <= 0:
                    continue
                _bump(f_city, _ct, kw, fee)
                _bump(f_adv, _nm, kw, fee)
                _bump(f_owner, _ok, kw, fee, label=olabels.get(_ok) or _ok)

    def _filt(store):
        items = [dict({"k": v["k"], "n": len(v["s"]), "fee": round(v["fee"], 2)},
                      **({"l": v["l"]} if v.get("l") else {}))
                 for v in store.values() if v["fee"] > 0]
        items.sort(key=lambda x: -x["fee"])
        return items[:60]

    # ---------- 主聚合（应用筛选） ----------
    kw_map = {}
    acct_map = {}
    city_map = {}
    cls_map = {}
    day_set = set()

    for aid, info in advs.items():
        name, cty, ok = _meta(info, aid)
        if city and cty != city:
            continue
        if adv and name != adv:
            continue
        if owner and ok != owner:
            continue
        a_days = info.get("days") or {}
        a_acc = None
        for dt, kws in a_days.items():
            if dt < start or dt > end:
                continue
            day_set.add(dt)
            for kw, a in kws.items():
                fee = _f(a.get("fee"))
                if fee <= 0 and _f(a.get("impression")) <= 0:
                    continue                      # 零展现零消耗的词不进榜
                tag, lvl, idx, bid = tag_of(kw, lib)
                k = kw_map.get(kw)
                if k is None:
                    k = _acc()
                    k["tag"] = tag
                    k["lvl"] = lvl
                    k["lib_idx"] = idx
                    k["lib_bid"] = bid
                    kw_map[kw] = k
                _add(k, a, name, cty, kw=kw, dt=dt)
                c = city_map.get(cty)
                if c is None:
                    c = _acc()
                    city_map[cty] = c
                _add(c, a, name, cty, kw=kw, dt=dt)
                cl = cls_map.get(tag)
                if cl is None:
                    cl = _acc()
                    cls_map[tag] = cl
                _add(cl, a, name, cty, kw=kw, dt=dt)
                if a_acc is None:
                    a_acc = _acc()
                _add(a_acc, a, name, cty, kw=kw, dt=dt)
        if a_acc is not None:
            row = _fin(a_acc)
            row.update({"adv": name, "adv_id": aid, "city": cty,
                        "kw_n": len(a_acc["kws"]),
                        "owner_key": ok,
                        "owner": olabels.get(ok) or ok})
            acct_map[aid] = row

    # 关键词行 + 账号词数
    keywords = []
    for kw, acc in kw_map.items():
        row = _fin(acc)
        accts = sorted(acc["accts"])
        row.update({
            "kw": kw,
            "tag": acc.get("tag") or "",
            "lvl": acc.get("lvl") or "",
            "lib_idx": acc.get("lib_idx") or 0,
            "lib_bid": acc.get("lib_bid") or 0,
            "acct_n": len(accts),
            "accts": accts[:8],
            "cities": sorted(acc["cities"]),
            "adv": accts[0] if len(accts) == 1 else "",
            "city": sorted(acc["cities"])[0] if len(acc["cities"]) == 1 else "多城",
            "pseudo": 1 if is_pseudo(kw) else 0,
        })
        keywords.append(row)
    keywords.sort(key=lambda r: -r["fee"])

    accounts = list(acct_map.values())
    accounts.sort(key=lambda r: -r["fee"])

    cities = []
    for c, acc in city_map.items():
        row = _fin(acc)
        row.update({"city": c, "acct_n": len(acc["accts"]),
                    "kw_n": len(acc["kws"])})
        cities.append(row)
    cities.sort(key=lambda r: -r["fee"])

    classes = []
    for t, acc in cls_map.items():
        row = _fin(acc)
        row.update({"cls": t, "acct_n": len(acc["accts"]), "kw_n": 0})
        classes.append(row)
    # 词类词数
    cls_kw = {}
    for kw, acc in kw_map.items():
        cls_kw[acc.get("tag") or ""] = cls_kw.get(acc.get("tag") or "", 0) + 1
    for r in classes:
        r["kw_n"] = cls_kw.get(r["cls"], 0)
    classes.sort(key=lambda r: -r["fee"])

    # 高耗无开口词（口径改为：零咨询 且 零开口人数 —— 花了钱一个私信都没聊起来）
    waste = [k for k in keywords
             if k["fee"] >= WASTE_MIN_FEE and k["open"] == 0 and k["msg"] == 0]
    waste.sort(key=lambda k: -k["fee"])

    # 总量
    tot_acc = _acc()
    for acc in kw_map.values():
        for f in SUM_FIELDS:
            tot_acc[f] += acc[f]
        tot_acc["loc_w"] += acc["loc_w"]
        tot_acc["loc_i"] += acc["loc_i"]
    totals = _fin(tot_acc)
    totals.update({
        "days": len(day_set),
        "kw_n": len(keywords),
        "kw_fee_n": sum(1 for k in keywords if k["fee"] > 0),
        "acct_n": len(accounts),
        "city_n": len(cities),
        "waste_n": len(waste),
        "waste_fee": round(sum(k["fee"] for k in waste), 2),
        "brand_fee": round(sum(k["fee"] for k in keywords if k["tag"] == "品牌词"), 2),
        "brand_kw_n": sum(1 for k in keywords if k["tag"] == "品牌词"),
    })

    all_days = _all_days(d)
    in_range_days = sorted(day_set)
    # 瘦身：只把页面真正用到的字段发给前端（KWSNAP 直接内联进 HTML，体积敏感）
    keywords = [_slim(r, ("kw", "tag", "lvl", "lib_idx", "lib_bid", "adv",
                          "city", "acct_n", "accts", "cities", "pseudo"), kw=True)
                for r in keywords]
    waste = [_slim(r, ("kw", "tag", "lvl", "lib_idx", "lib_bid", "adv",
                       "city", "acct_n", "accts", "cities", "pseudo"), kw=True)
             for r in waste]
    accounts = [_slim(r, ("adv", "adv_id", "city", "owner")) for r in accounts]
    cities = [_slim(r, ("city", "acct_n")) for r in cities]
    classes = [_slim(r, ("cls", "acct_n")) for r in classes]
    return {
        "range": {"start": start, "end": end, "label": label or f"{start} ~ {end}"},
        "active": {"city": city or "", "adv": adv or "", "owner": owner or ""},
        "filters": {
            "city": _filt(f_city),
            "adv": _filt(f_adv),
            "owner": _filt(f_owner),
        },
        "eff": {"good": EFF_GOOD, "ok": EFF_OK, "msg": EFF_OPEN_OK, "waste": WASTE_MIN_FEE},
        "coverage": {
            "days": len(in_range_days),
            "days_list": in_range_days,
            "first": all_days[0] if all_days else None,
            "last": all_days[-1] if all_days else None,
            "total_days": len(all_days),
            "pulled_at": d.get("pulled_at"),
            "lib_n": len(lib),
        },
        "totals": totals,
        "keywords": keywords,
        "accounts": accounts,
        "cities": cities,
        "classes": classes,
        "waste": waste,
    }


# 页面真正用到的字段（`msg` 私信咨询数与 `open` 私信人数几乎重合，2026-09-21 用户确认合并为「开口」，不再下发）
_ROW_KEEP = ("fee", "imp", "clk", "ctr", "cpc", "loc", "open", "open_c",
             "ml", "mlc", "rate", "days", "units", "kw_n", "acct_n")
# 值为 None 时直接不输出（省体积；前端按 ==null 兜底成「—」）
_NULL_DROP = ("ctr", "cpc", "loc", "open_c", "mlc", "rate")


def _slim(row, extra=(), kw=False):
    """只留页面用到的字段，压缩内联到 HTML 的体积"""
    out = {}
    for k in _ROW_KEEP:
        if k not in row:
            continue
        if row[k] is None and k in _NULL_DROP:
            continue
        out[k] = row[k]
    for k in extra:
        if k not in row:
            continue
        v = row[k]
        if k == "accts":
            v = list(v)[:4]
        elif k == "cities":
            v = list(v)[:2]
        if v == "" or v == 0 or v is None:
            if k in ("lvl", "lib_idx", "lib_bid", "pseudo"):
                continue          # 空值不占体积
        out[k] = v
    if kw:
        out.pop("kw_n", None)
    return out


def account_kw_counts(start, end):
    """每户在区间内的关键词条数（单独一遍，避免主循环里反向查找）"""
    d = load_daily() or {}
    out = {}
    for aid, info in (d.get("advertisers") or {}).items():
        s = set()
        for dt, kws in (info.get("days") or {}).items():
            if start <= dt <= end:
                s |= set(kws.keys())
        out[aid] = len(s)
    return out


def backfill(days=90, chunk=10, verbose=True):
    """
    回填近 N 天（按 chunk 天一段，段内一次请求即可覆盖多天，比逐天拉省得多）。
      90 天 / chunk=10 ≈ 9 段 × 163 户 ≈ 1,500 请求，约 6~10 分钟。
    """
    t_end = datetime.now().date() - timedelta(days=1)
    t_start = t_end - timedelta(days=days - 1)
    cur = t_start
    n = 0
    while cur <= t_end:
        seg_end = min(cur + timedelta(days=chunk - 1), t_end)
        n += 1
        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] 第 {n} 段 {cur} ~ {seg_end}", flush=True)
        collect_keyword_daily(cur.isoformat(), seg_end.isoformat(),
                              verbose=verbose, refresh_first=(n == 1))
        cur = seg_end + timedelta(days=1)
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="关键词日库")
    ap.add_argument("--build-lib", action="store_true", help="重建词库打标")
    ap.add_argument("--backfill", type=int, default=0, help="回填近 N 天")
    ap.add_argument("--chunk", type=int, default=10)
    ap.add_argument("--days", type=int, default=7, help="增量拉近 N 天")
    ap.add_argument("--dry", action="store_true", help="只统计不拉取")
    a = ap.parse_args()
    if a.build_lib:
        build_kwlib(force=True, verbose=True)
    if a.dry:
        snap = keyword_snapshot("1970-01-01", "2999-12-31")
        t = snap["totals"]
        print(f"本地词库：{snap['coverage']['total_days']} 天 / "
              f"{t['kw_n']} 词 / ¥{t['fee']:,.2f}")
        raise SystemExit(0)
    if a.backfill:
        backfill(a.backfill, a.chunk)
    else:
        from datetime import timedelta
        e = datetime.now().date() - timedelta(days=1)
        s = e - timedelta(days=a.days - 1)
        collect_keyword_daily(s.isoformat(), e.isoformat())
