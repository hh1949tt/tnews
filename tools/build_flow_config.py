# 把 Node-RED flow 的服务地址 / 账号 / HA token / 实体 ID 统一收敛到一个「⚙️ 统一配置」节点
import json, pathlib

P = pathlib.Path("extras/nodered-tnews-flow.json")
nodes = json.loads(P.read_text(encoding="utf-8"))
by_id = {n.get("id"): n for n in nodes}

CFG_FN = r"""// ════════════════════════════════════════════════════════════════
//  ⚙️ 统一配置：整条 flow 只改这一个节点，别的地方都不用动
//  改完点右上角「部署」，下一轮抓取（≤5 分钟）自动生效
// ════════════════════════════════════════════════════════════════
const CONFIG = {
    // ① TNews 服务地址：NAS 的局域网 IP + 端口
    //    不要用 localhost / 127.0.0.1——Node-RED 在 Docker 里，那样访问的是容器自己
    base: 'http://192.168.1.100:18282',

    // ② 账号鉴权（按顺序生效，推荐第 1 种）：
    //    ext   = 外部调用令牌：网页「我的账号 → 外部调用」里点「复制地址」，
    //            把地址里 ext= 后面那一串填到这里（管理员需先开「允许外部按账号调用」总开关）
    //    token = 登录口令（网页登录后下发的那种）
    //    user + pin = 账号名 + 6 位用户 ID（老方式，也能用）
    //    都留空 = 访客视角（只看公共节目）
    ext: '',
    token: '',
    user: '',
    pin: '',
    // ③ 只看某一档节目时填 program_id（如 'morning'），留空 = 该账号全部节目里最新的一期
    program_id: '',

    // ④ Home Assistant 地址 + 长期访问令牌
    //    令牌：HA → 左下角头像 → 安全 → 页面最底部「长期访问令牌」→ 创建令牌
    ha: 'http://homeassistant.local:8123',
    ha_token: '在此粘贴长期访问令牌',

    // ⑤ 实体 ID（在 HA 里改过名就改这里）
    player: 'media_player.xiaomi_speaker',
    input_title: 'input_text.tnews_title',
    input_url: 'input_text.tnews_url',
    input_brief: 'input_text.tnews_content',
    sensor: 'sensor.tnews_content'
};

CONFIG.base = String(CONFIG.base || '').trim().replace(/\/+$/, '');
CONFIG.ha = String(CONFIG.ha || '').trim().replace(/\/+$/, '');

// 拼鉴权参数：ext（外部调用令牌）优先，其次 token，再次 user + id，最后 program_id
const q = [];
const add = (k, v) => {
    const s = (v === undefined || v === null) ? '' : String(v).trim();
    if (s) q.push(k + '=' + encodeURIComponent(s));
};
if (CONFIG.ext) {
    add('ext', CONFIG.ext);
} else if (CONFIG.token) {
    add('token', CONFIG.token);
} else {
    add('user', CONFIG.user);
    add('id', CONFIG.pin);
}
add('program_id', CONFIG.program_id);
const query = q.length ? ('?' + q.join('&')) : '';

// 两个 inject 用 topic 区分：latest = 标题/地址；content = 全文
const isContent = String(msg.topic || '').toLowerCase() === 'content';
msg.url = CONFIG.base + (isContent ? '/api/latest/content' : '/api/latest') + query;

// 存起来给下游各节点用（播放器实体、HA 地址、令牌、sensor 名等）
flow.set('tncfg', CONFIG);
global.set('tncfg', CONFIG);
msg.tncfg = CONFIG;

const who = CONFIG.ext ? '外部令牌' : (CONFIG.token ? '口令' : (CONFIG.user ? CONFIG.user : '访客'));
node.status({ fill: 'green', shape: 'dot', text: who + ' · ' + (isContent ? 'content' : 'latest') });

// 两路分开走：第 1 口 → /api/latest，第 2 口 → /api/latest/content
return [isContent ? null : msg, isContent ? msg : null];
"""

