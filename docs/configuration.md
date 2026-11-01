# 配置

所有可调项都在 `config.yaml`，改完重启服务生效：

```bash
docker compose restart        # Docker
sudo systemctl restart tnews  # systemd
```

> 页面里改过的设置（分类、时长、语速、音色、源占比、自动更新、天气、保留天数…）存在数据库里，**优先级高于 `config.yaml`**。配置只作为默认值。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TNEWS_CONFIG` | `./config.yaml` | 配置文件路径 |
| `TNEWS_DATA` | `./data` | 数据目录（音频等） |
| `TNEWS_DB` | `$TNEWS_DATA/tnews.db` | 数据库路径 |
| `TNEWS_ADMIN_PASSWORD` | 随机生成 | 首次启动时 admin 的初始密码；不填则随机，打印在日志并写入 `data/INITIAL_ADMIN_PASSWORD.txt` |
| `LLM_API_KEY` | 空 | 大模型润色的 key（`digest.llm.api_key_env` 可改名） |
| `TZ` | `Asia/Shanghai` | 只影响容器日志时间；程序内部固定用东八区 |

`NEWSRADIO_CONFIG` / `NEWSRADIO_DATA` / `NEWSRADIO_DB` / `NEWSRADIO_ADMIN_PASSWORD` 是旧版名字，仍然兼容。

## 全文示例

```yaml
app_name: "TNews"

# 项目主页：首页底部入口 + 管理员「检查更新」的仓库来源；留空则两者都不启用
repo_url: ""                 # 例：https://github.com/your-name/tnews

server:
  host: "0.0.0.0"
  port: 18282
  public_base_url: ""          # 播客/音频的绝对地址前缀，留空按请求推断
  auto_generate_on_open: true  # 打开网页时若最新一期太旧就补一期
  auto_generate_min_age_hours: 6

home_assistant:
  webhook_url: ""              # 每期生成完 POST 一条 JSON，留空不推
  base_url: ""                 # 推送内容里音频地址的前缀

tts:
  voice: "zh-CN-YunxiNeural"
  rate: "+10%"
  volume: "+0%"
  pitch: "+0Hz"
  gap_seconds: 0.45

digest:
  max_items: 14
  max_chars_per_item: 120
  min_chars_per_item: 24
  llm:
    enabled: false
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
    api_key_env: "LLM_API_KEY"

categories:
  domestic:
    label: 国内
    desc: 时政、政策、民生要闻
    feeds:
      - { name: 中国新闻网·时政, url: "https://www.chinanews.com.cn/rss/china.xml" }

programs:
  - id: morning
    name: 要闻速听
    crons: ["30 7 * * *", "0 18 * * *"]
    lookback_hours: 12
    categories: [domestic, world, finance, tech, society, hot]

guest:
  enabled: true
  account: ""

auto_update:
  enabled: false
  interval_hours: 6
  program_id: ""

retention_days: 14
```

## 逐段说明

### app_name

网页标题、页面左上角、播客订阅源名称都用它。改成自己喜欢的，例如 `"漫路听新闻"`。

### repo_url

项目主页地址（GitHub / Gitee 等），两个用途：

- **首页底部入口**：填了就在页面底部显示一个「GitHub · 用户名/仓库名」的胶囊链接，点了跳转；留空整块隐藏；
- **检查更新**：管理员「设置 → 全局管理 → 版本更新」用 GitHub Releases API 查最新版本，与当前 `/health` 里的版本号比较，有新版会提示并可跳转 Release 页。成功结果缓存 10 分钟，失败缓存 2 分钟，页面上点「检查更新」可强制重查。

只认 GitHub 地址（`github.com/用户/仓库`），其他地址只显示入口、不检查更新。仓库若是**私有**的，GitHub API 匿名访问会 404，此时给容器加一个只读 token 环境变量 `TNEWS_GITHUB_TOKEN` 即可检查（不设则提示"仓库不可访问"）。

### server

- `public_base_url`：拼「播客订阅里的音频地址」和 webhook 里的 `latest_mp3` 时用。局域网留空即可（按请求推断）；做了反向代理或要从外网听，填外部地址，如 `https://news.example.com`。
- `auto_generate_on_open`：打开网页时检查最新一期，超过 `auto_generate_min_age_hours` 小时就后台补录一期，防止 NAS 关机错过定时任务。关掉则只在 cron 时刻生成。

