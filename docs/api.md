# 对外接口

给 Home Assistant、Node-RED、自建脚本用的只读接口。**默认都是访客视角**（管理员那份节目），要取某个账号的节目就带上鉴权参数。

## 鉴权：认哪个身份

按优先级从高到低，命中即停：

| 方式 | 写法 | 说明 |
|---|---|---|
| 登录令牌 | `Authorization: Bearer <token>` 或 `X-Token: <token>` 或 `?token=` | 正式鉴权。取某账号的令牌后，看到的就是该账号的节目（管理员令牌 = 全部账号里最新的一期） |
| 外部调用令牌 | `?ext=<令牌>` | 每个账号一个的只读令牌（我的账号 → 外部调用）。需管理员开总开关 + 该账号开分开关 |
| 速听分享 | `?sub=<令牌>` | 订阅链接里用的只读身份（`/s/<令牌>` 页面自动带） |
| 账号 + ID | `?user=ha&id=123456` | 匿名按账号取：账号名 + 该账号 6 位用户 ID，两个都要对。`uid=` 也行 |
| 访客 | 不带 | 管理员（或 `guest.account` 指定的账号）的最新一期 |

几个要点：

- `?user=` 是明文账号名，只适合内网。对外开放请改用 `token` 或 `ext`。
- 令牌无效返回 **401**，不会静默降级成访客。
- 只开总开关、账号没开分开关 → **403**「账号 xxx 未开启外部调用」；只开分开关 → 403「总开关未开启」；缺 ID 或 ID 不对 → 403 并提示正确的 ID。
- 账号不存在 → 404「没有这个账号」；账号存在但没生成过 → 404「账号 xxx 还没有生成过任何一期」。

## 接口一览

| 接口 | 返回 | 用途 |
|---|---|---|
| `GET /health` | `{"ok":true,"version":"1.0.1","time":"..."}` | 探活、确认版本 |
| `GET /api/latest` | 最新一期的最小信息 | HA REST sensor、脚本判断有没有新一期 |
| `GET /api/latest/content` | 最新一期的完整内容 | 仪表盘显示全文 / 标题清单 |
| `GET /api/programs` | 节目清单 + 各自最新一期 | 确认 `program_id` 该写什么 |
| `GET /latest.mp3` | 302 跳到最新一期音频 | 丢给播放器直接播 |
| `GET /audio/<文件名>` | 音频文件 | 直链播放（加 `?inline=1` 为内联流式） |
| `GET /feed.xml` | 播客 RSS（全站合集） | 播客 App 订阅 |
| `GET /feed/<令牌>.xml` | 播客 RSS（单账号） | 播客 App 订阅 |

通用参数：

| 参数 | 说明 |
|---|---|
| `program_id` | 节目 id（默认只有 `morning`）。写错会明确报可选项，不会静默返回别的节目；不传 = 所有节目里最新的一期 |
| `token` / `ext` / `sub` / `user` + `id` | 见上面鉴权表 |

## GET /api/latest

```bash
curl "http://NAS:18282/api/latest"
curl "http://NAS:18282/api/latest?program_id=morning"
curl "http://NAS:18282/api/latest?ext=EXTERNAL_TOKEN"       # 某账号的最新一期
```

```json
{
  "id": 56,
  "title": "要闻速听 · 11 月 1 日 · 第 15 期",
  "program_id": "morning",
  "program_name": "要闻速听",
  "seq": 15,
  "user_id": 1,
  "created_at": "2026-11-01T20:21:00",
  "duration": 280.0,
  "item_count": 14,
  "url": "http://NAS:18282/audio/20261101-2021-56.mp3",
  "latest_mp3": "http://NAS:18282/latest.mp3",
  "feed": "http://NAS:18282/feed.xml"
}
```

`url` / `latest_mp3` 里的主机名来自 `server.public_base_url`（没配则按请求推断）。

## GET /api/latest/content

比 `/api/latest` 多了每条新闻的正文、来源、原文链接与播报时间点，天气段也带。

```bash
curl "http://NAS:18282/api/latest/content?program_id=morning"
```

```json
{
  "title": "要闻速听 · 11 月 1 日 · 第 15 期",
  "seq": 15,
  "duration": 280.0,
  "items": [
    {"n": 1, "category": "domestic", "category_label": "国内",
     "title": "……", "text": "……", "source": "中国新闻网·时政",
     "link": "https://…", "start": 12.84}
  ],
  "weather": {"text": "杭州 15 度 有雾", "start": 0.5},
  "url": "http://NAS:18282/audio/20261101-2021-56.mp3"
}
```

`text` 是口播稿（用来显示或 TTS 复用），`start` 是在整期音频里的秒数。天气没开时 `weather` 为 `null`。

## GET /latest.mp3

302 跳到最新一期音频，播放器可以直接用。适合「不关心是哪一期、有就播」的场景。

```bash
curl -I "http://NAS:18282/latest.mp3"
```

返回头带 `Cache-Control: no-store`，避免播放器缓存成旧一期；个别播放器仍会缓存，可以带时间戳：`/latest.mp3?t={{ now().timestamp() }}`。

> 小米音箱这类设备建议用 `/audio/xxx.mp3?inline=1` 直链（`/api/latest` 里的 `url` 就是），`/latest.mp3` 的 302 + `attachment` 头会让它反复拉流。

## GET /api/programs

```bash
curl "http://NAS:18282/api/programs"
```

返回每档节目的 `id` / `name` / 分类 / 抓取窗口 / 最新一期。不确定 `program_id` 写什么时先看这个。

## 生成完成推送（webhook）

`config.yaml`：

```yaml
home_assistant:
  webhook_url: "http://192.168.1.50:8123/api/webhook/tnews_new_episode"
  base_url: "http://192.168.1.100:18282"
```

每期生成成功后 POST：

```json
{
  "event": "episode_ready",
  "id": 56,
  "title": "要闻速听 · 11 月 1 日 · 第 15 期",
  "program_id": "morning",
  "created_at": "2026-11-01T20:21:00",
  "duration": 280.0,
  "item_count": 14,
  "audio_file": "20261101-2021-56.mp3",
  "url": "http://192.168.1.100:18282/audio/20261101-2021-56.mp3",
  "latest_mp3": "http://192.168.1.100:18282/latest.mp3"
}
```

推送失败只写日志，不影响节目生成。接入示例见 [Home Assistant](home-assistant.md#方式三生成完成后自动播报webhook)。

## 错误码

| 状态码 | 含义 |
|---|---|
| 400 | 参数不合法（如账号格式、密码太短） |
| 401 | 令牌无效或未登录 |
| 403 | 总开关/分开关未开、缺用户 ID、无权操作 |
| 404 | 没有这个节目 / 账号 / 节目（还没生成过） |
| 429 | 预留（当前版本未启用限流） |

## 写接口

页面上做的事都有对应写接口（`POST /api/generate`、`/api/auto`、`/api/me/*`、`/api/admin/users/*` 等），需要登录态，**不承诺向后兼容**，不建议外部系统依赖。要做集成请用上面的只读接口。
