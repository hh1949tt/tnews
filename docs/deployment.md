# 部署

三种方式任选：Docker Compose（推荐）、裸机 Python、群晖/飞牛这类 NAS 的图形界面。数据只有一份：`data/` 目录（SQLite 数据库 + 音频），**备份它就是备份全部**。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | 群晖 / 威联通 / 极空间 / 飞牛 / 树莓派 / 任意 Linux、macOS、Windows |
| 运行时 | Docker 20+ 与 Compose v2，或 Python 3.10+ |
| 资源 | 1 核 512 MB 起步；一期 5 分钟节目约占 5 MB 音频、CPU 十几秒 |
| 网络 | 需要能访问各新闻站 RSS、`speech.platform.bing.com`（语音合成）；不开放任何入站依赖 |

## 方式一：Docker Compose

一台机器上跑起来只需要三样东西：项目文件、`Dockerfile`、`docker-compose.yml`。项目文件有两种拿法，**服务器上没有 git 就用第二种**，效果完全一样。

### 1.0 直接用现成镜像（不用构建，最快）

每个 `v*` 标签推送后，GitHub Actions 会自动构建多架构镜像（amd64 + arm64，树莓派可用）并发布到 GHCR。仓库里的 `docker-compose.yml` 默认就指向这个镜像，所以只要 compose 文件和 `config.yaml` 在位：

```bash
mkdir -p /opt/tnews && cd /opt/tnews
# 只需要 docker-compose.yml 和 config.yaml 两个文件（可从 Release 包里取）
cp .env.example .env        # 可选
docker compose pull && docker compose up -d
```

或者干脆不写 compose：

```bash
docker run -d --name tnews --restart always -p 18282:18282 \
  -v /opt/tnews/data:/app/data \
  -v /opt/tnews/config.yaml:/app/config.yaml:ro \
  ghcr.io/hh1949tt/tnews:latest
```

镜像标签规则：`v1.0.2` → `1.0.2`、`1.0`、`latest` 三个标签同时更新。首次从 GHCR 拉取需要在 https://github.com/hh1949tt/tnews/pkgs/container/tnews 把 Package 可见性设为 Public（跟随仓库，一般首次发布时自动继承），私有仓库则先 `docker login ghcr.io`。

### 1.1 用 git 拉取

```bash
git clone https://github.com/hh1949tt/tnews.git
cd tnews
cp .env.example .env        # 可选：固定管理员密码 / 填大模型 key
docker compose up -d --build
```

### 1.2 手动下载压缩包（没有 git、或不想装 git）

在电脑上下载 Release 里的 `tnews-<版本>.zip` / `.tar.gz`，上传到服务器解压即可。也可以在服务器上直接下（把 `v1.0.2` 和账号换成实际值）：

> 仓库还没打 tag 时，把 `refs/tags/v1.0.2` 换成 `refs/heads/main`，或者在网页上 **Code → Download ZIP**。归档包里不含 `data/`、`dist/`、`.env`，正好就是部署需要的那些文件。

