#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号档案
=========
把账号库里难以阅读的主体键（`示例婚纱摄影总部_780577`）
翻译成人能看懂的门店账号名，并关联到对应的登录手机号。

要解决的问题：**授权了哪些小红书账号，一眼看不出来** ——
所有键的前缀都是同一个营业执照名，后缀又是 ID，完全对不上号。

别名来源优先级：
  1. 主体键的中文后缀（如 `_东营`、`_美学中心`）
  2. 该主体下广告主名称里能识别出的城市（最常见者）
  3. user_id 尾号兜底
"""
import csv
import os
import sys

try:
    from . import tokenhub
    from .collector import infer_city, owner_label
    from .paths import BASE_DIR
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import tokenhub
    from core.collector import infer_city, owner_label
    from core.paths import BASE_DIR

LOGIN_CSV_CANDIDATES = [
    os.path.join(BASE_DIR, "_archive", "accounts2.csv"),
    os.path.join(BASE_DIR, "accounts2.csv"),
]

STATUS_TEXT = {
    "valid": "已授权",
    "expiring": "已授权·待续期",
    "refreshable": "已授权·待续期",
    "pending": "未授权",
    "placeholder": "token 已失效",
    "expired": "已过期",
}


def load_login_accounts():
    """读登录账号清单（列：小红书名称 / 账号 / 密码）"""
    from core.privacy import DEMO_MODE
    if DEMO_MODE:
        return [], None
    for p in LOGIN_CSV_CANDIDATES:
        if not os.path.exists(p):
            continue
        try:
            raw = open(p, "rb").read()
            text = None
            for enc in ("gbk", "utf-8-sig", "utf-8"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                continue
            rows = []
            for r in csv.reader(text.splitlines()):
                if len(r) >= 2 and r[0].strip() and r[1].strip():
                    if r[0].strip() in ("小红书名称", "名称"):
                        continue
                    rows.append({"name": r[0].strip(), "account": r[1].strip()})
            return rows, p
        except Exception:
            continue
    return [], None


def mask_account(s):
    """手机号或邮箱脱敏"""
    s = (s or "").strip()
    if s.isdigit() and len(s) >= 7:
        return f"{s[:3]}****{s[-4:]}"
    if "@" in s:
        a, _, d = s.partition("@")
        return (a[:3] + "***@" + d) if len(a) > 3 else ("***@" + d)
    return s


# 业务线特征词 → 区分后缀。同城有多个账号时，靠它把「滨州·1 / 滨州·2」变成「滨州·主号 / 滨州·婚纱照」
BIZ_HINTS = [
    ("婚前影像", "婚前影像"),
    ("微电影", "微电影"),
    ("直营店", "直营店"),
    ("客片分享", "客片分享"),
    ("真实客片", "真实客片"),
    ("客片集", "客片集"),
    ("海景", "海景"),
    ("外景", "外景"),
    ("美墅", "美墅"),
    ("大师系列", "大师系列"),
    ("大师", "大师系列"),
    ("民族风", "民族风"),
    ("婚纱照", "婚纱照"),
]


def biz_hint(names):
    """从广告主名里提取业务线特征（同城多账号时的区分依据之一）"""
    blob = " ".join(names or [])
    for kw, hint in BIZ_HINTS:
        if kw in blob:
            return hint
    return ""


def hint_from_login(name, city):
    """
    登录账号名 → 业务线后缀。如「滨州婚纱照示例婚纱摄影」→「婚纱照」，
    「滨州示例婚纱摄影」→ 空（说明它是该城市的主号）。
    """
    s = (name or "").strip()
    if city and s.startswith(city):
        s = s[len(city):]
    s = s.replace("示例", "").strip(" -_·－·")
    # 去掉尾部通用词：滨州婚纱照示例婚纱摄影 → 婚纱照
    for w in ("婚纱摄影", "婚纱照", "摄影", "婚纱"):
        if s.endswith(w) and len(s) > len(w):
            s = s[: -len(w)].strip(" -_·－·")
            break
    if s in ("", "婚纱摄影", "摄影", "婚纱"):
        return ""
    return s


def match_login(adv_names, label, rows):
    """
    匹配该主体对应的登录账号。
    优先用「登录账号名被广告主名包含」的关系（能精确到具体账号，
    如 `滨州婚纱照示例婚纱摄影ZX` 命中 `滨州婚纱照示例婚纱摄影`），
    精确匹配不到再退回按门店别名模糊匹配。
    """
    if not rows:
        return []
    hits, seen = [], set()
    for r in rows:
        nm = r["name"]
        if nm and any(nm in (n or "") for n in (adv_names or [])):
            if nm not in seen:
                seen.add(nm)
                hits.append(r)
    if hits:
        hits.sort(key=lambda x: -len(x["name"]))
        return hits[:3]

    if not label:
        return []
    core = label.replace("示例", "").strip()
    for r in rows:
        nm = r["name"]
        if (label in nm) or (core and core in nm):
            if nm not in seen:
                seen.add(nm)
                hits.append(r)
    return hits[:3]


def resolve_label(key, rec):
    """
    给主体定一个「好认且唯一」的门店名。
    优先级：主体键中文后缀 > 总店矩阵 > 城市 > user_id 尾号
    """
    if "_" in key:
        suf = key.rsplit("_", 1)[1].strip()
        if suf and not suf.isdigit():
            return suf

    names = [a.get("advertiser_name", "") for a in (rec.get("advertisers") or [])]
    # 含多个「x-总店」且户数多 → 跨城市的总号矩阵，别硬套某个城市
    if sum(1 for n in names if "总店" in n) >= 2 and len(names) >= 5:
        return "总号矩阵"

    tally = {}
    for n in names:
        c = infer_city(n)
        if c != "其他":
            tally[c] = tally.get(c, 0) + 1
    if tally:
        top, cnt = max(tally.items(), key=lambda x: x[1])
        total = sum(tally.values())
        if len(tally) == 1 or cnt / total >= 0.6:
            return top
        return f"{top}等{len(tally)}城"

    uid = (rec.get("user_id") or "").strip()
    return f"账号{uid[-4:]}" if uid else "未识别"


def build():
    accounts = tokenhub.load_accounts()
    rows, src = load_login_accounts()

    raw = []
    for key, rec in accounts.items():
        lb = resolve_label(key, rec)
        advs = rec.get("advertisers") or []
        names = [a.get("advertiser_name", "") for a in advs]
        hits = match_login(names, lb, rows)
        hint = ""
        if len(hits) == 1:
            hint = hint_from_login(hits[0]["name"], lb)
        if not hint:
            hint = biz_hint(names)
        raw.append({"key": key, "rec": rec, "base": lb, "hint": hint, "hits": hits})

    # 同名主体用业务线后缀区分：滨州·主号 / 滨州·婚纱照，而不是滨州·1 / 滨州·2
    groups = {}
    for it in raw:
        groups.setdefault(it["base"], []).append(it)
    for lb, items in groups.items():
        if len(items) == 1:
            items[0]["label"] = lb
            continue
        blanks = sum(1 for it in items if not it["hint"])
        for it in items:
            h = it["hint"] or ("主号" if blanks == 1 else "")
            it["label"] = f"{lb}·{h}" if h else lb

    # 兜底：后缀仍然撞车的，加序号
    from collections import Counter
    dup = Counter(it["label"] for it in raw)
    seq = {}
    for it in raw:
        if dup[it["label"]] > 1:
            seq[it["label"]] = seq.get(it["label"], 0) + 1
            it["label"] = f"{it['label']}·{seq[it['label']]}"

    out = []
    for it in raw:
        rec, key = it["rec"], it["key"]
        st = tokenhub.classify(rec)
        advs = rec.get("advertisers") or []
        authorized = st in ("valid", "expiring", "refreshable")
        login = [{"name": h["name"], "account": mask_account(h["account"])} for h in it["hits"]]
        note = ""
        if not login:
            note = "登录清单中无此账号，需确认用哪个号授权" if not authorized else ""
        out.append({
            "key": key,
            "label": it["label"],
            "baseLabel": it["base"],
            "status": st,
            "statusText": STATUS_TEXT.get(st, st),
            "authorized": authorized,
            "advertisers": len(advs),
            "advertiserNames": [a.get("advertiser_name", "") for a in advs][:10],
            "remainingHours": (round(tokenhub.remaining_hours(rec), 1)
                               if authorized else None),
            "createdAt": rec.get("created_at") or 0,
            "login": login,
            "loginNote": note,
        })
    out.sort(key=lambda x: (not x["authorized"], -x["advertisers"]))
    return {
        "accounts": out,
        "total": len(out),
        "authorized": sum(1 for x in out if x["authorized"]),
        "pending": sum(1 for x in out if not x["authorized"]),
        "loginSource": os.path.basename(src) if src else None,
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    d = build()
    print(f"账号授权总览：已授权 {d['authorized']} / 共 {d['total']}"
          f"（登录清单来源：{d['loginSource']}）\n")
    print(f"{'账号':<12s} {'状态':<14s} {'投放户':>5s}  {'剩余':>7s}  投放户样本")
    print("-" * 96)
    for a in d["accounts"]:
        rem = f"{a['remainingHours']}h" if a["remainingHours"] is not None else "—"
        sample = "、".join(a["advertiserNames"][:2])[:36]
        print(f"{a['label']:<14s} {a['statusText']:<14s} {a['advertisers']:>5d}  {rem:>7s}  {sample}")
        for lg in a["login"]:
            print(f"{'':14s} ↳ 登录账号：{lg['name']}（{lg['account']}）")
        if a.get("loginNote"):
            print(f"{'':14s} ↳ {a['loginNote']}")
