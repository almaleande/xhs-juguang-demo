# 服务器部署指南（让评委在线访问）

> 演示版是**纯 Python 标准库**服务（http.server），无需 pip install 任何第三方包。
> 服务器装好 Python 3.8+ 即可运行；所有 Token 均为 demo 占位，不会发起任何真实外部 API 调用，可放心公网展示。

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
# 把整个演示版目录传到服务器 /opt/ 下
scp -r "/Users/shuli/Documents/工作空间/夏天婚纱_聚光工作台_演示版" root@<服务器IP>:/opt/
```

### 3. 启动服务
```bash
ssh root@<服务器IP>
cd /opt/夏天婚纱_聚光工作台_演示版
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
WorkingDirectory=/opt/夏天婚纱_聚光工作台_演示版
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
- 本演示版不含任何真实密钥/真实品牌/真实城市数据，可安全公开。
