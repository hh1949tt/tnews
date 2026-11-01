# 在 Home Assistant 里播放

三个层次，按需要挑：**直接播最新一期** → **先拿标题再播** → **生成完自动播报**。再往下还能用 Node-RED 联动音箱（自动显示标题、点击播放、到点掐断）。

先说网络：HA 必须能访问到 TNews 的 IP 与端口。HA 或 Node-RED 跑在 Docker 里时**不要用 `localhost` / `127.0.0.1`**，那是容器自己；要用 NAS 的局域网 IP，不同网段还要在路由器上放通 18282。

## 方式一：直接播放

```yaml
script:
  tnews_play:
    alias: 播放最新一期新闻
    sequence:
      - service: media_player.play_media
        target:
          entity_id: media_player.living_room      # 换成你的音箱 / 电视 / 手机
        data:
          media_content_id: "http://192.168.1.100:18282/latest.mp3"
          media_content_type: music
```

只想听某个账号的节目（比如专为 HA 建的 `ha` 账号），用 `ext` 令牌最省事：

```yaml
media_content_id: "http://192.168.1.100:18282/latest.mp3?ext=该账号的外部调用令牌"
```

## 方式二：REST sensor（显示标题）

```yaml
rest:
  - resource: http://192.168.1.100:18282/api/latest
    scan_interval: 300
    sensor:
      - name: "TNews 最新一期"
        value_template: "{{ value_json.title }}"
        json_attributes:
          - url
          - duration
          - item_count
          - created_at
          - latest_mp3
```

播放时用属性里的地址：

```yaml
data:
  media_content_id: "{{ state_attr('sensor.tnews_latest', 'url') }}"
  media_content_type: music
```