> **仓库是私有的？** 上面这些地址都要登录，NAS 上 `wget` 会返回 404。改成「本机打包再传」：
>
> ```bash
> git clone git@github.com:hh1949tt/tnews.git /tmp/tnews && cd /tmp/tnews
> tar -czf /tmp/tnews.tar.gz --exclude=.git .
> scp /tmp/tnews.tar.gz user@服务器:/opt/tnews/
> ssh user@服务器 'cd /opt/tnews && tar -xzf tnews.tar.gz && rm tnews.tar.gz'
> ```
>
> 装了 `gh` 并登录过的机器也可以 `gh repo clone hh1949tt/tnews /tmp/tnews`、`gh release download`（会带凭证）。私有仓库的取舍见[开发 · 仓库可见性](development.md#仓库可见性私有还是公开)。

```bash
# 建目录
mkdir -p /opt/tnews && cd /opt/tnews

# 下载并解压（二选一）
wget -O tnews.tar.gz https://github.com/hh1949tt/tnews/archive/refs/tags/v1.0.2.tar.gz
tar -xzf tnews.tar.gz --strip-components=1

# 或者从本地上传后解压
# scp tnews-1.0.2.tar.gz user@服务器:/opt/tnews/
# tar -xzf tnews-1.0.2.tar.gz

cp .env.example .env        # 可选
docker compose up -d --build
```

解压后目录里应当有这些，缺一个都会构建失败：

```
/opt/tnews
├── Dockerfile            ← 必须有
├── docker-compose.yml    ← 必须有
├── requirements.txt      ← 必须有
├── config.yaml
├── app/                  ← 后端代码
├── web/                  ← 前端页面与背景音乐
└── data/                 ← 不用手动建，首次启动自动生成
```

几个容易踩的点：

- **别把 `data/` 一起打包上传**。它是运行数据（数据库 + 音频），覆盖会丢节目；新机器上让它自己生成。
- 压缩包若带一层目录（如 `tnews-1.0.2/`），要么用 `--strip-components=1` 拆掉，要么 `cd` 进去再 `docker compose up`；`docker-compose.yml` 里的 `build: .` 指的是**当前目录**，目录不对会报 `failed to read Dockerfile`。
- Windows 上解压后拷到 Linux 会带上 CRLF，`docker compose` 会报 `env file ... contains invalid characters`——用 `dos2unix .env` 或编辑器切回 LF。
- Windows 本地解压：`Expand-Archive tnews-1.0.2.zip -DestinationPath C:\tnews`，再整体拷到服务器；`config.yaml` / `.env` 别用记事本改（会存成带 BOM 的 UTF-8），用 VS Code 存「UTF-8 无 BOM」。
- 手动部署的升级就是「下载新包 → 覆盖 `app/ web/ config.yaml Dockerfile requirements.txt`（保留 `data/`）→ `docker compose up -d --build`」，见[升级](#升级)。

### 1.3 在别的机器上构建，把镜像拷过去

NAS 性能弱、或者服务器连不上 GitHub / PyPI 时，可以在有网的电脑上构建好镜像再搬。镜像里已经包含了代码和全部依赖，目标机器不用再 `--build`；但 compose 要挂载的文件得在位，所以仍然需要 `docker-compose.yml` + `config.yaml`（`data/` 可让容器自建）。

```bash
# ① 在电脑上构建并导出（amd64 NAS / 普通 x86 服务器）
docker build -t tnews:latest .
docker save tnews:latest | gzip > tnews-image.tar.gz

# ② 传到服务器
scp tnews-image.tar.gz user@服务器:/opt/tnews/

# ③ 服务器上导入
cd /opt/tnews
gunzip -c tnews-image.tar.gz | docker load
```

`docker-compose.yml` 里已经有 `image: tnews:latest`，把 `build: .` 那一行注释掉，然后：

```bash
cd /opt/tnews
ls config.yaml docker-compose.yml   # 这两个必须在，否则挂载会变成空目录
mkdir -p data
docker compose up -d
```

> ARM 设备（树莓派、部分 ARM NAS）不用自己交叉构建：GHCR 上的镜像自带 arm64 版本（见 1.0），`docker compose pull` 即可。只有离线搬运时才需要在同架构机器上构建，或手动交叉构建：
> `docker buildx build --platform linux/arm64 -t tnews:latest --load .`
> 架构不匹配时容器会起不来，日志报 `exec format error`。

### 部署完成后的公共说明

- 访问：`http://服务器IP:18282`
- 数据：宿主机 `./data` 挂到容器 `/app/data`
- 配置：宿主机 `./config.yaml` 只读挂到容器，改完 `docker compose restart` 生效
- 健康检查：容器每 60 秒请求一次 `/health`
- 首次启动在日志里打印随机管理员密码：`docker compose logs tnews | grep 管理员`

`docker-compose.yml` 里几个常改的地方：

```yaml
ports:
  - "8090:18282"        # 左边换成宿主机空闲端口，访问地址随之变化
environment:
  - TZ=Asia/Shanghai
  - TNEWS_ADMIN_PASSWORD=你的密码    # 仅首次启动建库时生效
volumes:
  - ./data:/app/data
  - ./config.yaml:/app/config.yaml:ro
```

### 群晖（Container Manager）

1. 把项目放到 `/volume1/docker/tnews`；
2. Container Manager → **项目 → 新增**，路径选该目录，它会自动读 `docker-compose.yml`；
3. 构建完成后访问 `http://群晖IP:18282`。

### 飞牛 fnOS

飞牛自带 Docker 与图形化 Compose，**不用 git 也能装**：建目录 → 上传压缩包解压 → Docker 应用里新建 Compose 项目（来源选「路径」，否则读不到 `Dockerfile`）→ 首次构建 2～5 分钟。完整带截图步骤、端口与卷路径写法、升级方式、常见报错处理见 **[飞牛 fnOS 部署](fnos.md)**。

### 不用 Compose：直接 docker run

不想写 `docker-compose.yml` 时（已在项目目录里，`config.yaml` 就位）：

```bash
docker build -t tnews:latest .
docker run -d --name tnews -p 18282:18282 \
  -v "$PWD/data:/app/data" -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -e TZ=Asia/Shanghai --restart always tnews:latest
```

缺点是没有 `docker compose logs / restart` 这类便利命令，升级也要重跑 `build` + `rm` + `run`，日常还是建议用 Compose。

## 方式二：裸机 Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 18282
```

用 systemd 常驻：

```ini
# /etc/systemd/system/tnews.service
[Unit]
Description=TNews
After=network-online.target

[Service]
WorkingDirectory=/opt/tnews
Environment=TNEWS_CONFIG=/opt/tnews/config.yaml
Environment=TNEWS_DATA=/opt/tnews/data
Environment=TNEWS_ADMIN_PASSWORD=换成你自己的密码
ExecStart=/opt/tnews/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 18282
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tnews
```

## 反向代理与 HTTPS

局域网可以直接用 `http://IP:18282`。以下情况需要反向代理：

- 想在公网听、或加 HTTPS（部分播放器只吃 `https://` 音频）；
- 播客 App 要求 https 订阅源；
- 想在外面加一层访问控制。

Caddy（自动证书）最省事：

```caddy
news.example.com {
    basic_auth { you $2a$14$... }        # 可选：再加一层口令
    reverse_proxy 127.0.0.1:18282
}
```

Nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name news.example.com;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:18282;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;              # 音频流式播放更顺
    }
}
```

代理后记得把 `config.yaml` 的 `server.public_base_url` 改成外部地址（如 `https://news.example.com`），否则播客订阅里的音频链接会指向内网 IP：

