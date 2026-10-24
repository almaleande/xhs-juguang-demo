#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
笔记元信息：标题 / 首图 / 跳转链接
====================================
问题背景：创意维度报表（/report/offline/creative）里只有投放内部代号
（如 7.10全站+spu+180开口_复制_20260913），运营看不懂、也无法把表格行
对应到真实笔记。笔记维度报表（/report/offline/note）则返回真实的
note_title / note_image / note_jump_url。

本模块负责：
  1. 并发拉取各投放户的笔记维度报表，按 note_id 去重落盘 data/note_titles.json
  2. 提供 load() / titles() 给看板生成器读取
  3. 拉取失败（如应用级限流 410029）时保留旧数据，并按冷却时间退避，不反复打接口

用法:
  python -m core.notemeta          # 拉最近 7 天
  from core import notemeta; notemeta.titles()
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import jgapi, tokenhub  # noqa: E402
from core.paths import DATA_DIR  # noqa: E402

NOTE_PATH = "/api/open/jg/data/report/offline/note"
CACHE_FILE = os.path.join(DATA_DIR, "note_titles.json")

REFRESH_INTERVAL = 6 * 3600     # 成功后 6 小时内不重复拉
RETRY_COOLDOWN = 3600           # 失败后 1 小时内不重试（限流 410029 是应用级窗口）

# 笔记维度字段名在不同版本接口里可能不同，这里做多候选兜底
_TITLE_KEYS = ("note_title", "note_name", "title", "noteTitle")
_IMAGE_KEYS = ("note_image", "note_cover", "noteImage", "cover", "image")
_URL_KEYS = ("note_jump_url", "note_url", "jump_url", "noteJumpUrl")


def _first(row, keys):
    for k in keys:
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def load():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("notes", {})
            return d
    except Exception:
        pass
    return {"fetched_at": 0, "last_try": 0, "last_error": "", "notes": {}}


def titles():
    """{note_id: {"title":…, "image":…, "url":…}}"""
    return load().get("notes") or {}


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def _fetch_one(tok, aid):
    out = {}
    page = 1
    while True:
        resp = jgapi.post(NOTE_PATH, {
            "advertiser_id": aid,
            "start_date": _START, "end_date": _END,
            "page": page, "page_size": 1000,
        }, token=tok)
        if not resp.get("success"):
            # 首页就失败 → 整户失败；后续页失败 → 保留已拿到数据
            if page == 1:
                return aid, out, {"code": resp.get("code"), "msg": resp.get("msg", "")}
            break
        data = resp.get("data") or {}
        rows = data.get("data_list") or []
        if not rows:
            break
        for r in rows:
            nid = str(r.get("note_id") or r.get("noteId") or "").strip()
            if not nid:
                continue
            rec = out.setdefault(nid, {})
            for key, keys in (("title", _TITLE_KEYS), ("image", _IMAGE_KEYS),
                              ("url", _URL_KEYS)):
                v = _first(r, keys)
                if v and not rec.get(key):
                    rec[key] = v
        total = data.get("total_count") or 0
        # 翻页到底：单页不足上限，或累计已覆盖 total_count
        if len(rows) < 1000 or (total and page * 1000 >= total):
            break
        page += 1
        if page > 50:   # 安全阀，避免异常分页死循环
            break
    return aid, out, {"ok": True}


_START = _END = ""


def fetch(days=30, verbose=True, force=False, refresh_first=False):
    """
    拉取笔记维度并合并落盘。返回 (notes, info)。

    force=False 时：
      - 距上次成功 < REFRESH_INTERVAL → 直接复用缓存
      - 上次失败距今 < RETRY_COOLDOWN → 跳过，不再打接口
    """
    global _START, _END
    cache = load()
    now = time.time()

    if not force and cache.get("fetched_at"):
        age = now - float(cache["fetched_at"])
        if age < REFRESH_INTERVAL:
            return cache["notes"], {"skipped": True,
                                    "reason": f"缓存 {age / 60:.0f} 分钟内，复用"}
    if not force and cache.get("last_error") and cache.get("last_try"):
        gap = now - float(cache["last_try"])
        if gap < RETRY_COOLDOWN:
            return cache["notes"], {"skipped": True,
                                    "reason": f"上次失败 {gap / 60:.0f} 分钟内，冷却中"}

    if refresh_first:
        try:
            tokenhub.ensure_valid(verbose=False, dry_run=False)
        except Exception:
            pass

    end = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    start = time.strftime("%Y-%m-%d", time.localtime(now - 86400 * max(1, days)))
    _START, _END = start, end

    tasks, seen = [], set()
    for _k, rec in tokenhub.load_accounts().items():
        tok = (rec.get("access_token") or "").strip()
        if not tok or len(tok) < tokenhub.MIN_REAL_TOKEN_LEN:
            continue
        for adv in rec.get("advertisers", []):
            aid = adv.get("advertiser_id")
            if aid is None or str(aid) in seen:
                continue
            seen.add(str(aid))
            tasks.append((tok, aid))

    merged = dict(cache.get("notes") or {})
    failed = []
    n_workers = max(1, int(os.environ.get("JUGUANG_CONCURRENCY_NOTE", "4")))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(_fetch_one, t[0], t[1]) for t in tasks]
        for fut in as_completed(futs):
            try:
                aid, out, meta = fut.result()
            except Exception as e:
                failed.append({"err": str(e)[:80]})
                continue
            if not meta.get("ok"):
                failed.append({"advertiser_id": aid, "code": meta.get("code"),
                               "msg": meta.get("msg", "")})
                continue
            for nid, rec in out.items():
                tgt = merged.setdefault(nid, {})
                for key in ("title", "image", "url"):
                    if rec.get(key) and not tgt.get(key):
                        tgt[key] = rec[key]

    info = {"tasks": len(tasks), "failed": len(failed), "notes": len(merged),
            "range": f"{start} ~ {end}", "skipped": False}
    if failed and not merged:
        # 全军覆没（典型是应用级限流）：保留旧数据，只记失败时间
        cache["last_try"] = now
        cache["last_error"] = str(failed[0])[:160]
        _save(cache)
        info["error"] = cache["last_error"]
        if verbose:
            print(f"  笔记维度拉取失败（已保留旧数据）：{cache['last_error']}")
        return cache.get("notes") or {}, info

    cache["notes"] = merged
    cache["fetched_at"] = now
    cache["last_try"] = now
    if failed:
        cache["last_error"] = f"{len(failed)}/{len(tasks)} 户失败"
    else:
        cache["last_error"] = ""
    _save(cache)
    if verbose:
        print(f"  笔记维度：{len(merged)} 条笔记标题"
              + (f"（{len(failed)}/{len(tasks)} 户失败）" if failed else ""))
    return merged, info


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="拉取小红书笔记标题/首图")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    notes, info = fetch(days=args.days, force=args.force)
    print(json.dumps(info, ensure_ascii=False))
    for nid, rec in list(notes.items())[:8]:
        print(f"  {nid}  {rec.get('title', '(无标题)')[:40]}")
    print(f"  合计 {len(notes)} 条")
