# 开发

## 本地跑起来

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 用一个独立的配置/数据目录，避免弄脏真实数据
export TNEWS_CONFIG=$PWD/config.yaml
export TNEWS_DATA=$PWD/data-dev
export TNEWS_DB=$PWD/data-dev/tnews.db

uvicorn app.main:app --host 127.0.0.1 --port 18282 --reload
```

- 接口文档：<http://127.0.0.1:18282/docs>（FastAPI 自带）
- 页面：`web/index.html` 是单文件，改完刷新即可，没有构建步骤
- 数据库：`sqlite3 data-dev/tnews.db`，表 `episodes` / `users` / `tokens` / `settings`

> 生成一期会真的去抓新闻和调语音合成，开发时更常用的是 `/api/episodes`、`/api/meta` 这类读接口。

## 代码结构

```
app/
├── config.py      配置加载（dataclass：Feed / Category / Program）、时间窗口
├── fetcher.py     RSS 抓取、正文清洗、跨源去重、热度排序（含微博热搜特殊源）
├── summarizer.py  口播稿编排：分类分组、开场白、每条文案、可选大模型润色
├── tts.py         edge-tts 分段合成 → MP3 帧级拼接 → 章节时间轴（不依赖 ffmpeg）
├── pipeline.py    串起来：抓取 → 摘要 → 合成 → 落库 → 通知
├── store.py       SQLite 访问层（含老库补列迁移）
├── auth.py        PBKDF2 密码哈希、随机令牌
├── weather.py     Open-Meteo 当前天气 + 城市反解
├── main.py        FastAPI 路由、鉴权、定时任务、播客 RSS
└── cli.py         命令行（list / gen）
web/index.html     前端（原生 JS + CSS 变量主题，无框架无打包）
extras/            Node-RED flow
tools/             开发工具
```

## 前端约定

- **单文件，无构建**：HTML + `<style>` + `<script>` 全在 `web/index.html`，直接由后端读取返回；改了立刻生效，记得部署时别只覆盖一半。
- **状态在 `state` 对象里**：`state.user` / `state.episodes` / `state.guest` 等，渲染函数都从它读。
- **请求统一走 `api(path, opts)`**：自动带 `Authorization`、自动拼 `?sub=`、401 时清登录态并弹登录框。新接口一律用它，不要裸 `fetch`。
- **主题**：CSS 变量 + `html[data-theme]`，配色只在 `:root` 里定义，别在组件里写死颜色。
- **设置项**：`store.get_user_setting` / `set_user_setting` 按账号存；前端偏好另有 `localStorage`（按 `u<uid>` 分作用域）。

## 工具脚本

| 脚本 | 作用 |
|---|---|
| `tools/build_flow_config.py` | 把 Node-RED flow 的地址/令牌/实体收敛到「⚙️ 统一配置」节点，并保证可重复执行（幂等） |
| `tools/check_flow.cjs` | 校验 flow：必填节点在不在、JS 函数语法对不对、各种鉴权组合拼出的 URL 是否正确（需要 Node.js） |
| `tools/make_bgm.py` | 重新生成 `web/bgm/` 里的 6 首背景音乐 |

改 flow 的正确姿势：**只改 `tools/build_flow_config.py` 里的 `CFG_FN`/各函数模板，再跑一遍脚本生成 JSON**，不要手改 JSON：

```bash
python tools/build_flow_config.py
node tools/check_flow.cjs
```

## 数据库

单文件 SQLite，路径由 `TNEWS_DB` 决定（默认 `data/tnews.db`）。启动时按需 `ALTER TABLE` 补列，所以直接拿旧库跑新版本是安全的。

常用查询：

```bash
sqlite3 data/tnews.db "select id,title,item_count,created_at from episodes order by id desc limit 5"
sqlite3 data/tnews.db "select id,email,role,disabled,uid from users"
sqlite3 data/tnews.db "select key,value from settings where key like 'u1:%' limit 20"
```

`settings.value` 存的是 JSON（`"true"`、`"\"\""`、`{"...":1}`），手改要注意引号，改完重启服务。

## 版本与发布

1. 改版本号：`app/__init__.py` 里的 `__version__`（唯一来源，`/health` 和 FastAPI 元数据都读它）；
2. 更新 `CHANGELOG.md`；
3. 自测：`python -m compileall -q app`、页面能开、关键接口能通；
4. 打 tag 并推送：

```bash
git add -A && git commit -m "release: v1.0.5"
git tag v1.0.5 && git push origin main --tags
```

5. 需要「覆盖式更新包」给不方便 git 的用户（排除 `data/`）：

```bash
tar --exclude=data --exclude=__pycache__ --exclude='*.pyc' \
    -czf tnews-update-$(python -c "import app;print(app.__version__)").tar.gz \
    Dockerfile docker-compose.yml requirements.txt config.yaml README.md app web extras docs tools
