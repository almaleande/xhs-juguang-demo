#!/usr/bin/env python3
"""
小红书聚光 Token Vault — Token安全层
=========================================
安全读写 juguang_accounts.json，自动备份、版本回滚、健康监控。

用法:
  python3 juguang_token_vault.py status         # 查看所有token健康状态
  python3 juguang_token_vault.py backup [备注]   # 手动创建还原点
  python3 juguang_token_vault.py list-backups   # 列出所有备份
  python3 juguang_token_vault.py restore <时间戳> # 回滚到指定备份
  python3 juguang_token_vault.py clean [天数]    # 清理旧备份(默认90天)

作为模块导入:
  from vault.juguang_token_vault import safe_write, safe_read, create_backup
"""

import json
import os
import sys
import time
import glob
import shutil
import hashlib

# ====== 路径配置 ======
# 以本文件位置为基准推导项目根，Windows / WSL 两端都能正确解析
_VAULT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.environ.get("JUGUANG_DIR") or os.path.dirname(_VAULT_DIR)
ACCOUNTS_FILE = os.path.join(WORK_DIR, "juguang_accounts.json")
VAULT_DIR = os.path.join(WORK_DIR, "vault")
BACKUP_DIR = os.path.join(VAULT_DIR, "backups")
BACKUP_PREFIX = "juguang_accounts."
BACKUP_EXT = ".json"
DEFAULT_RETENTION_DAYS = 90

os.makedirs(BACKUP_DIR, exist_ok=True)


# ====== 备份管理 ======

def _backup_timestamp():
    """返回格式化的时间戳字符串"""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _backup_path(ts):
    """根据时间戳构造备份文件路径"""
    return os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{ts}{BACKUP_EXT}")


def _last_backup_time():
    """获取最近一次备份的时间戳，没有则返回None"""
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*{BACKUP_EXT}")))
    if not backups:
        return None
    return os.path.getmtime(backups[-1])


def _content_hash(content):
    """对字典内容做md5（忽略key顺序）"""
    return hashlib.md5(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_backup(remark=""):
    """
    创建 juguang_accounts.json 的时间戳备份。
    如果内容跟上一次备份完全一致则跳过（60秒内去重）。
    返回备份文件名，或 None（跳过）。
    """
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"  ⚠️ 备份跳过: {ACCOUNTS_FILE} 不存在")
        return None

    # 读当前内容
    with open(ACCOUNTS_FILE, encoding='utf-8') as f:
        current = json.load(f)
    current_hash = _content_hash(current)

    # 去重：如果上次备份在60秒内且内容一致则跳过
    last_time = _last_backup_time()
    if last_time and (time.time() - last_time) < 60:
        last_backup = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*{BACKUP_EXT}")))[-1]
        try:
            with open(last_backup, encoding='utf-8') as f:
                last_content = json.load(f)
            if _content_hash(last_content) == current_hash:
                return None  # 内容没变，跳过
        except (json.JSONDecodeError, IndexError):
            pass

    # 写入备份
    ts = _backup_timestamp()
    dst = _backup_path(ts)
    shutil.copy2(ACCOUNTS_FILE, dst)

    # 写备注文件
    if remark:
        note_file = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{ts}.note")
        with open(note_file, 'w', encoding='utf-8') as f:
            f.write(remark.strip())

    return ts