PREP_FN = r"""const d = msg.payload || {};
if (!d.url) {
    node.warn('TNews /api/latest 返回异常：' + JSON.stringify(d).slice(0, 120));
    return null;
}
const cfg = global.get('tncfg') || {};
global.set('tnews', {
    id: d.id,
    title: d.title || '',
    url: d.url,
    duration: Number(d.duration) || 0,
    item_count: d.item_count || 0,
    created_at: d.created_at || ''
});
node.status({ fill: 'green', shape: 'dot', text: d.title || '' });
return [
    { payload: d.title || '', tncfg: cfg },
    { payload: d.url, tncfg: cfg }
];
"""

BUILD_FN = r"""const t = global.get('tnews');
if (!t || !t.url) {
    node.warn('还没有最新一期，等抓取节点跑一次');
    return null;
}
const cfg = global.get('tncfg') || {};
const PLAYER = cfg.player || 'media_player.xiaomi_speaker';
const durMs = Math.max(30, Number(t.duration) || 0) * 1000;
const stream = t.url + (t.url.includes('?') ? '&' : '?') + 'inline=1';  // 内联流式，避免 302 与 attachment 头导致反复拉流
// 提前量：在音频结束【之前】就暂停。
// 原来用 dur + 5 秒缓冲，结果第一遍播完后设备单曲循环、从头又放 5 秒才被掐掉，
// 听感上就是「明明结束了又重播一句」。改成提前 LEAD_MS 停，宁可少 0.8 秒尾巴。
// 如果某台设备起播很慢（听到被提前掐掉），把 LEAD_MS 调大到 1500~2000。
const LEAD_MS = 800;
const waitMs = Math.max(5000, durMs - LEAD_MS);
// 关键：新版 api-call-service 只认 payload.target.entity_id（数组），
// 把 entity_id 放在 payload 顶层会被忽略，HA 报 "at least one of [...] is required"。
msg.payload = {
    entity_id: PLAYER,                        // 老版本兼容
    target: { entity_id: [PLAYER] },          // 新版节点真正读取的位置
    data: {
        entity_id: PLAYER,                    // 再兜一层：老 HA 服务数据也认
        media_content_id: stream,
        media_content_type: 'music'
    }
};
msg.wait = String(waitMs);
msg.tncfg = cfg;                              // 下游 api-call-service 用 {{{tncfg.player}}}
global.set('tnewsWait', waitMs);              // 供计时节点使用（不依赖消息透传）
node.status({ fill: 'blue', shape: 'dot', text: (t.title || '') + ' · ' + Math.round(durMs / 1000) + 's，' + (waitMs / 1000).toFixed(1) + 's 后暂停' });
return msg;
"""

TIMER_FN = r"""// 按本期时长计时，到点发出暂停消息；重复点击会重置计时（不会出现旧定时把新播放掐掉）
const cfg = global.get('tncfg') || {};
const PLAYER = cfg.player || 'media_player.xiaomi_speaker';
const wait = Number(global.get('tnewsWait') || (msg && msg.wait) || 0);
if (!wait) {
    node.warn('没拿到本期时长，跳过自动暂停');
    return null;
}
const prev = context.get('timer');
if (prev) {
    clearTimeout(prev);
    node.status({ fill: 'yellow', shape: 'ring', text: '重新计时' });
}
const timer = setTimeout(function () {
    context.set('timer', null);
    node.send({ payload: { entity_id: PLAYER, target: { entity_id: [PLAYER] } }, tncfg: cfg });
}, wait);
context.set('timer', timer);
node.status({ fill: 'blue', shape: 'dot', text: Math.round(wait / 1000) + ' 秒后暂停' });
return null;
"""

SANITIZE_FN = r"""// catch 传来的是「出错那一步的原始消息」，里面还带着 data.media_content_id，
// 直接丢给下游会报 "not a valid option at 'media_content_id'"，所以整体重建 payload。
// 兜底动作是 media_play_pause（切换式）：小米 play_control 没有 stop，
// media_pause 在部分固件上也不认，但「播放/暂停切换」是可用的。
const cfg = global.get('tncfg') || {};
const PLAYER = cfg.player || 'media_player.xiaomi_speaker';
const e = (msg && msg.error) || {};
msg.errNode = (e.node && (e.node.name || e.node.id)) || '(未知节点)';
msg.errMsg = e.message || (typeof e === 'string' ? e : '') || '(无错误信息)';
msg.payload = {
    entity_id: PLAYER,
    target: { entity_id: [PLAYER] }
};
msg.tncfg = cfg;
delete msg.data;
delete msg.media_content_id;
delete msg.media_content_type;
delete msg.topic;
node.status({ fill: 'yellow', shape: 'ring', text: msg.errNode + ' 失败 → 切换播放/暂停' });
return msg;
"""

