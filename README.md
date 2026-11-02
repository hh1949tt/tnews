# TNews

根据新闻RSS，标题与简要自动压成 5 分钟左右的语音简报——每天早上和傍晚各一期，手机上打开网页就能听，也能加进播客 App。

> 自托管、单进程、无 ffmpeg、无外部数据库。一台 NAS / 树莓派 / 小主机就能跑。

<p align="center">
  <img src="docs/images/ui-light.png" width="330" alt="白天模式">
  <img src="docs/images/ui-dark.png" width="330" alt="黑夜模式">
</p>

## 它能做什么

| | |
|---|---|
| **定时出稿** | 按 cron 定点生成（如 7:30 / 18:00），也可以每隔 N 小时生成一期；抓取窗口支持「最近 12 小时」或「昨晚 20:00 → 今早 07:30」 |
| **多源聚合** | 内置 10 个分类、14 个国内可直连的 RSS 源；跨源去重、按热度排序，也支持自建任意 RSS / Atom 源并设「生成占比」 |
| **语音合成** | 微软 Edge 神经语音（云希 / 云健 / 晓晓 / 晓伊 / 东北话），可调语速、音调；MP3 帧级拼接，不需要 ffmpeg |
| **手机端播放器** | 章节跳转（点文稿任意一条直接跳到那句）、锁屏控制、倍速、后退 15 秒、断点续听、连播、跨日连播、睡眠暂停 |
| **播客订阅** | 站点合集 `/feed.xml`，每个账号另有专属地址 `/feed/<令牌>.xml`，可加进小宇宙 / Apple Podcasts 自动下载 |
| **多用户** | 访客免登录收听；注册账号后可生成自己的节目、用自己的源，数据彼此隔离；管理员可改账号 / 改密码 / 禁用 / 删除 |
| **分享与外部调用** | 速听分享、播客订阅、外部接口调用，各自一个可随时吊销的令牌 |
| **可选增强** | 今日天气播报（独立音频，当天首次播放前置）、跑步模式（按时长/配速出稿）、背景轻音乐、大模型润色口播稿 |
| **对外联动** | `/api/latest`、`/api/latest/content`、`/latest.mp3` 给 Home Assistant / Node-RED 用，附可直接导入的 flow |

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/hh1949tt/tnews.git
cd tnews
docker compose up -d --build
```

服务器上没有 git？下载压缩包解压后一样跑（`data/` 会自建）：

```bash
mkdir -p /opt/tnews && cd /opt/tnews
wget -O tnews.tar.gz https://github.com/hh1949tt/tnews/archive/refs/tags/v1.0.2.tar.gz
tar -xzf tnews.tar.gz --strip-components=1
docker compose up -d --build
```

打开 `http://你的IP:18282`，用首次启动时生成的管理员账号登录，点右上角 ⚙️ 生成第一期（约 1～2 分钟）。