def list_backups():
    """返回备份列表，按时间倒序"""
    files = sorted(
        glob.glob(os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*{BACKUP_EXT}")),
        reverse=True
    )
    results = []
    for fp in files:
        basename = os.path.basename(fp)
        ts = basename.replace(BACKUP_PREFIX, "").replace(BACKUP_EXT, "")
        size = os.path.getsize(fp)
        mtime = os.path.getmtime(fp)
        # 读备注
        note_file = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{ts}.note")
        remark = ""
        if os.path.exists(note_file):
            with open(note_file, encoding='utf-8') as f:
                remark = f.read().strip()
        results.append({
            "timestamp": ts,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
            "size": size,
            "size_str": f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB",
            "remark": remark,
        })
    return results


def restore_backup(ts):
    """从指定时间戳备份恢复 juguang_accounts.json"""
    src = _backup_path(ts)
    if not os.path.exists(src):
        print(f"❌ 备份不存在: {src}")
        return False

    # 恢复前先备份当前文件（抢救性质）
    if os.path.exists(ACCOUNTS_FILE):
        rescue_ts = _backup_timestamp()
        rescue_dst = _backup_path(f"{rescue_ts}_before_restore")
        shutil.copy2(ACCOUNTS_FILE, rescue_dst)
        print(f"  💾 当前文件已抢救备份: {os.path.basename(rescue_dst)}")

    shutil.copy2(src, ACCOUNTS_FILE)
    print(f"✅ 已恢复: {ts}")
    return True


def clean_backups(retention_days=DEFAULT_RETENTION_DAYS):
    """清理超过 retention_days 天的备份"""
    now = time.time()
    cutoff = now - retention_days * 86400
    deleted = 0

    for fp in glob.glob(os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}*{BACKUP_EXT}")):
        if os.path.getmtime(fp) < cutoff:
            os.remove(fp)
            deleted += 1
            # 删除对应的备注文件
            basename = os.path.basename(fp)
            ts = basename.replace(BACKUP_PREFIX, "").replace(BACKUP_EXT, "")
            note_file = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{ts}.note")
            if os.path.exists(note_file):
                os.remove(note_file)

    print(f"🧹 已清理 {deleted} 份过期备份（>{retention_days}天）")
    return deleted


# ====== 安全读写 ======

def safe_read():
    """
    安全读取 juguang_accounts.json。
    如果文件损坏/不存在，返回空dict。
    """
    if not os.path.exists(ACCOUNTS_FILE):
        return {}

    try:
        with open(ACCOUNTS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"  ⚠️ {ACCOUNTS_FILE} 格式异常（不是dict），返回空")
            return {}
        return data
    except json.JSONDecodeError as e:
        print(f"  ❌ {ACCOUNTS_FILE} 损坏: {e}")
        # 尝试从备份恢复
        backups = list_backups()
        if backups:
            latest = backups[0]["timestamp"]
            print(f"  🔄 尝试从最近备份恢复: {latest}")
            restore_backup(latest)
            # 重读
            with open(ACCOUNTS_FILE, encoding='utf-8') as f:
                return json.load(f)
        return {}


def safe_write(accounts, remark=""):
    """
    安全写入 juguang_accounts.json。
    步骤: 备份 → 写.tmp → 校验JSON → rename覆盖。
    """
    if not isinstance(accounts, dict):
        print(f"  ❌ safe_write 拒绝: 数据不是dict类型")
        return False

    # 1. 备份现有文件
    create_backup(remark)

    # 2. 写临时文件
    tmp_file = ACCOUNTS_FILE + ".tmp"
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  ❌ 写临时文件失败: {e}")
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        return False

    # 3. 校验临时文件
    try:
        with open(tmp_file, encoding='utf-8') as f:
            verified = json.load(f)
        if not isinstance(verified, dict):
            raise ValueError("写入结果不是dict")
    except Exception as e:
        print(f"  ❌ 校验临时文件失败: {e}")
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        return False

    # 4. 原子覆盖
    os.replace(tmp_file, ACCOUNTS_FILE)
    return True


# ====== 健康监控 ======

def token_status():
    """分析 juguang_accounts.json 中的所有token状态"""
    accounts = safe_read()
    if not accounts:
        return {"valid": 0, "empty_access": 0, "expired_access": 0, "no_refresh": 0, "total": 0}

    now = time.time()
    result = {
        "total": len(accounts),
        "valid": 0,       # access_token 有效或在24h内可自动续期
        "empty_access": 0, # access_token 为空，但有 refresh_token 可续
        "empty_all": 0,    # 两个token都为空
        "warning": [],     # 快过期的主体列表
        "critical": [],    # 已过期需要重新授权的主体列表
    }

    for key, acct in accounts.items():
        corp = acct.get("corporation_name", key)
        at = acct.get("access_token", "")
        rt = acct.get("refresh_token", "")
        created = acct.get("created_at", 0)
        at_expires = acct.get("access_token_expires_in", 86400)
        rt_expires = acct.get("refresh_token_expires_in", 2592000)
        elapsed = now - created

        adv_count = len(acct.get("advertisers", []))

        if at:
            # access_token 存在
            if elapsed < at_expires * 0.9:
                result["valid"] += 1
            elif elapsed < rt_expires:
                result["valid"] += 1  # refresh_token 还能续
                days_left = (rt_expires - elapsed) / 86400
                result["warning"].append(f"{corp}: access_token将过期, refresh还有{days_left:.0f}天")
            else:
                result["critical"].append(f"{corp}: refresh_token过期({adv_count}户), 需重新授权")
        elif rt:
            # access_token 为空但有 refresh_token
            if elapsed < rt_expires:
                result["empty_access"] += 1
                days_left = (rt_expires - elapsed) / 86400
                result["warning"].append(f"{corp}: access_token为空, 可自动续期({days_left:.0f}天)")
            else:
                result["critical"].append(f"{corp}: refresh_token过期({adv_count}户), 需重新授权")
                result["empty_all"] += 1
        else:
            result["critical"].append(f"{corp}: 两个token都为空({adv_count}户)")
            result["empty_all"] += 1

    result["accounts"] = accounts
    return result


def print_status(status):
    """格式化输出健康状态"""
    total = status["total"]
    print(f"\n📊 Token Vault 状态")
    print(f"{'━' * 50}")
    print(f"📁 主体总数: {total}")

    # 账号统计
    adv_count = sum(len(a.get("advertisers", [])) for a in status.get("accounts", {}).values())
    print(f"   投放户数: {adv_count}")

    # 文件大小
    if os.path.exists(ACCOUNTS_FILE):
        size_kb = os.path.getsize(ACCOUNTS_FILE) / 1024
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(ACCOUNTS_FILE)))
        print(f"   文件大小: {size_kb:.1f}KB")
        print(f"   最后修改: {mtime}")

    print()
    print(f"✅  access_token 有效:             {status['valid']}")
    print(f"⚠️  access_token 为空(有refresh):   {status['empty_access']}")
    print(f"❌  需要重新授权:                   {status['empty_all']}")

    if status["warning"]:
        print(f"\n⚠️ 预警:")
        for w in status["warning"]:
            print(f"  • {w}")

    if status["critical"]:
        print(f"\n🔴 需处理:")
        for c in status["critical"]:
            print(f"  • {c}")

    # 备份统计
    backups = list_backups()
    print(f"\n💾 备份: {len(backups)} 份")
    if backups:
        print(f"   最新: {backups[0]['time_str']} ({backups[0]['size_str']})")
        print(f"   最旧: {backups[-1]['time_str']} ({backups[-1]['size_str']})")
    print(f"{'━' * 50}")
    print()


# ====== CLI ======

def cli():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        s = token_status()
        print_status(s)

    elif cmd == "backup":
        remark = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        ts = create_backup(remark)
        if ts:
            print(f"✅ 备份完成: {ts}")
        else:
            print(f"⏭️ 跳过（内容无变化或60秒内已备份）")

    elif cmd == "list-backups":
        backups = list_backups()
        if not backups:
            print("📭 暂无备份")
            return
        print(f"\n💾 备份列表（共 {len(backups)} 份）")
        print(f"{'━' * 60}")
        for i, b in enumerate(backups, 1):
            remark = f" — {b['remark']}" if b['remark'] else ""
            print(f"  #{i:<3} {b['timestamp']}  {b['time_str']}  {b['size_str']}{remark}")
        print(f"{'━' * 60}")

    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("用法: python3 juguang_token_vault.py restore <时间戳>")
            print("查看可用时间戳: python3 juguang_token_vault.py list-backups")
            return
        ts = sys.argv[2]
        if restore_backup(ts):
            print("🔄 建议运行 status 验证恢复结果")

    elif cmd == "clean":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_RETENTION_DAYS
        clean_backups(days)

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    cli()
