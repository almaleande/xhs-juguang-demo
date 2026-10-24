#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 生命周期管理
===================
职责：判定每个主体 token 的健康状态 → 该续期的自动续期 → 续不了的说清楚原因。

这正是原项目最致命的缺口：`月报生成.py` 直接拿 access_token 就用，
既不检查是否过期，也不续期，而续期代码只存在于归档的旧版脚本里。

状态语义
--------
pending      从未授权（access/refresh 都为空）
placeholder  token 被占位符污染（如 '***' / 'cf2d22...6454'），必须重新授权
valid        有效
expiring     access 已超龄但 refresh 仍在有效期 → 可自动续期
refreshable  access 为空但 refresh 可用 → 可自动续期
expired      refresh 也过期 → 只能重新授权
"""
import json
import os
import sys
import time

try:
    from . import jgapi
    from .paths import (ACCOUNTS_FILE, AUTH_STATE_FILE, DATA_DIR, SECRET_FILE)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import jgapi
    from core.paths import (ACCOUNTS_FILE, AUTH_STATE_FILE, DATA_DIR, SECRET_FILE)

# 占位符判定：真 token 长度通常 32+；这里放宽到 20 以兼容未来格式
MIN_REAL_TOKEN_LEN = 20
REFRESH_THRESHOLD = 0.8  # access 用掉 80% 寿命就该续期了
RT_REFRESH_THRESHOLD = 0.8  # refresh_token 用掉 80%（≈24天）就主动滚动，避免 30 天悬崖

_NEED_AUTH = ("pending", "placeholder", "expired")


def load_secret():
    with open(SECRET_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return {}
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        print(f"  ✗ 账号库损坏: {e}")
        return {}


def save_accounts(accounts, remark=""):
    """走 vault 的安全写入（备份 → 写临时 → 校验 → 原子替换）"""
    vault = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vault")
    if vault not in sys.path:
        sys.path.insert(0, vault)
    try:
        from juguang_token_vault import safe_write
        return safe_write(accounts, remark)
    except Exception as e:
        print(f"  ✗ vault 不可用({e})，回退直写")
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ACCOUNTS_FILE)
        return True


def classify(rec):
    """判定单个主体的状态"""
    at = (rec.get("access_token") or "").strip()
    rt = (rec.get("refresh_token") or "").strip()
    created = rec.get("created_at") or 0
    at_ttl = rec.get("access_token_expires_in") or 86399
    rt_ttl = rec.get("refresh_token_expires_in") or 2591999
    now = time.time()
    age = now - created if created else float("inf")

    if not at and not rt:
        return "pending"
    if not at and rt:
        if len(rt) < MIN_REAL_TOKEN_LEN:
            return "placeholder"
        return "refreshable" if age < rt_ttl else "expired"
    if len(at) < MIN_REAL_TOKEN_LEN:
        return "placeholder"
    if age < at_ttl * REFRESH_THRESHOLD:
        return "valid"
    return "expiring" if age < rt_ttl else "expired"


def remaining_hours(rec):
    """access_token 还剩多少小时"""
    created = rec.get("created_at") or 0
    ttl = rec.get("access_token_expires_in") or 86399
    if not created:
        return 0.0
    return max(0.0, (created + ttl - time.time()) / 3600)


def refresh_one(rec, secret, verbose=True):
    """续期单个主体，就地更新 rec"""
    rt = (rec.get("refresh_token") or "").strip()
    if not rt:
        return False, "无 refresh_token"
    resp = jgapi.refresh_token(secret["app_id"], secret["app_secret"], rt)
    if not resp.get("success"):
        code = resp.get("code")
        semantics, hint = jgapi.describe(code)
        return False, f"{semantics}（{hint}）" if hint else semantics

    d = resp["data"]
    rec["access_token"] = d["access_token"]
    rec["refresh_token"] = d.get("refresh_token") or rt
    rec["access_token_expires_in"] = d.get("access_token_expires_in", 86399)
    rec["refresh_token_expires_in"] = d.get("refresh_token_expires_in", 2591999)
    rec["created_at"] = d.get("created_at", int(time.time()))
    rec.setdefault("refresh_history", []).append(int(time.time()))
    if verbose:
        print(f"      ✓ 续期成功，新 token 有效期 {remaining_hours(rec):.1f}h")
    return True, "续期成功"


def ensure_valid(verbose=True, dry_run=False):
    """
    遍历所有主体：该续期的续期，续不了的分门别类。
    返回报告 dict。
    """
    accounts = load_accounts()
    secret = load_secret()
    report = {
        "checked_at": int(time.time()),
        "total": len(accounts),
        "valid": [], "refreshed": [], "failed": [],
        "need_reauth": [], "pending": [],
    }

    if verbose:
        print(f"账号库共 {len(accounts)} 个主体\n")

    changed = False
    now = time.time()
    for key, rec in accounts.items():
        corp = rec.get("corporation_name", key)
        label = key if key != corp else corp
        st = classify(rec)

        if st == "valid":
            # 防御性兜底：access 还在有效期内时，若 refresh_token 已用掉 80% 寿命
            # 也主动滚动一次，给 30 天悬崖留出余量（正常情况下 access 先过期、本就会续期，
            # 这里只防「某次刷新没正确重置 rt 窗口」这类边界，不改变主流程）。
            rt_ttl = rec.get("refresh_token_expires_in") or 2591999
            rt_age = now - (rec.get("created_at") or 0)
            if 0 < rt_age < rt_ttl * RT_REFRESH_THRESHOLD:
                report["valid"].append(label)
                if verbose:
                    print(f"  ✓ {label[:34]:36s} 有效（rt 剩 {int((rt_ttl - rt_age) / 86400)} 天）")
                continue
            st = "expiring"  # 落到续期分支，滚动 rt

        if st in ("expiring", "refreshable"):
            if verbose:
                print(f"  ↻ {label[:34]:36s} 状态={st}，尝试续期…")
            if dry_run:
                report["refreshed"].append(label)
                continue
            ok, msg = refresh_one(rec, secret, verbose)
            if ok:
                changed = True
                report["refreshed"].append(label)
            else:
                report["failed"].append({"key": label, "reason": msg})
                report["need_reauth"].append(label)
                if verbose:
                    print(f"      ✗ {msg}")
            continue

        if st == "pending":
            report["pending"].append(label)
            if verbose:
                print(f"  ○ {label[:34]:36s} 未授权，token 为空")
            continue

        # placeholder / expired
        report["need_reauth"].append(label)
        if verbose:
            tag = "占位符污染" if st == "placeholder" else "refresh_token 已过期"
            print(f"  ✗ {label[:34]:36s} {tag}，需重新授权")

    if changed and not dry_run:
        save_accounts(accounts, "tokenhub自动续期")
        if verbose:
            print(f"\n💾 已保存续期结果")

    # 写台账
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(AUTH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if verbose:
        print(f"\n{'─' * 58}")
        print(f"  有效 {len(report['valid'])} | 已续期 {len(report['refreshed'])} | "
              f"需重授权 {len(report['need_reauth'])} | 未授权 {len(report['pending'])}")
        print(f"{'─' * 58}")
    return report


def pending_list():
    """给用户看的「还需要授权哪些主体」清单"""
    accounts = load_accounts()
    rows = []
    for key, rec in accounts.items():
        st = classify(rec)
        if st in _NEED_AUTH:
            rows.append({
                "key": key,
                "corporation": rec.get("corporation_name", ""),
                "status": st,
                "advertisers": len(rec.get("advertisers", [])),
                "user_id": rec.get("user_id", ""),
            })
    rows.sort(key=lambda r: -r["advertisers"])
    return rows


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        ensure_valid(verbose=True, dry_run=True)
    elif mode == "refresh":
        ensure_valid(verbose=True)
    elif mode == "pending":
        rows = pending_list()
        print(f"待授权主体 {len(rows)} 个：")
        for r in rows:
            print(f"  [{r['status']:11s}] {r['key'][:36]:38s} {r['advertisers']:2d} 户")
