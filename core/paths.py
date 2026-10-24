#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一路径与配置
================
Windows / WSL 双端可用。优先读环境变量 JUGUANG_DIR，否则以本文件位置推导项目根。

原脚本通病是硬编码 "/mnt/d/工作空间/小红书聚光"，在 Windows 下会被解析成
"D:\\mnt\\d\\..."，导致读不到文件、写错位置、备份静默跳过。这里一次性根治。
"""
import os

_ENV_KEY = "JUGUANG_DIR"


def _detect_base():
    env = os.environ.get(_ENV_KEY)
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    # core/paths.py → 上一级即项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _detect_base()

ACCOUNTS_FILE = os.path.join(BASE_DIR, "juguang_accounts.json")
SECRET_FILE = os.path.join(BASE_DIR, "juguang_secret.json")
SNAPSHOT_DIR = os.path.join(BASE_DIR, "快照")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
DATA_DIR = os.path.join(BASE_DIR, "data")
VAULT_DIR = os.path.join(BASE_DIR, "vault")
BACKUP_DIR = os.path.join(VAULT_DIR, "backups")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 授权状态台账：记录每个主体的授权进度与续期历史
AUTH_STATE_FILE = os.path.join(DATA_DIR, "auth_state.json")
# 每轮拉取的覆盖率与失败清单
COLLECT_REPORT_FILE = os.path.join(DATA_DIR, "collect_report.json")


def ensure_dirs():
    for d in (SNAPSHOT_DIR, REPORTS_DIR, DATA_DIR, BACKUP_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


if __name__ == "__main__":
    print(f"BASE_DIR       = {BASE_DIR}")
    print(f"ACCOUNTS_FILE  = {ACCOUNTS_FILE}")
    print(f"SNAPSHOT_DIR   = {SNAPSHOT_DIR}")
    print(f"REPORTS_DIR    = {REPORTS_DIR}")
    print(f"环境变量覆盖   = {_ENV_KEY}={os.environ.get(_ENV_KEY, '(未设置)')}")