```yaml
server:
  public_base_url: "https://news.example.com"
```

> 安全：服务本身只有账号密码，没有 HTTPS、没有失败次数限制。**不要直接映射 18282 到公网**。必须外网访问时，务必走反代 + HTTPS，并考虑再加一层 Basic Auth 或只允许固定 IP。

## 升级

代码在镜像里，**只覆盖文件不动镜像是不会生效的**。升级完都要 `--build` 重建，最后看 `/health` 里的版本号确认。配置了 `repo_url` 且仓库公开时，管理员在「设置 → 全局管理 → 版本更新」里能直接看到有没有新版。

```bash
cd /path/to/tnews
docker compose down                       # 或不停机直接下一步
docker tag tnews:latest tnews:old         # 留个回滚点（可选）
curl http://127.0.0.1:18282/health        # {"ok":true,"version":"x.y.z",...}
```

**git 部署**的，更新源码再重建：

```bash
git pull
docker compose up -d --build
```

**手动压缩包部署**的，下载新版本覆盖（`data/` 千万别动）：

```bash
cd /opt/tnews
wget -O tnews-new.tar.gz https://github.com/hh1949tt/tnews/archive/refs/tags/v新版本.tar.gz
mkdir -p /tmp/tnews-new && tar -xzf tnews-new.tar.gz -C /tmp/tnews-new --strip-components=1
cp -r /tmp/tnews-new/app /tmp/tnews-new/web /tmp/tnews-new/Dockerfile \
      /tmp/tnews-new/requirements.txt /tmp/tnews-new/docker-compose.yml ./
# config.yaml 建议先对比，别直接盖
diff -u config.yaml /tmp/tnews-new/config.yaml | head -40
docker compose up -d --build
```

**只要镜像、不要源码**的（1.3 那种），把新镜像 `docker load` 进来后 `docker compose up -d` 即可，不需要 `--build`。

只更新部分文件时，安全的做法是打包时排除 `data/`：

```bash
tar --exclude=data --exclude=__pycache__ --exclude='*.pyc' \
    -czf tnews-update.tar.gz Dockerfile docker-compose.yml requirements.txt config.yaml app web
```

### 回滚

```bash
docker compose down
docker tag tnews:old tnews:latest
docker compose up -d
```

配置改坏了就用备份顶回去（见下）。

### 升级会动到什么

| 内容 | 影响 |
|---|---|
| `data/tnews.db` 里的节目 | 不动；启动时自动补新字段（`ALTER TABLE`，幂等） |
| `data/audio/*.mp3` | 不动 |
| 页面上调过的设置 | 不动（存在 `settings` 表，优先级高于 `config.yaml`） |
| `config.yaml` | 需要你手动对比新版本模板，挑想用的新配置项 |

## 备份与迁移

```bash
# 备份
tar -czf tnews-backup-$(date +%Y%m%d).tar.gz data/ config.yaml

# 迁移到新机器：拷过去解压，启动即可（数据库里带着账号、设置、往期节目）
```

`data/` 里只有：

```
data/
├── tnews.db              数据库（节目、用户、令牌、设置）
├── audio/*.mp3           音频
└── INITIAL_ADMIN_PASSWORD.txt   首次启动生成，改完密码可删
```

## 端口与网络排查

| 现象 | 检查 |
|---|---|
| 页面打不开 | `docker compose ps` 是否 Up；`curl http://127.0.0.1:18282/health` 本机通不通 |
| 局域网其他设备打不开 | 端口映射是否写成 `"18282:18282"`；宿主防火墙放通端口 |
| 播客里的音频放不了 | `public_base_url` 是否填了对方能访问到的地址 |
| Home Assistant 播不了 | HA 容器里别用 `localhost`，要用 NAS 的局域网 IP |
