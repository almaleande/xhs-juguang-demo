# 服务器部署指南（让评委在线访问）

> 演示版是**纯 Python 标准库**服务（http.server），无需 pip install 任何第三方包。
> 所有 Token 均为 demo 占位，聚光外部 API 请求已禁用。对外上传请使用脱敏交付 ZIP，勿上传原开发目录的 Git 历史；经营指标仍须自行确认公开权限。

## 云服务器跑完整动态版（推荐，含关键词页/诊断页）

### 1. 准备服务器
- 任意 Linux 服务器（阿里云 / 腾讯云轻量应用服务器，最低 2C2G 即可，Ubuntu 22.04 示例）
- 安装 Python3：
  ```bash
  sudo apt update && sudo apt install -y python3
  python3 --version   # 确认 ≥ 3.8
  ```

### 2. 上传项目
在本地电脑执行（Windows 用 WinSCP / macOS 用 scp）：
```bash
# 在脱敏交付包解压后的上级目录执行
scp -r "聚光投放工作台_脱敏版" root@<服务器IP>:/opt/
```

### 3. 启动服务
```bash
ssh root@<服务器IP>
cd /opt/聚光投放工作台_脱敏版
nohup python3 看板服务.py 8868 5 > /tmp/demo.log 2>&1 &
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8868/   # 应返回 200
```

### 4. 开放端口
- 云服务器控制台 → 安全组 / 防火墙 → 放行 **TCP 8868 端口**（源：0.0.0.0/0）
- 然后浏览器访问：`http://<服务器IP>:8868/`

### 5.（推荐）nginx 反代 80 端口 + 域名
```bash
sudo apt install -y nginx
sudo tee /etc/nginx/sites-available/xhs-demo > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;              # 或你的域名 demo.example.com
    location / {
        proxy_pass http://127.0.0.1:8868;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/xhs-demo /etc/nginx/sites-enabled/
sudo nginx -s reload
# 现在访问 http://<服务器IP>/ 即可（无需 8868 端口）
```
HTTPS：用 Certbot 一键申请（`sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx`）。

### 6.（推荐）systemd 开机自启
```bash
sudo tee /etc/systemd/system/xhs-demo.service > /dev/null <<'EOF'
[Unit]
Description=XHS Juguang Demo
After=network.target

[Service]
WorkingDirectory=/opt/聚光投放工作台_脱敏版
ExecStart=/usr/bin/python3 看板服务.py 8868 5
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now xhs-demo
```

## 演示后清理
- 公开展示结束，建议关闭端口或删除服务器上的演示目录，避免长期暴露。
- 原工作台品牌、原封面与文档元数据已处理；当前 190 张封面及匹配标题为用户提供的新摄影配图与抓取原始标题，含真实人像，并非完全匿名。城市地名及指标保留，公开前需确认经营数据和图文使用权限。