### home_assistant

生成成功后往 HA Webhook 推一条 JSON（见 [对外接口](api.md#生成完成推送webhook)）。`base_url` 用来拼出 HA 能直接播放的音频地址；留空则退回 `server.public_base_url`，相对路径在很多播放器上放不出来。

### tts

| 字段 | 说明 |
|---|---|
| `voice` | 音色。常用：`zh-CN-YunxiNeural`（男·清晰）、`zh-CN-YunjianNeural`（男·浑厚）、`zh-CN-XiaoxiaoNeural`（女·温和）、`zh-CN-XiaoyiNeural`（女·活泼）、`zh-CN-liaoning-XiaobeiNeural`（东北话）。完整列表 `edge-tts --list-voices` |
| `rate` / `volume` / `pitch` | 语速 / 音量 / 音调，写法如 `+10%`、`-5%`、`+2Hz` |
| `gap_seconds` | 条与条之间的静音间隔，默认 0.45 秒 |

页面上选过音色/语速后按那期为准，配置只作默认值。

### digest

- `max_items`：一期最多多少条（页面上选了「目标时长」时以时长折算为准）
- `max_chars_per_item` / `min_chars_per_item`：单条口播稿的字数上限与有效下限
- `llm`：可选的大模型润色。`base_url` 填任何 OpenAI 兼容接口，key 从环境变量读（`api_key_env`），**不要写进配置文件**。模型不可用时自动退回抽取式摘要，不会让整期失败。

14 条约 5 分钟，10 条约 3 分半。

### categories

分类 → 一组 RSS 源。加源只要往 `feeds` 里加一条：

```yaml
      - { name: 某博客, url: "https://example.com/feed.xml", share: 30 }
```

- 支持标准 RSS / Atom（XML 里有 `<rss>` 或 `<feed>`）
- `share` 是**生成占比**（%）：填 30 表示本期约三成条数来自该源；不填则按各源候选条数比例分配
- 内置特殊源 `weibo://hot-search`：直接抓微博实时热搜（不是 RSS），`hot` 分类默认用它

页面上「抓取源」区域也能添加自定义源（保存到数据库，按账号隔离），会先校验能否访问、是不是 RSS/Atom 再入库。

### programs

一档节目 = 一个抓取窗口 + 一组分类 + 一组定时器。名字只是这一档的叫法，不绑定时段。

```yaml
programs:
  - id: morning                  # 稳定标识，接口里的 ?program_id= 用它
    name: 要闻速听
    crons: ["30 7 * * *", "0 18 * * *"]
    lookback_hours: 12
    categories: [domestic, world, finance, tech, society, hot]
```

- `crons`：标准 5 段 cron（分 时 日 月 周），可写多条；时区固定东八区。也兼容老写法 `cron: "30 7 * * *"`
- `lookback_hours` 与 `window: {start: "20:00", end: "07:30"}` 二选一；`end < start` 表示跨天
- 期数在同一档节目里连续往下排；只留一档最省心

### guest

未登录的人打开首页看谁的节目：

```yaml
guest:
  enabled: true      # false = 访客打开首页没有内容，提示登录
  account: ""        # 登录名 / 邮箱 / 6 位用户 ID；留空 = 主管理员
```

前提是该账号在「我的账号 → 速听分享」里是开启状态，否则访客同样看不到内容。网页「设置 → 全局管理 → 首页访客指向」里的选择优先于这里；整段删掉则保持旧行为（访客始终能听管理员的节目）。

### auto_update

定点 cron 之外的「每隔 N 小时生成一期」：

```yaml
auto_update:
  enabled: true
  interval_hours: 6
  program_id: ""     # 用哪档节目的分类/音色，留空 = 第一档
```

实际运行时**按账号独立**：每个登录用户各存一份自己的自动更新设置，定时生成的节目归各自账号；账号被禁用/删除时任务一起移除。配置里这段只是新库的初始默认值。

### retention_days

音频与记录的保留天数（默认 14）。每天 04:10 清理过期内容；管理员也可以在页面里选 7/14/30/60/90 天或永久，并能一键清空往期。