CMK_FN = r"""// 地址、令牌、实体名都在「⚙️ 统一配置」节点里改，这里不用动
const cfg = global.get('tncfg') || {};
const HATOKEN = String(cfg.ha_token || '');

const d = msg.payload || {};
if (!d.items || !d.items.length) {
    node.warn('TNews /api/latest/content 返回异常：' + JSON.stringify(d).slice(0, 120));
    return null;
}
// ---- 拼 Markdown 全文（写进 sensor 属性，无长度限制）----
const md = [];
md.push('## ' + (d.title || '要闻速听'));
const when = String(d.created_at || '').replace('T', ' ').slice(0, 16);
md.push('');
md.push('更新于 ' + when + ' · 共 ' + (d.item_count || d.items.length) + ' 条');
if (d.weather && d.weather.text) {
    md.push('');
    md.push('**🌤 天气** ' + d.weather.text);
}
let lastCat = null;
for (const it of d.items) {
    const cat = it.category_label || '要闻';
    if (cat !== lastCat) {
        lastCat = cat;
        md.push('');
        md.push('### ' + cat);
    }
    md.push('');
    md.push('**' + it.n + '. ' + (it.title || '') + '**');
    if (it.text) md.push(it.text);
    const src = (it.sources && it.sources.length ? it.sources.join(' / ') : (it.source || ''));
    const tail = [];
    if (src) tail.push(src);
    if (it.link) tail.push('[原文](' + it.link + ')');
    if (tail.length) md.push('*' + tail.join(' · ') + '*');
}
const full = md.join('\n');
// ---- 简版清单：每条一行「- 标题」，配合 HA 的 Markdown 卡片就是热榜卡片那种样式 ----
const list = d.items.map(i => (i.title ? '- ' + i.title : null)).filter(Boolean).join('\n');
// ---- 255 字内摘要（兼容旧 input_text 方案）----
let brief = (d.title || '') + '：' + d.items.map(i => i.title).filter(Boolean).join('；');
if (brief.length > 255) brief = brief.slice(0, 254) + '…';
// ---- 令牌体检：占位符 / 带引号 / 带 Bearer 前缀 / 带空白，都会让请求头非法（ERR_INVALID_CHAR）----
const token = HATOKEN.trim().replace(/^Bearer\s+/i, '').replace(/^['"]|['"]$/g, '').trim();
if (!token || /[\u4e00-\u9fff]/.test(token) || /\s/.test(token)) {
    node.status({ fill: 'red', shape: 'ring', text: 'HA 长期令牌未正确填写' });
    node.error('「⚙️ 统一配置」里的 ha_token 还没换成真实令牌（现在收到的是「' + token.slice(0, 12) + '」）。填法：HA → 左下角头像 → 安全 → 最底部「长期访问令牌」→ 创建令牌，把整串原样粘到引号内——不要加 Bearer、不要加引号、不要换行。本次跳过全文写入，摘要照常。');
    return [null, { payload: brief, tncfg: cfg }];
}

node.status({ fill: 'green', shape: 'dot', text: (d.title || '') + ' · ' + d.items.length + ' 条' });
const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
return [
    {   // ① 直写 HA REST API：sensor.<…>，content 属性放 Markdown 全文
        url: (cfg.ha || '') + '/api/states/' + (cfg.sensor || 'sensor.tnews_content'),
        tncfg: cfg,
        headers: {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json'
        },
        payload: {
            state: String(d.item_count || d.items.length),
            attributes: {
                friendly_name: 'TNews 最新内容',
                title: d.title || '',
                content: full,
                brief: brief,
                list: list,
                audio_url: d.url || '',
                updated_at: now
            }
        }
    },
    {   // ② 可选：写 input_text 摘要（摘要版，需在 HA 建同名辅助元素）
        payload: brief,
        tncfg: cfg
    }
];
"""