> 更多部署细节（手动包的目录结构、镜像离线搬运、ARM 架构、端口与反向代理）见 [部署文档](docs/deployment.md#方式一docker-compose)。仓库设成私有时，上面的归档地址需要登录，改为本机 `git clone` 后打包 `scp` 上传，见[私有仓库怎么取包](docs/deployment.md#12-手动下载压缩包没有-git或不想装-git)。

> 首次启动会在日志里打印随机管理员密码，同时写入 `data/INITIAL_ADMIN_PASSWORD.txt`：
>
> ```bash
> docker compose logs tnews | grep 管理员
> ```
>
> 也可以用环境变量固定：`TNEWS_ADMIN_PASSWORD=你的密码 docker compose up -d`（写进 `.env` 更省事，见 `.env.example`）。

### 直接跑（需要 Python 3.10+）

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 18282
```

命令行补录一期：

```bash
python -m app.cli list          # 看节目配置
python -m app.cli gen morning   # 立即生成一期
```

## 文档

| 文档 | 内容 |
|---|---|
| [部署](docs/deployment.md) | Compose（git / 手动压缩包 / 离线镜像）、裸机、群晖、反向代理与 HTTPS、升级与回滚、备份 |
| [飞牛 fnOS](docs/fnos.md) | 图形界面装 Docker Compose 的完整步骤：目录、上传、端口、升级、常见报错 |
| [版权与合规](docs/legal.md) | 新闻内容、TTS 接口、依赖许可证、素材署名——自用与公开分享的边界 |
| [配置](docs/configuration.md) | `config.yaml` 每个字段、环境变量、节目与定时、新闻源与生成占比、大模型润色 |
| [使用](docs/usage.md) | 播放器、生成设置、账号与权限、分享/播客/外部调用、天气、跑步模式、外观 |
| [对外接口](docs/api.md) | `/api/latest` 系列、鉴权方式与优先级、webhook 推送格式、错误码 |
| [Home Assistant](docs/home-assistant.md) | 直接播放、REST sensor、生成后自动播报、Node-RED + 小米音箱 flow |
| [常见问题](docs/faq.md) | 合成没声音、源失效、时间不对、外网访问、忘记密码、数据迁移 |
| [开发](docs/development.md) | 目录结构、本地调试、工具脚本、发布流程 |
| [CHANGELOG](CHANGELOG.md) | 各版本改动 |

## 目录结构

```
tnews/
├── app/                 后端（FastAPI）
│   ├── config.py        配置加载、时间窗口
│   ├── fetcher.py       RSS 抓取、清洗、去重、排序
│   ├── summarizer.py    口播稿编排（可选大模型润色）
│   ├── tts.py           语音合成 + MP3 拼接 + 章节时间轴
│   ├── pipeline.py      抓取 → 摘要 → 合成 → 入库
│   ├── store.py         SQLite（节目 / 用户 / 令牌 / 设置）
│   ├── auth.py          密码哈希与令牌
│   ├── weather.py       今日天气（Open-Meteo，无需 Key）
│   ├── main.py          API、定时任务、播客 RSS
│   └── cli.py           命令行工具
├── web/index.html       单文件前端（无构建步骤）
├── extras/              Node-RED flow 等外部集成
├── tools/               开发工具（重建 flow、校验、生成背景音乐）
├── docs/                文档
├── config.yaml          全部可调项
└── data/                运行数据（数据库、音频；已 gitignore）
```

## 安全提示

- 这套服务为**局域网自用**设计，自带账号体系但没有 HTTPS、没有限流。**不要直接把 18282 端口暴露到公网。**
- 要在外面听，用 Tailscale / WireGuard 之类的组网，或在前面挂 Nginx / Caddy 做 HTTPS + 访问控制。
- 首次登录后请立刻改掉管理员密码；分享/订阅/外部调用令牌可以随时关闭或重开。
- 发现安全问题请看 [SECURITY.md](SECURITY.md)。

## 免责声明

本项目是一个自托管的新闻聚合 + 语音合成**工具**：不提供、不托管任何新闻内容，也不提供任何在线服务；所有文字与音频都来自使用者自己配置的公开 RSS 源，版权归原媒体与作者所有，程序只做摘要转述。

- 请仅用于**个人与家庭**范围内的收听，不要用于商业用途，不要部署成面向公众的新闻服务——在中国境内向公众提供互联网新闻信息服务需要取得相应许可。
- 合成语音由第三方在线接口生成，可能随时失效；重要信息请以原媒体报道为准。
- 使用本项目产生的任何责任由使用者自行承担。详细分析见 [版权与合规](docs/legal.md)。

## 许可与致谢

- 代码：[MIT](LICENSE)。第三方依赖与素材的许可、署名要求见 [CREDITS.md](CREDITS.md)。
- 新闻内容来自各媒体的公开 RSS，本项目只做摘要转述，**音频仅供个人收听，请勿用于商业转载**。
- 背景音乐：Kevin MacLeod（[incompetech.com](https://incompetech.com)），CC BY 4.0；也可用 `python tools/make_bgm.py` 生成自制音乐。
- 语音合成：[edge-tts](https://github.com/rany2/edge-tts)（微软 Edge 在线语音接口的第三方封装，LGPL-3.0）。
- 天气数据：[Open-Meteo](https://open-meteo.com)（CC BY 4.0）。
