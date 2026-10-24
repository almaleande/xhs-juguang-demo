# 演示版 · Render 部署镜像
# 服务为纯 Python 标准库实现，无需任何第三方依赖
FROM python:3.11-slim

WORKDIR /app

# 复制整个演示版目录（含 data / 快照 / assets 封面）
COPY . /app

EXPOSE 8867

# Render 会注入 PORT 环境变量，由 shell 展开直接传给服务（argv 优先）
CMD ["sh", "-c", "python 看板服务.py ${PORT:-8867} 5"]