# ---- 新增「⚙️ 统一配置」节点 ----
cfg_node = {
    "id": "tn_cfg",
    "type": "function",
    "z": "tnews",
    "name": "⚙️ 统一配置（只改这里：地址/账号/令牌/实体）",
    "func": CFG_FN,
    "outputs": 2,
    "timeout": 0,
    "noerr": 0,
    "initialize": "",
    "finalize": "",
    "libs": [],
    "x": 390,
    "y": 20,
    "wires": [["tn_http"], ["tn_chttp"]],
}
# 先按 id 去重（老版本脚本每次跑都会插一个副本），再原地替换，保证脚本可重复执行
_seen: set[str] = set()
_unique = []
for _n in nodes:
    _id = _n.get("id")
    if _id in _seen:
        continue
    _seen.add(_id)
    _unique.append(_n)
nodes = _unique

_idx = next((i for i, n in enumerate(nodes) if n.get("id") == "tn_cfg"), None)
if _idx is None:
    nodes.insert(1, cfg_node)
else:
    nodes[_idx] = cfg_node
by_id = {n.get("id"): n for n in nodes}

# 1) 两个 inject 带上 topic 区分用途
by_id["tn_fetch"]["topic"] = "latest"
by_id["tn_fetch"]["wires"] = [["tn_cfg"]]
by_id["tn_cfetch"]["topic"] = "content"
by_id["tn_cfetch"]["wires"] = [["tn_cfg"]]

# 2) http 节点 URL 留空 → 由配置节点给的 msg.url 决定
by_id["tn_http"]["url"] = ""
by_id["tn_http"]["name"] = "GET /api/latest（地址见⚙️配置）"
by_id["tn_http"]["x"], by_id["tn_http"]["y"] = 660, 100
by_id["tn_chttp"]["url"] = ""
by_id["tn_chttp"]["name"] = "GET /api/latest/content（地址见⚙️配置）"
by_id["tn_chttp"]["x"], by_id["tn_chttp"]["y"] = 690, 560

# 3) 主路函数：带上 cfg，实体名从 cfg 取
by_id["tn_prep"]["func"] = PREP_FN
by_id["tn_prep"]["x"] = 930
by_id["tn_buildplay"]["func"] = BUILD_FN
by_id["tn_timer"]["func"] = TIMER_FN
by_id["tn_sanitize"]["func"] = SANITIZE_FN
by_id["tn_cmk"]["func"] = CMK_FN
by_id["tn_cmk"]["x"] = 930

# 4) 写 input_text / 调服务的 data 改成引用 cfg（mustache）
def set_data(nid, mapping):
    by_id[nid]["data"] = json.dumps(mapping, ensure_ascii=False)
    by_id[nid]["dataType"] = "json"

set_data("tn_set_title", {"entity_id": "{{{tncfg.input_title}}}", "value": "{{{payload}}}"})
set_data("tn_set_url", {"entity_id": "{{{tncfg.input_url}}}", "value": "{{{payload}}}"})
set_data("tn_cset", {"entity_id": "{{{tncfg.input_brief}}}", "value": "{{{payload}}}"})
set_data("tn_play", {"entity_id": "{{{tncfg.player}}}"})
set_data("tn_stop2", {"entity_id": "{{{tncfg.player}}}"})
set_data("tn_fallback", {"entity_id": "{{{tncfg.player}}}"})

# 5) 写 HA 的 http 节点：地址由 tn_cmk 给的 msg.url 决定
by_id["tn_cpost"]["url"] = ""
by_id["tn_cpost"]["name"] = "POST /api/states（地址见⚙️配置）"

# 6) 顺手把被移动节点后面的链路摆整齐
by_id["tn_set_title"]["x"], by_id["tn_set_title"]["y"] = 1180, 60
by_id["tn_set_url"]["x"], by_id["tn_set_url"]["y"] = 1190, 140
by_id["tn_dbg"]["x"], by_id["tn_dbg"]["y"] = 1400, 100
by_id["tn_cpost"]["x"], by_id["tn_cpost"]["y"] = 1180, 510
by_id["tn_cset"]["x"], by_id["tn_cset"]["y"] = 1180, 600
by_id["tn_cdbg"]["x"], by_id["tn_cdbg"]["y"] = 1450, 560

P.write_text(json.dumps(nodes, ensure_ascii=False, indent=4), encoding="utf-8")
print("written", len(nodes), "nodes")
