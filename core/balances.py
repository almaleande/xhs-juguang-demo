# -*- coding: utf-8 -*-
"""
投放账户余额采集
================
聚光开放平台「获取账号余额接口」：
  POST /api/open/jg/account/balance/info
  body: {"advertiser_id": <int>}
  → data: total_balance / available_balance / cash_balance /
          return_balance / freeze_balance / credit_balance /
          compensate_return_balance / today_spend

注意：该接口与离线报表不同——是**实时**接口，且按 advertiser_id 逐户查询。
全部户约 165 个，故：
  - 缓存到 data/balances.json（避免每次页面加载打 165 次）；
  - 由看板「刷新余额」按钮或定时任务触发 collect_all 更新。
  - 单位：接口返回整数。离线报表 fee 用「元」，但 SDK 文档标注余额用「分」。
    当前按「元」展示并显式标注，待与聚光后台核对；若为分，将读数÷100 即可。

实测（2026-09-22）：adapi.xiaohongshu.com 上该接口**可用**，GET 404、POST 成功。
"""
import os
import sys
import json
import time

try:
    from . import jgapi, tokenhub
    from .paths import DATA_DIR
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi, tokenhub
    from core.paths import DATA_DIR

BALANCE_PATH = "/api/open/jg/account/balance/info"
BALANCE_FILE = os.path.join(DATA_DIR, "balances.json")

# 余额接口返回字段（顺序即展示优先级）
FIELDS = [
    "total_balance", "available_balance", "cash_balance", "return_balance",
    "freeze_balance", "credit_balance", "compensate_return_balance", "today_spend",
]


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_one(token, advertiser_id):
    """查单个投放户余额。失败/无数据返回 None。"""
    resp = jgapi.post(BALANCE_PATH, {"advertiser_id": int(advertiser_id)},
                      token=token, timeout=20)
    if not resp.get("success"):
        return None
    d = resp.get("data") or {}
    if not d:
        return None
    out = {f: _to_float(d.get(f)) / 100.0 for f in FIELDS}  # 接口金额单位为「分」，换算为「元」
    out["ok"] = True
    return out


def collect_all(verbose=False):
    """
    遍历所有主体 × 其下投放户，逐户拉余额。
    返回 {str(advertiser_id): {字段..., "fetched_at": int, "ok": True}}。
    token 失效的主体（refreshable）会自动续期一次；无法续期的跳过。
    """
    accounts = tokenhub.load_accounts()
    try:
        secret = tokenhub.load_secret()
    except Exception:
        secret = None
    out = {}
    n_with = 0
    refreshed = False

    for key, rec in accounts.items():
        st = tokenhub.classify(rec)
        token = rec.get("access_token")
        if st in ("pending", "placeholder", "expired") or not token:
            continue
        if st == "refreshable":
            if secret and tokenhub.refresh_one(rec, secret, verbose=False)[0]:
                token = rec.get("access_token")
                refreshed = True
            else:
                continue
        for adv in (rec.get("advertisers") or []):
            aid = adv.get("advertiser_id")
            if aid is None:
                continue
            b = fetch_one(token, aid)
            if b:
                b["fetched_at"] = int(time.time())
                out[str(aid)] = b
                if any(b.get(f) for f in FIELDS):
                    n_with += 1
            if verbose:
                print(f"  {key[:22]:24s} 户 {aid}: {b}")

    if verbose:
        print(f"余额采集完成：{len(out)} 户，其中 {n_with} 户有余额")
    if refreshed:
        try:
            tokenhub.save_accounts(accounts, "余额采集自动续期")
        except Exception:
            pass
    return out


def load_balances():
    if os.path.exists(BALANCE_FILE):
        try:
            return json.load(open(BALANCE_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_balances(b):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BALANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=2)


def attach(snapshot, balances=None):
    """把余额挂到 snapshot['advertisers'][aid]['balance']，供看板渲染。"""
    if balances is None:
        balances = load_balances()
    if not balances:
        return snapshot
    for aid, adv in (snapshot.get("advertisers") or {}).items():
        b = balances.get(str(aid))
        if b:
            adv["balance"] = b
    return snapshot


def summary(balances=None):
    """给接口/状态用的概览。"""
    if balances is None:
        balances = load_balances()
    n = len(balances)
    n_with = sum(1 for b in balances.values() if any(b.get(f) for f in FIELDS))
    total_avail = sum(_to_float(b.get("available_balance")) for b in balances.values())
    total = sum(_to_float(b.get("total_balance")) for b in balances.values())
    ts = max((b.get("fetched_at", 0) for b in balances.values()), default=0)
    return {"n": n, "n_with_balance": n_with,
            "total_available": round(total_avail, 2),
            "total_balance": round(total, 2),
            "fetched_at": ts}
