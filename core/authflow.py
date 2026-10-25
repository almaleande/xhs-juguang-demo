#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演示版授权边界
=============
保留账号总览所需的接口形状；授权入口固定禁用，不接收或保存真实凭证。
"""
import os
import sys
import time

try:
    from . import jgapi, tokenhub
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi, tokenhub

APP_ID_HINT = "10001"
SCOPE = ["report_service", "ad_query", "ad_manage", "account_manage"]


def auth_url(app_id=None):
    """演示版不生成真实授权链接。"""
    return "#"


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
    （`示例婚纱摄影总部`），导致**新授权的主体被写进了
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
    """演示版拒绝授权，不解析授权码、不访问上游、不保存凭证。"""
    from core.privacy import blocked
    return blocked()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python core/authflow.py <auth_code 或回调URL> [备注]")
        print("\n授权链接：\n" + auth_url())
        sys.exit(0)
    r = authorize(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2))
