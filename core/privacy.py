"""演示版安全边界：不接受真实授权，不向聚光平台发送请求。

固定在代码中，不能通过网页参数或环境变量切换为生产模式。
"""

DEMO_MODE = True
DEMO_REFERENCE_DATE = "2026-10-24"  # 演示“今日”；三页的“昨日”统一为已有数据的 10 月 23 日。
DEMO_MESSAGE = "演示模式：真实授权和外部采集已禁用，仅展示本地演示数据"


def blocked():
    return {"ok": False, "success": False, "code": "DEMO_DISABLED",
            "demo": True, "msg": DEMO_MESSAGE}