```

6. 在 GitHub 的 Release 里把上面的包挂成附件（`dist/` 已被 gitignore，上传靠网页或 `gh release upload`）。这样没有 git 的机器可以直接下载解压部署，见[部署 · 手动下载压缩包](deployment.md#12-手动下载压缩包没有-git或不想装-git)。

7. **Docker 镜像自动发布**：推 `v*` 标签会触发 `.github/workflows/docker.yml`，自动构建 amd64 + arm64 镜像并推到 GHCR（`ghcr.io/<owner>/<repo>`，标签 `1.0.5` / `1.0` / `latest`）。不用配置任何 Secret（用的是仓库自带的 `GITHUB_TOKEN`）。首次发布后到仓库 Packages 页把镜像可见性设为 Public。想给某个 commit 出测试镜像，在 Actions 页手动 Run workflow 即可。

## 仓库可见性：私有还是公开

GitHub 没有「公开但只我看得见」这种档位，只有 **public** 和 **private**（private 里只有你和你邀请的协作者能看）。所以「上架后仅自己可见」就是建一个**私有仓库**。

```bash
# 用 gh 建私有仓库并推送
gh repo create tnews --private --source=. --remote=origin --push

# 或者网页上 New repository 选 Private，然后
git remote add origin git@github.com:hh1949tt/tnews.git
git push -u origin main --tags
```

免费账号下私有仓库的限制（个人账号 2026 年现状）：

| 项 | 私有仓库 | 公开仓库 |
|---|---|---|
| 数量 / 协作者 | 不限 / 不限 | 不限 / 不限 |
| GitHub Actions | 每月 2000 分钟 | **不限**（标准 runner） |
| GitHub Pages | 需 Pro | 免费 |
| 受保护分支、Wiki、insights 图 | 需 Pro | 有 |
| 别人能否看到 / fork | 不能 | 能 |

对本项目的影响：

- CI 一次约 1～2 分钟，2000 分钟/月够跑上百次，不用管；
- **部署文档里的下载地址会失效**——私有仓库的网页、归档包、Release 附件都要登录，NAS 上 `wget` 会返回 404。手动部署请用「本机打包 + `scp`」，或用已登录的 `gh repo clone`，见[部署 · 私有仓库怎么取包](deployment.md#12-手动下载压缩包没有-git或不想装-git)；
- 法律层面：私有 = 没有对外分发，CC BY 的署名义务、LGPL 的替换条款都不触发。但 `LICENSE`、`CREDITS.md` 建议照留，将来转公开不用补。

**先私有、再公开**是稳妥顺序：私有状态下跑一段时间，确认没有密钥、个人信息、内网地址，再切公开。反过来则收不回来——**仓库一旦公开，别人 clone/fork/镜像出去的副本你删不掉**。

切换位置：`Settings → General → 最下面 Danger Zone → Change visibility`（改成 Public 需要手输仓库名确认）。

## 测试

项目目前**没有单元测试框架**，靠三条线保证质量：

- CI（`.github/workflows/ci.yml`）：依赖安装、`compileall`、导入 `app.main`、flow 校验；
- 手工冒烟：起服务 → 生成一期 → 页面播放 → 切换账号 / 主题 / 分享地址；
- 临时脚本放在 `data/`（已 gitignore），不要把会改真实库的脚本提交上来。

欢迎补 pytest：`tests/` 目录放纯函数测试（`summarizer` 的口播稿拼装、`fetcher` 的去重排序、`config` 的时间窗口）最容易落地，也不需要网络。

## 风格约定

- Python：标准库优先，3.10+ 语法（`X | None`、内建泛型）；函数短、注释写「为什么」而不是「做了什么」；对外行为改动同步更新 `docs/`。
- 提交信息：`feat: …` / `fix: …` / `docs: …` / `refactor: …`，一个提交做一件事。
- 新增配置项：`config.yaml` 里写清默认值与注释 → `app/config.py` 解析 → `docs/configuration.md` 补一行。