完整内容（每条新闻的标题、正文、来源、链接）用 `/api/latest/content`，返回结构见 [对外接口](api.md#get-apilatestcontent)。

## 方式三：生成完成后自动播报（webhook）

1. HA → 设置 → 自动化 → 新建，触发器选 **Webhook**，记下 ID（如 `tnews_new_episode`）；
2. 编辑 `config.yaml`：

```yaml
home_assistant:
  webhook_url: "http://192.168.1.50:8123/api/webhook/tnews_new_episode"
  base_url: "http://192.168.1.100:18282"      # HA 能访问到的 TNews 地址，用来拼可直接播放的音频链接
```

3. 重启 TNews。之后每期生成完会 POST 一条 JSON（字段见 [对外接口](api.md#生成完成推送webhook)），自动化这样接：

```yaml
automation:
  - alias: 新一期新闻自动播报
    triggers:
      - platform: webhook
        webhook_id: tnews_new_episode
        local_only: true
    conditions:
      - condition: state
        entity_id: input_boolean.news_autoplay     # 可选：加个开关，避免半夜播
        state: "on"
    actions:
      - service: media_player.play_media
        target:
          entity_id: media_player.living_room
        data:
          media_content_id: "{{ trigger.json.latest_mp3 }}"
          media_content_type: music
```

`base_url` 没配时会退回相对路径（`/audio/xxx.mp3`），多数播放器放不出来。

## Node-RED 联动小米音箱

小米音箱播网络音频时**默认单曲循环**，「播完自动停」只能由外部按时长掐断。仓库自带一份可直接导入的 flow：`extras/nodered-tnews-flow.json`。

flow 能做五件事：

1. 每 5 分钟拉 `/api/latest`（或 `/api/latest/content`），把标题/直链写进 `input_text`，全文写进 `sensor` 属性；
2. 点 `input_button.tnews_play` 播放最新一期：用 `/audio/xxx.mp3?inline=1` 直链（避开 302 与 `attachment` 头导致的循环拉流）；
3. 点 `input_button.tnews_stop` 立即暂停，并撤掉这次播放排的自动暂停计时；
4. 按本期 `duration` 减 0.8 秒计时，到点 `media_player.media_pause` 掐断循环；重复点击会重置计时；
5. 暂停失败时兜底 `media_play_pause`（小米 `play_control` 没有 `media_stop`），播放失败只记日志、不动音箱。

### 准备：4 个辅助元素

HA → 设置 → 设备与服务 → 辅助元素 → 新建：

| 类型 | 实体 ID | 用途 |
|---|---|---|
| 文本 | `input_text.tnews_title` | 存最新一期标题 |
| 文本 | `input_text.tnews_url` | 存音频直链（排障用） |
| 按钮 | `input_button.tnews_play` | 点一下播放 |
| 按钮 | `input_button.tnews_stop` | 点一下暂停（同时撤掉这次播放排的自动暂停计时） |

（可选）再加一个 `input_text.tnews_content` 存 255 字摘要——不需要就把 flow 里那条支线删掉。

### 导入与配置

Node-RED → 菜单 → 导入 → 选 `extras/nodered-tnews-flow.json`。

**只改一个节点：「⚙️ 统一配置（只改这里：地址/账号/令牌/实体）」**

| 字段 | 说明 |
|---|---|
| `base` | TNews 地址，如 `http://192.168.1.100:18282`（Node-RED 在 Docker 里，别用 localhost） |
| `ext` | 外部调用令牌（推荐）：我的账号 → 外部调用 → 复制地址里 `ext=` 后面那串 |
| `token` | 或填登录令牌 |
| `user` + `pin` | 或填账号名 + 6 位用户 ID（老方式） |
| `program_id` | 只看某一档节目时填 `morning`，留空 = 该账号全部节目里最新一期 |
| `ha` / `ha_token` | HA 地址 + 长期访问令牌（HA → 头像 → 安全 → 长期访问令牌） |
| `player` / `input_*` / `sensor` | 实体 ID，HA 里改过名才需要动 |
| `media_type` | 播放通道，默认 `music`；叮一声没声音就改 `url`（见踩坑 7） |
| `play_prefix` | 播放专用地址前缀，如 `http://192.168.2.6:18282`；留空 = 用 `base` |

令牌**原样粘进引号**，不要加 `Bearer`、不要换行。填的仍是占位符时，节点会提示「ha_token 还没换成真实令牌」并跳过全文写入（摘要照常）。

唯一还要手动选的：各节点右上角 **Server** 下拉（你自己的 HA 实例），这属于 Node-RED 与 HA 的连接配置，写不进 flow。

### 仪表盘

- 标题：Markdown 卡片 `## {{ states('input_text.tnews_title') }}`
- 播放 / 暂停按钮：两个 Button 卡片，`tap_action` → `perform-action` → `input_button.press`，实体分别选 `input_button.tnews_play` 和 `input_button.tnews_stop`

```yaml
type: horizontal-stack
cards:
  - type: button
    name: 播放
    icon: mdi:play
    tap_action:
      action: perform-action
      perform_action: input_button.press
      target: { entity_id: input_button.tnews_play }
  - type: button
    name: 暂停
    icon: mdi:pause
    tap_action:
      action: perform-action
      perform_action: input_button.press
      target: { entity_id: input_button.tnews_stop }
```

暂停是「先撤计时、再 `media_pause`」：不撤计时的话，手动停了以后那个定时器到点还会再调一次 pause，而 pause 对已暂停的设备可能报错，兜底又是切换式的 `media_play_pause`，一翻就把节目又放出来。
- 全文卡片：

```yaml
type: markdown
title: 要闻速听
content: >
  {{ state_attr('sensor.tnews_content', 'content') }}
```

标题栏想显示「更新于 … · 共 N 条」：`更新于 {{ state_attr('sensor.tnews_content', 'updated_at') }} · 共 {{ states('sensor.tnews_content') }} 条`。

想要「热榜卡片」那种极简样式（每条一行 `- 标题`），sensor 还带 `list` 属性：

```yaml
type: markdown
title: '{{ state_attr(''sensor.tnews_content'', ''title'') }}'
content: >
  {{ state_attr('sensor.tnews_content', 'list') }}
```

> 改文案格式就动「拼 Markdown」函数，`md.push(...)` 每行一目了然。HA 重启后 sensor 会消失一次，最多 5 分钟自动写回。

### 踩过的坑

**1. 小米 `play_control` 没有 `media_stop`**

直接调会报 `Entity ... does not support action media_player.media_stop`，而且这个错会中断整条流程，后面根本不播。flow 用的是 `media_pause`，失败再兜底 `media_play_pause`。

注意 `media_play_pause` 是**切换**：正常时序下音箱正在循环播放，切一下就停；但如果某次它已经自己停了，切换会把它重新放出来。真遇到就把兜底动作换成 `media_player.turn_off`。

想看某实体支持哪些动作：开发者工具 → 动作 → 搜 `media_player.pause` → 「选择实体」看能不能选中。

**2. 目标实体必须放在 `msg.payload.target.entity_id`**

`node-red-contrib-home-assistant-websocket` 1.x 起不再认顶层 `entity_id`：

```js
// ❌ 旧写法：HA 报 at least one of ['entity_id','device_id',...] is required
msg.payload = { entity_id: 'media_player.xxx', data: { media_content_id: '...' } };

// ✅ 新写法：target 包一层，entity_id 是数组
msg.payload = {
    target: { entity_id: ['media_player.xxx'] },
    data: { media_content_id: '...', media_content_type: 'music' }
};
```

flow 里的「组装 play_media」已经按新格式输出。对应地，播放/暂停/兜底三个服务节点的 **Block input overrides 必须不勾**——勾上等于丢弃 `msg.payload`，`target` 传不进去。

**3. `catch` 带下来的脏参数**

`catch` 转发的是出错那一步的原始消息，里面还带着 `data.media_content_id`，直接丢给兜底动作会报 `not a valid option at 'media_content_id'`。所以中间夹了「清掉脏参数，只留 entity_id」函数节点，整体重建 `msg.payload`，并把原始错误写进 `msg.errNode` / `msg.errMsg`。

**4. 播放失败和暂停失败要分开处理**

两个 catch 各管一段：暂停失败 → 切一下播放/暂停；播放失败 → 只记日志，不去动音箱（否则可能把正在放的内容打断）。排障先看 `错误详情（先看这个）` 这个 debug 节点。

**5. 为什么提前 0.8 秒停**

最初用 `duration + 5 秒` 缓冲，结果第一遍播完立即单曲循环，那 5 秒就成了「明明结束了又重播一句」。改成结束**前** 0.8 秒停，宁可少 0.8 秒尾巴。起播慢的设备把「组装 play_media」顶部的 `const LEAD_MS = 800;` 调到 1500～2000；起播快的可以调到 300。

**6. `Content-Disposition: attachment` 导致反复拉流**

默认音频地址带这个头，部分小米固件会反复重新拉流（看着也像循环）。播放地址加 `?inline=1` 改成内联流式即可。

**7. 叮一声之后没声音（实体停在 paused）**

这是小爱音箱最常见的一类「假成功」：服务调用没报错，音箱也「叮」了一下，然后什么都没放，`media_player.*_play_control` 停在 `paused`。

原因是 miot auto 的 `play_media` 有**两条完全不同的通道**，走哪条只由 `media_content_type` 决定（见 [media_player.py](https://github.com/al-one/hass-xiaomi-miot/blob/master/custom_components/xiaomi_miot/media_player.py)）：

| `media_content_type` | 实际调用 | 适用 |
|---|---|---|
| `music` / `audio` / `mp3` / `tts` / `audio/*` | 小米云端「音乐」接口 `player_play_music` | 多数型号可用；部分型号会「叮完就停」 |
| 其它任意值（如 `url`） | 「直链播放」接口 `player_play_url` | 小爱音箱 Play、OH2P 等只认直链的型号 |

flow 的「⚙️ 统一配置」里 `media_type` 就是这一项，默认 `music`；**叮完没声音就把它改成 `'url'`**，重新部署再试一次。两种都不行时，才是网络或固件问题（往下看）。

排查顺序（照做，3 分钟能定位）：

1. 开发者工具 → 动作 → `media_player.play_media`，YAML 模式填：

   ```yaml
   action: media_player.play_media
   target:
     entity_id: media_player.xiaomi_oh2p_5b29_play_control
   data:
     media_content_id: http://192.168.2.6:18282/audio/你的文件名.mp3?inline=1
     media_content_type: url      # ← 只改这一行，music / url 各试一次
   ```

2. 两条通道都无声 → 换个公网、443 端口的 mp3（如 `https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3`）。
   - 这个能放 → 是音箱取不到你的地址：音箱和 NAS 不在同一网段 / 路由器开了 AP 隔离 / 域名要走公网回环。把配置里的 `play_prefix` 填成 NAS 的局域网地址（如 `http://192.168.2.6:18282`），只让「播放」走内网，网页和播客不受影响。
   - 这个也不能放 → 是音箱或集成本身：重载 Xiaomi Miot Auto 集成、重新登录小米账号，再确认型号是否支持 URL 播放。

3. 还有一层容易忽略：实体音量。`media_player.volume_set` 之后看 `volume_level` 有没有变，没变说明控制通道也没通。

`?inline=1` 必须留着（见坑 6），`play_prefix` 只会替换 `http://域名:端口` 这一段，路径和参数不动。
