#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据质量闸门
=============
旧版为什么能连出 59 天「总消耗 ¥0」的日报还没人发现？
因为日报里的告警条件写成「有投放户 且 城市数为 0」，而实际场景是
「有户、有城市、值全 0」—— 恰好绕过告警。

这里把判定改对：区分「真没投放」和「拉取失败」，并做环比突变检测。
fatal 级别的问题会阻断日报生成，避免再产出误导性数据。
"""
import os
import sys

try:
    from .paths import SNAPSHOT_DIR
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.paths import SNAPSHOT_DIR

FATAL = "fatal"
WARN = "warn"
INFO = "info"

LEVEL_LABEL = {FATAL: "阻断", WARN: "警告", INFO: "提示"}


def load_previous(label, snapshot_dir=None):
    """读上一份日快照（按文件名倒序找比 label 早的那份）"""
    import glob
    import json
    snap_dir = snapshot_dir or SNAPSHOT_DIR
    files = sorted(glob.glob(os.path.join(snap_dir, "*.json")))
    cands = []
    for f in files:
        base = os.path.basename(f).replace(".json", "")
        if len(base) == 10 and base < label:
            cands.append(base)
    if not cands:
        return None
    try:
        with open(os.path.join(snap_dir, f"{cands[-1]}.json"), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return None


def check(snapshot, prev_snapshot=None):
    """返回问题清单 [{'level','msg'}]"""
    issues = []
    s = (snapshot or {}).get("summary", {}) or {}

    total_adv = s.get("advertiser_count", 0)
    ok = s.get("advertiser_ok", 0)
    empty = s.get("advertiser_empty", 0)
    err = s.get("advertiser_error", 0)
    skipped = s.get("advertiser_skipped", 0)
    fee = s.get("total_fee", 0) or 0
    msg = s.get("total_msg", 0) or 0

    pullable = ok + empty + err

    if pullable == 0:
        issues.append({"level": FATAL, "msg":
                       f"本轮未能拉取任何投放户"
                       + (f"（另有 {skipped} 户因未授权被跳过）" if skipped else "")
                       + " —— 请先检查授权状态"})
    else:
        ratio = err / pullable
        if ratio > 0.5:
            issues.append({"level": FATAL, "msg":
                           f"{err}/{pullable} 户拉取失败（{ratio:.0%}），超过半数，数据不可信"})
        elif err > 0:
            issues.append({"level": WARN, "msg": f"{err}/{pullable} 户拉取失败，详见失败清单"})

        if fee == 0:
            if ok == 0 and empty == pullable:
                issues.append({"level": WARN, "msg":
                               f"全部 {empty} 户零消耗。若连续多日如此，需核查投放是否停摆"})
            elif ok > 0:
                issues.append({"level": FATAL, "msg":
                               f"{ok} 户有创意数据但总消耗为 0，疑似聚合或字段异常"})

        if msg == 0 and fee > 0:
            issues.append({"level": WARN, "msg": "有消耗但私信数为 0，需确认口径"})

    if skipped > 0:
        issues.append({"level": WARN, "msg":
                       f"{skipped} 个投放户因未授权被跳过，本轮日报不完整"})

    if prev_snapshot:
        ps = prev_snapshot.get("summary", {}) or {}
        pf = ps.get("total_fee", 0) or 0
        if pf > 100:
            if fee == 0:
                issues.append({"level": WARN, "msg":
                               f"消耗从上期 ¥{pf:,.0f} 骤降到 0"})
            elif fee < pf * 0.3:
                issues.append({"level": WARN, "msg":
                               f"消耗环比骤降 {100 * (1 - fee / pf):.0f}%"
                               f"（¥{pf:,.0f} → ¥{fee:,.0f}）"})

    return issues


def has_fatal(issues):
    return any(i["level"] == FATAL for i in issues)


def format_issues(issues):
    if not issues:
        return "  ✓ 数据质量检查通过"
    lines = []
    for i in issues:
        icon = {"fatal": "✗", "warn": "!", "info": "·"}.get(i["level"], "·")
        lines.append(f"  {icon} [{LEVEL_LABEL.get(i['level'], i['level'])}] {i['msg']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    label = sys.argv[1] if len(sys.argv) > 1 else "最新"
    path = os.path.join(SNAPSHOT_DIR, f"{label}.json")
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    prev = load_previous(snap.get("date", label))
    its = check(snap, prev)
    print(f"检查 {path}")
    print(format_issues(its))
    print(f"\n是否阻断日报: {'是' if has_fatal(its) else '否'}")
