#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据采集
=========
遍历账号库里所有「已授权」的主体 → 逐个投放户拉取创意报表 → 聚合落快照。

相比旧版 `月报生成.py` 的改进：
1. 只对 token 有效的主体发请求，坏的直接跳过并记账（旧版会拿坏 token 白打一遍）
2. 区分「真没投放（0 行）」和「拉取失败（success=False）」，各自计数
3. 输出覆盖率报告 + 失败清单，落盘 data/collect_report.json
4. 城市推断修复：旧规则没有裸城市名，导致「东营婚纱照夏天」这类户名全部落进「其他」
"""
import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from . import jgapi, tokenhub
    from .paths import (COLLECT_REPORT_FILE, DATA_DIR, SNAPSHOT_DIR)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi, tokenhub
    from core.paths import (COLLECT_REPORT_FILE, DATA_DIR, SNAPSHOT_DIR)

# 特殊映射（先匹配，避免被城市关键词抢走）
SPECIAL_RULES = [
    ("华中旗舰店", "青岛"), ("拾光宝盒", "青岛"), ("叁柒服务", "青岛"),
    ("摄影美学中心", "总号"), ("美学中心", "总号"),
    ("夏天品牌", "品牌"),
    ("化妆摄影职业培训", "培训"), ("夏天摄影培训", "培训"), ("夏天化妆培训", "培训"),
    ("夏天旅拍", "旅拍"),
    ("烟台LIX", "其他"), ("LIX肖像", "其他"),
    ("烟台夏天", "烟台"), ("烟台实", "烟台"),
    ("枣庄云传媒", "枣庄"),
    ("微思敦", "其他"), ("夏天-YX", "其他"),
]

# 用户负责的 14 城（裸关键词兜底匹配，修复旧规则的漏判）
CITY_KEYWORDS = [
    "青岛", "威海", "临沂", "泰安", "日照", "聊城", "滨州",
    "济宁", "菏泽", "东营", "潍坊", "德州", "济南", "淄博",
]


def infer_city(adv_name):
    name = adv_name or ""
    for kw, city in SPECIAL_RULES:
        if kw in name:
            return city
    for c in CITY_KEYWORDS:
        if c in name:
            return c
    return "其他"


def owner_label(key, rec):
    """
    推导「小红书账号」的可读短名。
    优先级：主体键的中文后缀 > 该账号下广告主的高频城市 > user_id 尾号
    """
    if "_" in key:
        suf = key.rsplit("_", 1)[1].strip()
        if suf and not suf.isdigit():
            return suf
    advs = rec.get("advertisers") or []
    tally = {}
    for a in advs:
        c = infer_city(a.get("advertiser_name", ""))
        if c != "其他":
            tally[c] = tally.get(c, 0) + 1
    if tally:
        return max(tally.items(), key=lambda x: x[1])[0]
    uid = (rec.get("user_id") or "").strip()
    if uid:
        return f"账号{uid[-4:]}"
    return "未识别账号"


DAILY_CACHE_FILE = os.path.join(DATA_DIR, "account_daily.json")

# 日粒度缓存里保留的字段（够算 KPI / 各维度 / 趋势即可，控制文件体积）
DAILY_FIELDS = [
    "fee", "impression", "click", "message_consult", "msg_leads_num",
    "like", "comment", "collect", "share", "follow", "interaction",
    "video_play_5s_cnt",
]

ACCOUNT_DIM_PATH = "/api/open/jg/data/report/offline/account"


def fetch_account_daily(tok, aid, start_date, end_date, page_size=1000):
    """
    账户维度报表天然是「1 行/天/户」，所以一次拉取就能覆盖整个区间。
    返回 {date: {field: value}}；这是多时间维度的数据底座 ——
    拉一次近一年，之后任何区间都能在本地聚合出来，切换无需再请求 API。

    注：这里的写入是「按天覆盖」而非累加，所以即便碰上聚光的翻页重复 bug
    也不会算错；额外加一道「整页无新数据即停」的防御，避免空跑请求。
    """
    out = {}
    page = 1
    while page <= 4:
        resp = jgapi.post(ACCOUNT_DIM_PATH, {
            "advertiser_id": aid, "start_date": start_date, "end_date": end_date,
            "page": page, "page_size": page_size}, token=tok)
        if not resp.get("success"):
            return out, {"ok": False, "code": resp.get("code"), "msg": resp.get("msg", "")}
        d = resp.get("data") or {}
        lst = d.get("data_list") or []
        new_days = 0
        for r in lst:
            dt = str(r.get("time") or "")[:10]
            if not dt:
                continue
            if dt not in out:
                new_days += 1
            out[dt] = {f: float(jgapi._to_float(r.get(f))) for f in DAILY_FIELDS}
        if not lst or page * page_size >= int(d.get("total_count") or 0):
            break
        if new_days == 0:
            break                      # 整页重复 → API 分页异常，停止
        page += 1
    return out, {"ok": True}


def authorized_advertiser_ids(accounts=None, require_valid_token=True):
    """
    账号库里的投放户 ID 集合。

    require_valid_token=True 时只算 token 有效的账号 —— 坏 token 的户本来也拉不到，
    不该影响缓存指纹；False 时把账号库里登记的户全算上（用于判断「是否已从账号库移除」）。
    """
    accounts = accounts if accounts is not None else tokenhub.load_accounts()
    ids = set()
    for _k, rec in (accounts or {}).items():
        if require_valid_token:
            tok = (rec.get("access_token") or "").strip()
            if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
                continue
        for adv in rec.get("advertisers") or []:
            aid = adv.get("advertiser_id")
            if aid is not None:
                ids.add(str(aid))
    return ids


def live_advertiser_ids(start_date, end_date, accounts=None, daily=None):
    """
    有消耗投放户 ID 集合（诊断采集预筛用）。

    实测（2026-09-24，两个区间交叉验证）：与诊断实测「有消耗计划的户」**零漏零多**
    —— 09-22 单日 28/28、08 月全月 34/34，命中率 100%。且账户日库是每日刷新的
    既有产物，预筛**零额外请求**。

    ⚠ 三道保护，宁多拉不漏拉（预筛错了会永久丢数据，多拉只是慢）：
      ① 只有日库覆盖范围确实包含本区间时才敢筛；
      ② 日库拉取失败的户状态未知 → 一律保留；
      ③ 日库完全没有记录的户（刚授权）状态未知 → 一律保留。
    返回 None 表示**不可用**（日库缺失/未覆盖区间），调用方应退化为「不预筛」。
    """
    daily = daily if daily is not None else load_account_daily()
    if not daily or not (daily.get("advertisers") or {}):
        return None
    dr = daily.get("range") or {}
    if not (dr.get("start") and dr.get("end")
            and dr["start"] <= start_date and dr["end"] >= end_date):
        return None

    live = set()
    for aid, info in daily["advertisers"].items():
        for dt, vals in (info.get("days") or {}).items():
            if not (start_date <= dt <= end_date):
                continue
            if any(float(vals.get(f) or 0) > 0 for f in
                   ("fee", "impression", "message_consult", "msg_leads_num")):
                live.add(str(aid))
                break
    for x in (daily.get("failed") or []):
        if x.get("advertiser_id") is not None:
            live.add(str(x["advertiser_id"]))
    acc = accounts if accounts is not None else tokenhub.load_accounts()
    known = set((daily.get("advertisers") or {}).keys())
    for aid in authorized_advertiser_ids(acc):
        if aid not in known:
            live.add(aid)
    return live


def account_signature(accounts=None):
    """
    账号集合指纹：已授权投放户 ID 集合的稳定 hash。

    ⚠ 区间创意缓存必须用它做失效判据，光比数据期(start/end)会漏掉一类关键变化：
    用户「后来新授权了主体」时数据期没变（还是昨天结尾），但账号集合变了。
    只比数据期 → 缓存被复用 → 新授权主体的数据在 近3日/近7日/本月/上月 视图里
    永远看不到（上月视图更是整个月都不更新）。加了指纹后账号集合一变就重拉。
    """
    ids = authorized_advertiser_ids(accounts)
    sig = hashlib.md5(",".join(sorted(ids)).encode("utf-8")).hexdigest()[:12]
    return sig, ids


def collect_account_daily(start_date, end_date, verbose=True, refresh_first=True):
    """
    拉全部已授权投放户的日粒度账户数据 → 落盘 data/account_daily.json。
    成本：1 请求/户（近一年也就 348 行/户，1 页拿完）。
    """
    t0 = time.time()
    # 聚光接口不接受 end_date = 今天（返回 10002「结束时间不能为今天」），这里兜底夹到昨天
    today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    if end_date >= today:
        end_date = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")
    if start_date > end_date:
        start_date = end_date
    if refresh_first:
        tokenhub.ensure_valid(verbose=False, dry_run=False)
    accounts = tokenhub.load_accounts()

    tasks, seen = [], set()
    for key, rec in accounts.items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        for adv in rec.get("advertisers", []):
            aid = adv.get("advertiser_id")
            if aid is None or str(aid) in seen:
                continue
            seen.add(str(aid))
            tasks.append((tok, aid, key, rec.get("user_id") or "",
                          adv.get("advertiser_name") or f"广告主{aid}"))

    n_workers = max(1, int(os.environ.get("JUGUANG_CONCURRENCY", "4")))

    def one(t):
        tok, aid, key, uid, name = t
        rows, meta = fetch_account_daily(tok, aid, start_date, end_date)
        return aid, key, uid, name, rows, meta

    store, failed = {}, []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for fut in as_completed([pool.submit(one, t) for t in tasks]):
            try:
                aid, key, uid, name, rows, meta = fut.result()
            except Exception as e:
                failed.append({"err": str(e)[:80]})
                continue
            if not meta.get("ok"):
                failed.append({"advertiser_id": aid, "name": name,
                               "code": meta.get("code"), "msg": meta.get("msg")})
                continue
            # 拉成功了但一行都没有 = 「已知零消耗」。显式记一条空记录，
            # 这样下游才能把「已知零消耗」和「状态未知（没拉到/新授权）」区分开。
            store[str(aid)] = {
                "name": name,
                "owner_key": key,
                "owner_user_id": uid,
                "city": infer_city(name),
                "days": rows,
                "empty": not rows,
            }

    # ---- 合并写：不再整体覆盖 ----
    # 整体覆盖有个致命副作用：某户本轮拉取失败（或它的账号 token 暂时失效被跳过），
    # 它就立刻从**所有**区间视图里消失，下一轮成功才回来 —— 表现为「有时候看得到、
    # 有时候看不到」。这里把「仍在账号库里、但本轮没拿到」的户沿用上一轮数据兜底。
    # 已从账号库移除的户不再保留（避免数据永久滞留）。
    listed_all = authorized_advertiser_ids(accounts, require_valid_token=False)
    prev_advs = (load_account_daily() or {}).get("advertisers") or {}
    carried = 0
    for aid, info in prev_advs.items():
        if aid in store or aid not in listed_all:
            continue
        store[aid] = info
        carried += 1

    # token 失效的账号：其名下投放户**永远**拉不到数据，必须让用户看见并重新授权
    bad_tok = []
    for _k, rec in accounts.items():
        _tok = (rec.get("access_token") or "").strip()
        if not _tok or len(_tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            bad_tok.append({
                "key": _k,
                "n": len(rec.get("advertisers") or []),
                "advertisers": [str(a.get("advertiser_id"))
                                for a in (rec.get("advertisers") or [])],
            })

    payload = {
        "pulled_at": int(time.time()),
        "range": {"start": start_date, "end": end_date},
        "advertisers": store,
        "failed": failed,
        "invalid_token_accounts": bad_tok,
        "account_sig": account_signature(accounts)[0],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DAILY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if verbose:
        n_live = sum(1 for v in store.values() if not v.get("empty"))
        print(f"  日粒度缓存：{n_live} 户有数据 / {len(store)} 户在册"
              f"（沿用旧数据 {carried} 户），{len(failed)} 户失败，"
              f"{len(bad_tok)} 个账号 token 失效需重新授权，"
              f"耗时 {payload['elapsed_sec']}s → {DAILY_CACHE_FILE}")
    return payload


def load_account_daily():
    try:
        with open(DAILY_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============ 区间创意明细（让区间视图也能看到素材与封面）============
#
# 背景：账户维度（1 行/天/户）拿不到素材，所以长区间只能看到「投放户」。
# 但短中区间（≤31 天）的创意维度行数是可控的（实测单户：7 日 255 行、本月 ~515 行、
# 上月 1265 行，page_size=500 时分别只要 1/2/3 页），所以这些区间完全可以拉到
# 真正的素材明细（含封面、笔记 ID、单元名），做区间素材榜。

CREATIVE_CACHE_DIR = os.path.join(DATA_DIR, "range_creative")

# 能走创意明细的区间（超过 31 天的区间，行数会到万级，改用账户粒度）
CREATIVE_MAX_DAYS = 31


def collect_range_creative(key, start_date, end_date, verbose=True,
                           refresh_first=False, force=False):
    """
    拉某个区间的创意维度明细（跨天按创意 ID 聚合）→ data/range_creative/<key>.json。
    key 是区间标识（3d / 7d / month ...），一个区间一个缓存文件。

    复用条件 = 数据期(start/end)一致 **且** 账号集合指纹一致。
    只看数据期是不行的：用户后来新授权主体时数据期没变，但账号集合变了 ——
    缓存被复用 → 新授权主体的数据在这些区间视图里永远看不到。
    """
    t0 = time.time()
    # 聚光接口不接受 end_date = 今天
    today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    if end_date >= today:
        end_date = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")
    if start_date > end_date:
        start_date = end_date

    if refresh_first:
        tokenhub.ensure_valid(verbose=False, dry_run=False)

    accounts = tokenhub.load_accounts()
    sig, auth_ids = account_signature(accounts)

    if not force:
        old = load_range_creative(key)
        if (old and (old.get("advertisers") or {})
                and (old.get("range") or {}).get("end") == end_date
                and (old.get("range") or {}).get("start") == start_date):
            # 老缓存没有 account_sig 字段（历史版本）→ 视为账号集合已变，重拉一次补齐
            if old.get("account_sig") == sig:
                if verbose:
                    print(f"  区间创意明细[{key}]：数据期与账号集合均未变，复用缓存")
                return old
            if verbose:
                print(f"  区间创意明细[{key}]：账号集合已变化"
                      f"（在册 {len(auth_ids)} 户 / 缓存 {len(set((old.get('advertisers') or {}).keys()))} 户）"
                      f"→ 强制重拉")

    # 用日粒度账户缓存先筛一遍：区间内零消耗的户不可能有创意明细，
    # 不必发请求。实测可把请求数从 116 降到 ~25，耗时从 66s 降到 ~12s。
    # 三道保护，避免筛过头丢数据：
    #   ① 只有当日粒度缓存的覆盖范围确实包含本区间时，才敢用它筛；
    #   ② 日粒度拉取失败的户状态未知，一律保留；
    #   ③ 日缓存里**完全没有记录**的户（刚授权、日缓存还没轮到）状态未知，一律保留。
    live_ids = None
    daily = load_account_daily()
    if daily and (daily.get("advertisers") or {}):
        dr = daily.get("range") or {}
        covered = (dr.get("start") and dr.get("end")
                   and dr["start"] <= start_date and dr["end"] >= end_date)
        if covered:
            live_ids = set()
            known = set(daily["advertisers"].keys())
            for aid, info in daily["advertisers"].items():
                for dt, vals in (info.get("days") or {}).items():
                    if not (start_date <= dt <= end_date):
                        continue
                    if any(float(vals.get(f) or 0) > 0 for f in
                           ("fee", "impression", "message_consult", "msg_leads_num")):
                        live_ids.add(str(aid))
                        break
            for x in (daily.get("failed") or []):
                if x.get("advertiser_id") is not None:
                    live_ids.add(str(x["advertiser_id"]))
            n_unknown = 0
            for aid in (auth_ids or set()):
                if aid not in known:
                    live_ids.add(aid)
                    n_unknown += 1
            if verbose:
                print(f"  区间创意明细：按日粒度缓存筛出 {len(live_ids)} 个在投户"
                      + (f"（含 {n_unknown} 个状态未知的新户）" if n_unknown else ""))

    tasks, seen = [], set()
    for k, rec in accounts.items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        for adv in rec.get("advertisers", []):
            aid = adv.get("advertiser_id")
            if aid is None or str(aid) in seen:
                continue
            seen.add(str(aid))
            if live_ids is not None and str(aid) not in live_ids:
                continue                      # 该区间内没有消耗，跳过
            tasks.append((tok, aid, k, rec.get("user_id") or "",
                          adv.get("advertiser_name") or f"广告主{aid}"))

    # 区间创意明细的请求数比账户维度多（每户 1~2 页），并发开高一点
    n_workers = max(1, int(os.environ.get("JUGUANG_CONCURRENCY_CREATIVE", "6")))

    def one(t):
        tok, aid, k, uid, name = t
        creatives, meta = jgapi.fetch_creatives(tok, aid, start_date, end_date)
        return aid, k, uid, name, creatives, meta

    store, failed = {}, []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for fut in as_completed([pool.submit(one, t) for t in tasks]):
            try:
                aid, k, uid, name, creatives, meta = fut.result()
            except Exception as e:
                failed.append({"err": str(e)[:80]})
                continue
            if not meta.get("ok"):
                failed.append({"advertiser_id": aid, "name": name,
                               "code": meta.get("code"), "msg": meta.get("msg")})
                continue
            if not creatives:
                continue
            store[str(aid)] = {
                "name": name,
                "owner_key": k,
                "owner_user_id": uid,
                "city": infer_city(name),
                "creatives": creatives,
            }

    payload = {
        "key": key,
        "pulled_at": int(time.time()),
        "range": {"start": start_date, "end": end_date},
        "advertisers": store,
        "failed": failed,
        "account_sig": sig,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    os.makedirs(CREATIVE_CACHE_DIR, exist_ok=True)
    with open(os.path.join(CREATIVE_CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if verbose:
        n_cr = sum(len(v["creatives"]) for v in store.values())
        n_zero = len(failed) + max(0, len(tasks) - len(store) - len(failed))
        print(f"  区间创意明细[{key}]：{len(store)} 户 / {n_cr} 条素材，"
              f"{len(failed)} 户失败，{n_zero} 户无投放，耗时 {payload['elapsed_sec']}s")
    return payload


def load_range_creative(key):
    try:
        with open(os.path.join(CREATIVE_CACHE_DIR, key + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_COVER_CACHE = {"at": 0.0, "data": {}}


def representative_covers(max_age=600):
    """
    {advertiser_id: {img, name, note_id}} —— 每个投放户最近一次有效素材的封面。
    用途：长区间只能显示到「投放户」粒度，这里给它配一张代表封面，避免封面列全空。
    """
    now = time.time()
    if _COVER_CACHE["data"] and now - _COVER_CACHE["at"] < max_age:
        return _COVER_CACHE["data"]

    out = {}
    days = sorted(d for d in os.listdir(SNAPSHOT_DIR)
                  if len(d) == 15 and d.endswith(".json")) if os.path.isdir(SNAPSHOT_DIR) else []
    for d in reversed(days):                      # 从最近一天往回找
        try:
            with open(os.path.join(SNAPSHOT_DIR, d), encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue
        for aid, adv in (snap.get("advertisers") or {}).items():
            if aid in out:
                continue
            best = None
            for c in adv.get("creatives", []):
                img = c.get("creativity_image")
                if not img:
                    continue
                fee = float(c.get("fee") or 0)
                if best is None or fee > best[0]:
                    best = (fee, img, c.get("creativity_name") or "",
                            c.get("note_id") or "")
            if best:
                out[aid] = {"img": best[1], "name": best[2], "note_id": best[3]}
    _COVER_CACHE.update(at=now, data=out)
    return out


def snapshot_from_range_creative(key, label, base=None):
    """
    用区间创意明细生成素材粒度快照（granularity="creative"），看板因此
    在区间视图下也能显示封面 / 素材名 / 效率标签。

    base 传 snapshot_from_daily 的结果：它的 KPI 是「账户口径」，这里把
    summary 换成「创意口径」（= 素材榜合计），并把账户口径值一并留在
    summary.account_fee 里，供页面标注两个口径的差异。
    """
    p = load_range_creative(key)
    if not p:
        return None
    advs = p.get("advertisers") or {}
    if not advs:
        return None

    kb = tokenhub.load_accounts()
    label_cache = {k: owner_label(k, rec) for k, rec in kb.items()}

    advertisers, city_summary = {}, defaultdict(
        lambda: {"fee": 0.0, "msg": 0, "msg_leads": 0, "creative_count": 0})
    total_fee = total_msg = total_ml = 0

    for aid, info in advs.items():
        creatives = info.get("creatives") or []
        if not creatives:
            continue
        name = info.get("name") or f"广告主{aid}"
        city = info.get("city") or infer_city(name)
        advertisers[str(aid)] = {
            "city": city, "name": name, "creatives": creatives,
            "owner_key": info.get("owner_key") or "",
            "owner_label": label_cache.get(info.get("owner_key") or "", ""),
            "owner_user_id": info.get("owner_user_id") or "",
        }
        fee = sum(float(c.get("fee") or 0) for c in creatives)
        msg = int(sum(float(c.get("message_consult") or 0) for c in creatives))
        ml = int(sum(float(c.get("msg_leads_num") or 0) for c in creatives))
        cs = city_summary[city]
        cs["fee"] += fee
        cs["msg"] += msg
        cs["msg_leads"] += ml
        cs["creative_count"] += len(creatives)
        total_fee += fee
        total_msg += msg
        total_ml += ml

    base = base or {}
    bsum = base.get("summary") or {}
    return {
        "date": label,
        "range": p.get("range"),
        "granularity": "creative",
        "generated_at": int(time.time()),
        "source": "range_creative",
        "trend": base.get("trend") or [],
        "summary": {
            "total_fee": total_fee, "total_msg": total_msg,
            "total_msg_leads": total_ml, "cities": dict(city_summary),
            "advertiser_count": len(advertisers),
            "advertiser_ok": len(advertisers), "advertiser_empty": 0,
            "advertiser_error": len(p.get("failed") or []),
            "advertiser_skipped": 0, "error_list": [],
            "account_fee": bsum.get("total_fee"),        # 账户口径（对比用）
            "account_msg": bsum.get("total_msg"),
            "account_ml": bsum.get("total_msg_leads"),
            "days_covered": bsum.get("days_covered"),
            "creative_pulled_at": p.get("pulled_at"),
        },
        "advertisers": advertisers,
    }


def snapshot_from_daily(start_date, end_date, label, daily=None):
    """
    用日粒度缓存聚合出任意区间的快照 —— 本地计算，秒出，不再请求 API。
    粒度是「投放户」（不是素材），因为账户维度报表本身就没有素材明细。
    """
    daily = daily or load_account_daily()
    if not daily:
        return None

    # 账号名用账号库推导（日粒度缓存里没有广告主列表，不能直接用 owner_label）
    kb = tokenhub.load_accounts()
    label_cache = {}
    for k, rec in kb.items():
        label_cache[k] = owner_label(k, rec)

    advs = daily.get("advertisers") or {}
    covers = representative_covers()
    advertisers, city_summary = {}, defaultdict(
        lambda: {"fee": 0.0, "msg": 0, "msg_leads": 0, "creative_count": 0})
    trend = defaultdict(lambda: {"fee": 0.0, "imp": 0.0, "clk": 0.0,
                                 "msg": 0.0, "ml": 0.0})
    total_fee = total_msg = total_ml = 0
    ok = 0
    days_hit = set()

    for aid, info in advs.items():
        acc = {f: 0.0 for f in DAILY_FIELDS}
        hit = False
        for dt, vals in (info.get("days") or {}).items():
            if dt < start_date or dt > end_date:
                continue
            hit = True
            days_hit.add(dt)
            for f in DAILY_FIELDS:
                acc[f] += float(vals.get(f) or 0)
            tr = trend[dt]
            tr["fee"] += float(vals.get("fee") or 0)
            tr["imp"] += float(vals.get("impression") or 0)
            tr["clk"] += float(vals.get("click") or 0)
            tr["msg"] += float(vals.get("message_consult") or 0)
            tr["ml"] += float(vals.get("msg_leads_num") or 0)
        if not hit:
            continue
        fee = acc.get("fee", 0.0)
        msg = int(acc.get("message_consult", 0.0))
        ml = int(acc.get("msg_leads_num", 0.0))
        if fee <= 0 and msg <= 0 and ml <= 0:
            continue
        ok += 1
        name = info.get("name") or f"广告主{aid}"
        city = info.get("city") or infer_city(name)
        # 用一条「伪创意」承载该户在区间内的汇总，让下游口径统一。
        # 配一张代表封面（该户最近一次有效素材的封面），避免长区间封面列全空。
        cov = covers.get(str(aid)) or {}
        pseudo = {
            "creativity_id": f"adv_{aid}", "creativity_name": name,
            "creativity_image": cov.get("img") or "",
            "note_id": cov.get("note_id") or "",
            "note_user_id": "",
            "unit_name": "", "campaign_name": "",
            "fee": fee, "impression": acc.get("impression", 0.0),
            "click": acc.get("click", 0.0), "message_consult": msg,
            "msg_leads_num": ml, "like": acc.get("like", 0.0),
            "comment": acc.get("comment", 0.0), "collect": acc.get("collect", 0.0),
            "share": acc.get("share", 0.0), "follow": acc.get("follow", 0.0),
            "interaction": acc.get("interaction", 0.0),
            "video_play_5s_cnt": acc.get("video_play_5s_cnt", 0.0),
            "is_pseudo": True,
            "cover_from": cov.get("name", "")[:40] if cov else "",
        }
        advertisers[str(aid)] = {
            "city": city, "name": name, "creatives": [pseudo],
            "owner_key": info.get("owner_key") or "",
            "owner_label": label_cache.get(info.get("owner_key") or "", ""),
            "owner_user_id": info.get("owner_user_id") or "",
        }
        cs = city_summary[city]
        cs["fee"] += fee
        cs["msg"] += msg
        cs["msg_leads"] += ml
        cs["creative_count"] += 1
        total_fee += fee
        total_msg += msg
        total_ml += ml

    trend_list = [{"date": d, **v} for d, v in sorted(trend.items())]
    return {
        "date": label,
        "range": {"start": start_date, "end": end_date},
        "granularity": "advertiser",       # 区间视图是账户粒度，没有素材明细
        "generated_at": int(time.time()),
        "source": "account_daily",
        "trend": trend_list,
        "summary": {
            "total_fee": total_fee, "total_msg": total_msg,
            "total_msg_leads": total_ml, "cities": dict(city_summary),
            "advertiser_count": len(advertisers),
            "advertiser_ok": ok, "advertiser_empty": 0,
            "advertiser_error": len((daily.get("failed") or [])),
            "advertiser_skipped": 0, "error_list": [],
            "days_covered": len(days_hit),
            "daily_pulled_at": daily.get("pulled_at"),
        },
        "advertisers": advertisers,
    }


def collect(label, start_date, end_date, verbose=True, refresh_first=True):
    """拉取区间数据 → 生成并保存快照 → 返回 (snapshot, report)"""
    t0 = time.time()
    if verbose:
        print(f"\n{'=' * 60}")
        print(f"采集 {label}  ({start_date} ~ {end_date})")
        print(f"{'=' * 60}")

    auth_report = None
    if refresh_first:
        auth_report = tokenhub.ensure_valid(verbose=verbose, dry_run=False)
        if verbose:
            print()

    accounts = tokenhub.load_accounts()
    advertisers = {}
    city_summary = defaultdict(lambda: {"fee": 0.0, "msg": 0, "msg_leads": 0, "creative_count": 0})
    stats = {"ok": 0, "empty": 0, "error": 0, "skipped_unauth": 0, "total_adv": 0}
    errors = []
    seen = set()

    # 先统计一下有多少户是可拉取的
    for rec in accounts.values():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            stats["skipped_unauth"] += len(rec.get("advertisers", []))
            continue
        stats["total_adv"] += len(rec.get("advertisers", []))

    if verbose:
        print(f"可拉取投放户 {stats['total_adv']} 个"
              f"（跳过未授权户 {stats['skipped_unauth']} 个）\n")

    # ---------- 组织任务：按「已授权主体 → 其名下投放户」展开，按广告主去重 ----------
    tasks = []
    idx = 0
    for key, rec in accounts.items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        o_label = owner_label(key, rec)
        o_uid = rec.get("user_id") or ""
        for adv in rec.get("advertisers", []):
            aid = adv.get("advertiser_id")
            if aid is None or str(aid) in seen:
                continue
            seen.add(str(aid))
            idx += 1
            tasks.append((idx, key, o_label, o_uid, tok, adv))

    n_workers = max(1, int(os.environ.get("JUGUANG_CONCURRENCY", "4")))
    if verbose:
        print(f"并发 {n_workers} 线程拉取 {len(tasks)} 个投放户"
              f"（限流间隔 {jgapi.MIN_INTERVAL}s）\n")

    def fetch_one(task):
        i, key, o_label, o_uid, tok, adv = task
        aid = adv.get("advertiser_id")
        name = adv.get("advertiser_name") or f"广告主{aid}"
        city = infer_city(name)
        t1 = time.time()
        creatives, meta = jgapi.fetch_creatives(tok, aid, start_date, end_date)
        return {
            "idx": i, "key": key, "o_label": o_label, "o_uid": o_uid,
            "aid": aid, "name": name, "city": city,
            "creatives": creatives, "meta": meta,
            "sec": round(time.time() - t1, 2),
        }

    results = []
    print_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(fetch_one, t) for t in tasks]
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as e:                       # 单户异常不影响整轮
                continue
            results.append(r)
            if verbose:
                with print_lock:
                    if not r["meta"]["ok"]:
                        print(f"  [{r['idx']:3d}] ✗ {r['name'][:26]:28s} "
                              f"{r['meta'].get('code')} {r['meta'].get('msg')}")
                    else:
                        mark = "·" if r["creatives"] else "○"
                        fee = sum(c.get("fee", 0.0) for c in r["creatives"])
                        msg = int(sum(c.get("message_consult", 0.0) for c in r["creatives"]))
                        print(f"  [{r['idx']:3d}] {mark} {r['name'][:26]:28s} {r['city']:4s} "
                              f"{len(r['creatives']):4d}条 ¥{fee:>9,.0f} 私信{msg:>3d}")

    # ---------- 按任务序号归集（保证快照顺序稳定）----------
    total_fee = total_msg = total_msg_leads = 0
    for r in sorted(results, key=lambda x: x["idx"]):
        meta = r["meta"]
        if not meta["ok"]:
            stats["error"] += 1
            errors.append({
                "advertiser_id": r["aid"], "name": r["name"], "city": r["city"],
                "code": meta.get("code"), "msg": meta.get("msg"),
                "hint": meta.get("hint", ""),
            })
            continue

        creatives = r["creatives"]
        fee = sum(c.get("fee", 0.0) for c in creatives)
        msg = int(sum(c.get("message_consult", 0.0) for c in creatives))
        ml = int(sum(c.get("msg_leads_num", 0.0) for c in creatives))
        stats["ok" if creatives else "empty"] += 1

        advertisers[str(r["aid"])] = {
            "city": r["city"], "name": r["name"], "creatives": creatives,
            "owner_key": r["key"], "owner_label": r["o_label"], "owner_user_id": r["o_uid"],
        }
        cs = city_summary[r["city"]]
        cs["fee"] += fee
        cs["msg"] += msg
        cs["msg_leads"] += ml
        cs["creative_count"] += len(creatives)
        total_fee += fee
        total_msg += msg
        total_msg_leads += ml

    snapshot = {
        "date": label,
        "range": {"start": start_date, "end": end_date},
        "generated_at": int(time.time()),
        "summary": {
            "total_fee": total_fee,
            "total_msg": total_msg,
            "total_msg_leads": total_msg_leads,
            "cities": dict(city_summary),
            "advertiser_count": len(advertisers),
            # 新增：覆盖率与健康度，旧版缺这些导致「静默空日报」
            "advertiser_ok": stats["ok"],
            "advertiser_empty": stats["empty"],
            "advertiser_error": stats["error"],
            "advertiser_skipped": stats["skipped_unauth"],
            "error_list": errors,
        },
        "advertisers": advertisers,
    }

    # 保存快照
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snap_file = os.path.join(SNAPSHOT_DIR, f"{label}.json")
    with open(snap_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    with open(os.path.join(SNAPSHOT_DIR, "最新.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    report = {
        "label": label,
        "range": {"start": start_date, "end": end_date},
        "finished_at": int(time.time()),
        "elapsed_sec": round(time.time() - t0, 1),
        "concurrency": n_workers,
        "throttle_sec": jgapi.MIN_INTERVAL,
        "stats": stats,
        "total_fee": total_fee,
        "total_msg": total_msg,
        "total_msg_leads": total_msg_leads,
        "errors": errors,
        "auth": auth_report,
        "snapshot_file": snap_file,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(COLLECT_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"\n{'─' * 60}")
        print(f"  可拉取 {stats['total_adv']:3d} 户 → 有数据 {stats['ok']:3d} | "
              f"零消耗 {stats['empty']:3d} | 失败 {stats['error']:3d}")
        print(f"  合计 ¥{total_fee:,.2f}  私信 {total_msg}  留资 {total_msg_leads}")
        print(f"  耗时 {report['elapsed_sec']}s（{n_workers} 线程并发）")
        print(f"  快照 → {snap_file}")
        print(f"{'─' * 60}")
    return snapshot, report


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else \
        datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")
    end = sys.argv[2] if len(sys.argv) > 2 else start
    label = sys.argv[3] if len(sys.argv) > 3 else start
    collect(label, start, end)
