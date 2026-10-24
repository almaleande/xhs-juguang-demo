#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
授权流程
=========
把「auth_code → token → 匹配主体 → 验证 → 入库」串成一次调用。
看板服务、命令行工具都复用它。

为什么 auth_code 必须立刻用：一次性、有效期只有几分钟，
人工复制再转手很容易失效。所以入口做得越短越好。
"""
import os
import sys
import time

try:
    from . import jgapi, tokenhub
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi, tokenhub

APP_ID_HINT = "10231"
SCOPE = ["report_service", "ad_query", "ad_manage", "account_manage"]


def auth_url(app_id=None):
    """生成授权链接（回调固定为 httpbin.org，方便用户从地址栏取 auth_code）"""
    aid = app_id or tokenhub.load_secret().get("app_id", APP_ID_HINT)
    sc = "%5B" + "%2C".join(f"%22{s}%22" for s in SCOPE) + "%5D"
    return (f"https://ad-market.xiaohongshu.com/auth?appId={aid}"
            f"&scope={sc}&redirectUri=https://httpbin.org/get&state=abcd")


def extract_code(raw):
    """从整条回调 URL 或纯 auth_code 里取出 code"""
    s = (raw or "").strip()
    if not s:
        return None
    if "auth_code=" in s:
        seg = s.split("auth_code=", 1)[1]
        for sep in ("&", "#", " ", '"', "\n"):
            seg = seg.split(sep)[0]
        return seg.strip() or None
    # 纯 code：只允许字母数字、下划线、连字符
    if s and all(c.isalnum() or c in "_-" for c in s) and len(s) >= 16:
        return s
    return None


def match_key(accounts, corp, user_id, adv_ids):
    """
    把新授权的账号对回账号库里已有的条目（避免重复新增）与重复写入。

    ⚠ 这里曾出过一次事故：第 3 步兜底只比对 corporation_name，
    而库里 18 个条目的 corporation_name 全是同一个营业执照名
    （`夏天婚纱摄影总部`），导致**新授权的主体被写进了
    第一个"待修复"条目、把该条目原有的投放户覆盖掉**（青岛 12 个户丢失）。

    教训：**身份匹配必须有正向证据**（user_id 或投放户重叠），
    兜底绝不允许匹配到"有数据"的条目。
    """
    adv_set = {str(a) for a in (adv_ids or [])}
    # 1) user_id 精确匹配最可靠
    if user_id:
        for k, rec in accounts.items():
            if (rec.get("user_id") or "").strip() == user_id:
                return k, "user_id 匹配"
    # 2) 投放户重叠度最高者
    best, best_hit, best_n = None, 0, 0
    for k, rec in accounts.items():
        ids = {str(a.get("advertiser_id")) for a in (rec.get("advertisers") or [])}
        hit = len(ids & adv_set)
        if hit > best_hit or (hit == best_hit and hit > 0 and len(ids) < best_n):
            if hit > 0:
                best, best_hit, best_n = k, hit, len(ids)
    if best:
        return best, f"投放户重叠 {best_hit} 个"
    # 3) 兜底：同主体名 + 当前不可用 + **且该条目没有任何投放户**（没有数据可丢）
    if corp:
        for k, rec in accounts.items():
            if (rec.get("corporation_name") == corp
                    and not (rec.get("advertisers") or [])
                    and tokenhub.classify(rec) in ("pending", "placeholder", "expired")):
                return k, "同主体空条目（无投放户，可安全复用）"
    return None, "新主体（新建条目）"


def verify(token, advertisers, days=3, sample=3):
    """拉一点真实数据，确认这个 token 真能用"""
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * max(1, days)))
    checked, rows_total, errs = 0, 0, []
    for a in (advertisers or [])[:sample]:
        aid = a.get("advertiser_id")
        creatives, meta = jgapi.fetch_creatives(token, aid, start, end, page_size=20)
        checked += 1
        if not meta.get("ok"):
            errs.append(f"[{aid}] {meta.get('code')} {meta.get('msg')}")
        else:
            rows_total += len(creatives)
    return {"checked": checked, "creatives": rows_total, "errors": errs,
            "range": f"{start} ~ {end}", "ok": checked > 0 and not errs}


def authorize(auth_code_raw, label="", do_verify=True):
    """
    一站式授权。
    返回 {ok, msg, corp, key, match, advertisers, verified, token_preview}
    """
    code = extract_code(auth_code_raw)
    if not code:
        return {"ok": False, "msg": "没能从输入里识别出 auth_code，请粘贴完整回调地址或纯 code"}

    secret = tokenhub.load_secret()
    resp = jgapi.exchange_auth_code(secret["app_id"], secret["app_secret"], code)
    if not resp.get("success"):
        c = resp.get("code")
        semantics, hint = jgapi.describe(c)
        extra = "（auth_code 一次性且时效极短，请重新授权后立刻提交）" if str(c) == "410014" else ""
        return {"ok": False, "msg": f"换取失败：{semantics} {hint}{extra}", "code": c}

    data = resp["data"]
    corp = data.get("corporation_name", "未知主体")
    uid = (data.get("user_id") or "").strip()
    advs = data.get("approval_advertisers") or []

    v = None
    if do_verify and advs:
        v = verify(data["access_token"], advs)

    accounts = tokenhub.load_accounts()
    key, how = match_key(accounts, corp, uid, [a.get("advertiser_id") for a in advs])

    old = accounts.get(key) if key else None

    # 投放户列表做「并集」，绝不覆盖 —— 覆盖过一次，丢了 12 个青岛投放户
    merged, seen_ids = [], set()
    for a in ((old or {}).get("advertisers") or []) + list(advs):
        aid = a.get("advertiser_id")
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        merged.append(a)
    added = len(merged) - len((old or {}).get("advertisers") or []) if old else 0
    dropped = len((old or {}).get("advertisers") or []) + len(advs) - len(merged) if old else 0

    rec = {
        "corporation_name": corp,
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "access_token_expires_in": data.get("access_token_expires_in", 86399),
        "refresh_token_expires_in": data.get("refresh_token_expires_in", 2591999),
        "created_at": data.get("created_at", int(time.time())),
        "advertiser_ids": [a["advertiser_id"] for a in merged],
        "advertisers": merged,
    }
    for f in ("user_id", "scope"):
        if data.get(f):
            rec[f] = data[f]
    if old:
        # 保留历史备注
        for f in ("refresh_history", "note"):
            if old.get(f):
                rec[f] = old[f]
        if added or dropped:
            notes = []
            if added:
                notes.append(f"本次授权新增 {added} 个投放户")
            if dropped:
                notes.append(f"与设备记录重合 {dropped} 个")
            prev = rec.get("note")
            rec["note"] = (f"{prev}｜" if prev else "") + "；".join(notes)

    if key is None:
        from core.collector import owner_label
        key = f"{corp}" + (f"_{label}" if label else "")
        n = 1
        base = key
        while key in accounts:
            n += 1
            key = f"{base}_{n}"

    accounts[key] = rec
    saved = tokenhub.save_accounts(accounts, f"看板授权-{corp}")

    tok = data["access_token"]
    return {
        "ok": True,
        "msg": "授权成功" + ("，数据验证通过" if (v and v["ok"]) else
                            ("，但验证未通过" if v and not v["ok"] else "")),
        "corp": corp,
        "user_id": uid,
        "key": key,
        "match": how,
        "advertisers": advs,
        "verified": v,
        "saved": saved,
        "token_preview": f"{tok[:6]}...{tok[-4:]}(len={len(tok)})",
        "classify": tokenhub.classify(rec),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python core/authflow.py <auth_code 或回调URL> [备注]")
        print("\n授权链接：\n" + auth_url())
        sys.exit(0)
    r = authorize(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2))
