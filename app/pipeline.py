"""生成一期节目的完整流水线。"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import fetcher, summarizer, tts, weather
from .config import AUDIO_DIR, Category, Config, Program, now_local
from .store import Store

log = logging.getLogger("tnews.pipeline")


class BusyError(RuntimeError):
    pass


@dataclass
class GenOptions:
    """页面「生成新一期」里可覆盖的参数；不填的项回落到 config.yaml。"""

    categories: list[str] | None = None
    feeds: list[str] | None = None  # 只抓这些源的 url；None = 不限制
    window_start: datetime | None = None
    window_end: datetime | None = None
    lookback_hours: float | None = None  # 最近 N 小时，优先于 window
    rate: str | None = None
    max_items: int | None = None
    target_minutes: float | None = None  # 目标时长（分钟），按它估算条数；优先于 max_items
    sort: str | None = None  # category=按分类 / date=按时间 / source=按平台
    voice: str | None = None  # 本期音色；空 = 用 config.yaml 默认
    user_id: int | None = None  # 属于哪个用户（None = 兼容老数据/管理员）
    shares: dict[str, int] | None = None  # 源 url → 占比%（1~100）；空 = 不参考
    run_km: float | None = None  # 跑步模式：距离（公里）；与 target_minutes 一起生成节奏贴合的节目


_running: set[str] = set()


async def synthesize_weather(
    user_id: int | None, wcfg: dict, cfg: Config, store: Store, rate: str = "", voice: str = "",
) -> dict | None:
    """合成（或复用）「今日天气」独立音频，写入 u{id}:weather_audio 并返回其内容。

    当天已合成过就直接返回已有信息（一天一条）；获取或合成失败返回 None。
    供两处调用：生成节目时顺带合成；用户开启了天气但当天还没有音频时按需补合成。
    """
    uid = user_id or 0
    now = now_local()
    today = now.strftime("%Y-%m-%d")
    # 文件名只用字母数字：不带 - _ 之类符号（部分播放器/自动化对特殊字符不友好）
    name = f"weather{uid}{now.strftime('%Y%m%d')}.mp3"
    old = store.get_setting(f"u{uid}:weather_audio") or {}
    if old.get("date") == today and old.get("file") == name and (AUDIO_DIR / name).exists():
        return old
    try:
        info = await weather.report(float(wcfg["lat"]), float(wcfg["lon"]), str(wcfg.get("place") or ""))
    except Exception as e:
        log.warning("[天气] 获取失败：%s", e)
        return None
    try:
        wx_script = {
            "title": "今日天气",
            "segments": [{"type": "weather", "title": "今日天气", "text": info["text"]}],
        }
        meta = await tts.synthesize(wx_script, cfg, AUDIO_DIR / name, rate=rate, voice=voice)
        data = {
            "date": today, "file": name, "duration": meta.get("duration", 0),
            "text": info["text"], "place": info.get("place", ""),
            "temp": info.get("temp"), "desc": info.get("desc", ""),
            "tmax": info.get("tmax"), "tmin": info.get("tmin"),
        }
        store.set_setting(f"u{uid}:weather_audio", data)
        log.info("[天气] 当日音频已生成：%s（%.0f 秒）", name, meta.get("duration", 0))
        return data
    except Exception as e:
        log.warning("[天气] 音频合成失败（不影响正片）：%s", e)
        return None


def estimate_items(minutes: float, rate: str | None, max_chars: int = 120) -> int:
    """按目标时长估算新闻条数。

    经验值：语速 +10%、每条 120 字封顶时，一期 14 条约 6 分钟出头，
    即每条（含段落引言分摊）约 25 秒。这里按字数和语速折算，宁可少不要超。
    """
    pct = 0.0
    try:
        pct = float(str(rate or "+0%").replace("%", "").replace("+", ""))
    except ValueError:
        pct = 0.0
    speed = max(0.6, 1.0 + pct / 100.0)
    # 每条：正文按 90 字估（很多条不满 120 字）+ 标题复述，再加段间隙
    sec_per_item = (90 + 25) / (5.2 * speed) + 1.5
    # 开场白、结束语、各分类引言
    overhead = 30.0
    n = int((float(minutes) * 60 - overhead) / sec_per_item)
    return max(4, min(80, n))


def _largest_remainder(total: int, weights: dict[str, int]) -> dict[str, int]:
    """按权重把 total 个名额分给各源（最大余数法），权重为 0 的源不分。"""
    keys = [k for k, w in weights.items() if w > 0]
    wsum = sum(weights[k] for k in keys)
    if not keys or wsum <= 0 or total <= 0:
        return {k: 0 for k in weights}
    raw = {k: total * weights[k] / wsum for k in keys}
    alloc = {k: int(raw[k]) for k in keys}
    rest = total - sum(alloc.values())
    by_frac = sorted(keys, key=lambda k: raw[k] - alloc[k], reverse=True)
    for i in range(rest):
        alloc[by_frac[i % len(by_frac)]] += 1
    return {**{k: 0 for k in weights}, **alloc}


def _norm_feed(url: str) -> str:
    """源地址归一化：忽略 http/https、主机大小写、末尾斜杠和空白。

    占比是按 url 存的，配置里写成 `http://…` 还是 `https://…`、末尾带不带 `/`，
    都会让「同一个源」对不上号，表现为占比静默不生效。
    """
    s = str(url or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.rstrip("/").lower()
    # 内置伪协议源：早期版本抓微博热榜时 Item.feed_url 被写死成 weibo://hot，
    # 而配置里是 weibo://hot-search，两边对不上导致占比失效。统一按 scheme 归一，
    # 让旧存档 / 旧数据里这两种写法都能命中同一个源。
    m = re.match(r"^([a-z][a-z0-9+.-]*)://", s)
    if m and m.group(1) not in ("http", "https"):
        return f"{m.group(1)}://"
    return s


def _apply_shares(
    buckets: dict[str, list], shares: dict[str, int], max_items: int
) -> tuple[dict[str, list], set[str]]:
    """按占比给各源分配名额并裁剪 buckets，返回 (裁剪后的 buckets, 占比源的名字集合)。

    - 占比源名额 = round(本期条数 × pct/100)，源内条目不足就少给（不影响其他源）；
    - 多个占比源名额合计超过本期条数时按比例压缩；
    - 剩余名额按候选条数比例分给未设置占比的源，保证占比真的成立。
    """
    # 各源现有候选数（跨分类合并）
    avail: dict[str, int] = {}
    for items in buckets.values():
        for it in items:
            avail[it.feed_url] = avail.get(it.feed_url, 0) + 1

    # 按归一化地址匹配：配置里 http/https、末尾斜杠、主机大小写不同也能命中同一个源
    norm2url = {}
    for items in buckets.values():
        for it in items:
            norm2url.setdefault(_norm_feed(it.feed_url), it.feed_url)
    shared: dict[str, int] = {}
    for u, p in (shares or {}).items():
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        tgt = u if u in avail else norm2url.get(_norm_feed(u))
        if tgt and 0 < p <= 100:
            shared[tgt] = p
    if not shared:
        return buckets, set()

    # 占比源名额（源不够就按现有条数封顶）
    alloc = {u: min(avail[u], max(1, round(max_items * p / 100))) for u, p in shared.items()}
    over = sum(alloc.values()) - max_items
    if over > 0:
        # 名额超额：按比例压缩（保留至少 1 个）
        scaled = _largest_remainder(max_items, {u: alloc[u] for u in alloc})
        alloc = {u: min(alloc[u], max(1, scaled.get(u, 1))) for u in alloc}
        while sum(alloc.values()) > max_items:
            u = max(alloc, key=lambda k: alloc[k])
            alloc[u] -= 1

    rest = max(0, max_items - sum(alloc.values()))
    others = _largest_remainder(rest, {u: avail[u] - alloc.get(u, 0) for u in avail if u not in shared})
    final = {**{u: alloc.get(u, 0) for u in shared}, **{u: min(others.get(u, 0), avail[u] - alloc.get(u, 0)) for u in others}}

    # 按各分类内原有排序（已按热度排）裁剪
    trimmed: dict[str, list] = {}
    for cat, items in buckets.items():
        kept = []
        for it in items:
            u = it.feed_url
            if final.get(u, 0) > 0:
                final[u] -= 1
                kept.append(it)
        if kept:
            trimmed[cat] = kept

    # 占比源不受「同一平台最多 N 条」的防刷屏上限约束
    url2name = {}
    for items in buckets.values():
        for it in items:
            url2name.setdefault(it.feed_url, it.source)
    return trimmed, {url2name[u] for u in shared if u in url2name}


async def generate_episode(
    program: Program,
    cfg: Config,
    store: Store,
    categories: list[str] | None = None,
    now: datetime | None = None,
    opts: GenOptions | None = None,
) -> dict:
    """抓取 → 摘要 → 合成 → 入库。返回完整节目数据。"""
    key = program.id
    if key in _running:
        raise BusyError(f"{program.name} 正在生成中，请稍后再试")
    _running.add(key)
    opts = opts or GenOptions()
    try:
        now = now or now_local()
        wanted = opts.categories if opts.categories is not None else categories
        cats = [c for c in (wanted or program.categories) if c in cfg.categories]
        if not cats:
            cats = list(cfg.categories.keys())

        custom_window = bool(opts.lookback_hours or (opts.window_start and opts.window_end))
        if opts.lookback_hours:
            start, end = now - timedelta(hours=float(opts.lookback_hours)), now
        elif opts.window_start and opts.window_end:
            start, end = opts.window_start, opts.window_end
        else:
            start, end = program.resolve_window(now)

        allowed = set(opts.feeds) if opts.feeds else None
        plan: list[tuple[str, str, str]] = []
        for cat_key in cats:
            cat: Category = cfg.categories[cat_key]
            for feed in cat.feeds:
                if allowed is not None and feed.url not in allowed:
                    continue
                plan.append((feed.name, feed.url, cat_key))

        # 用户在页面上添加的自定义源（按用户存在数据库），合并进抓取计划
        builtin_urls = {url for _n, url, _c in plan}
        for cf in store.custom_feeds(opts.user_id):
            url, name = str(cf.get("url") or ""), str(cf.get("name") or "自定义源")
            cat_key = str(cf.get("category") or "")
            if not url or cat_key not in cats or url in builtin_urls:
                continue
            if allowed is not None and url not in allowed:
                continue
            plan.append((name, url, cat_key))
        if not plan:
            raise RuntimeError("没有选中任何可用的抓取源，请至少勾选一个")

        log.info("[%s] 抓取 %d 个源，窗口 %s ~ %s", program.id, len(plan), start, end)
        buckets = await fetcher.collect(plan, start, end, per_category_limit=40)
        total = sum(len(v) for v in buckets.values())
        log.info("[%s] 窗口内去重后 %d 条", program.id, total)

        # 补录场景：错过定时任务后，原窗口（如昨晚到今早）往往已经没有内容，
        # 自动放宽到最近 12 小时，避免生成一期空节目。用户手动指定窗口时不放宽。
        if total < 3 and not custom_window:
            fb_start, fb_end = now - timedelta(hours=12), now
            log.info("[%s] 原窗口内容不足，放宽到最近 12 小时", program.id)
            buckets = await fetcher.collect(plan, fb_start, fb_end, per_category_limit=40)
            if sum(len(v) for v in buckets.values()) > total:
                start, end = fb_start, fb_end

        program_for_run = Program(
            id=program.id,
            name=program.name,
            cron=program.cron_list[0],
            categories=cats,
            lookback_hours=program.lookback_hours,
            window=program.window,
            crons=program.cron_list,
        )
        rate = opts.rate or cfg.tts.get("rate", "+0%")
        run_mode = bool(opts.run_km and opts.target_minutes)
        max_items = opts.max_items
        if max_items is None and opts.target_minutes:
            max_items = estimate_items(opts.target_minutes, rate,
                                       int(cfg.digest.get("max_chars_per_item", 120)))
            if run_mode:
                # 跑步模式宁多勿少：估算本身有误差，加 10% 余量，保证合成时长不短于跑步用时
                max_items = math.ceil(max_items * 1.10)
            log.info("[%s] 目标时长 %.0f 分钟 ≈ %d 条（语速 %s）",
                     program.id, opts.target_minutes, max_items, rate)

        # 跑步模式：条数必须凑够估算值，不够就逐步扩大时间窗（最多放宽到最近 7 天），
        # 避免合成出来的时长明显短于跑步用时；扩窗也凑不够就用现有条数尽力而为。
        if run_mode and max_items:
            need = int(max_items)
            got = sum(len(v) for v in buckets.values())
            for hours in (24, 48, 96, 168):
                if got >= need:
                    break
                w_start, w_end = now - timedelta(hours=hours), now
                log.info("[%s] 跑步模式条数不足（%d/%d），扩大窗口到最近 %d 小时",
                         program.id, got, need, hours)
                b2 = await fetcher.collect(plan, w_start, w_end,
                                           per_category_limit=60)
                n2 = sum(len(v) for v in b2.values())
                if n2 > got:
                    buckets, got = b2, n2
                    start, end = w_start, w_end
            log.info("[%s] 跑步模式最终窗口 %s ~ %s，可用 %d / 目标 %d 条",
                     program.id, start, end, got, need)

        # 源占比：按各源设置的百分比分配名额（空 = 不参考）
        uncapped: set[str] = set()
        if opts.shares:
            if not max_items:
                # 没指定目标时长时用节目默认条数做分配基数，否则 int(None) 会崩
                max_items = int(cfg.digest.get("max_items", 14))
                log.info("[%s] 未指定条数，源占比按默认 %d 条分配", program.id, max_items)
            buckets, uncapped = _apply_shares(buckets, opts.shares, int(max_items))
            hit = {u: p for u, p in opts.shares.items() if p}
            if uncapped:
                log.info("[%s] 源占比生效：%s", program.id, hit)
            else:
                got = sorted({it.feed_url for items in buckets.values() for it in items})
                log.warning(
                    "[%s] 源占比一个都没生效：请求带 %s，实际抓到的源是 %s（两边对不上）",
                    program.id, list(hit), got[:8],
                )

        # 今日天气：不写进正片，单独合成一条当日音频（前端当日首次播放时前置；
        # 当天已有就直接复用，没有就现场补一条——最新一期可能是昨天生成的）
        if opts.user_id:
            wcfg = store.get_setting(f"u{opts.user_id}:weather") or {}
            if wcfg.get("enabled") and wcfg.get("lat") is not None:
                wx_data = await synthesize_weather(
                    opts.user_id, wcfg, cfg, store,
                    rate=rate, voice=opts.voice or cfg.tts.get("voice", ""),
                )
                if wx_data:
                    log.info("[%s] 天气播报：%s", program.id, wx_data["text"])

        script = summarizer.build_script(
            program_for_run, cfg, buckets, start, end, now,
            max_items=max_items, sort=opts.sort or "category",
            uncapped=uncapped or None,
        )
        if script["item_count"] == 0:
            raise RuntimeError("这个时间段内没有抓到有效新闻，可能是订阅源异常或窗口太窄")

        # 标题期数 = 生成账号在这档节目里的期数（各账号独立计数），与角标、往期列表一致
        ep_no = store.count_program_episodes(program.id, opts.user_id) + 1
        voice = opts.voice or cfg.tts.get("voice", "")

        def _apply_meta(s: dict) -> dict:
            """给 build_script 产出的文稿补上期数标题、语速、跑步信息等统一字段。"""
            s["title"] = f"{s['title']} · 第 {ep_no} 期"
            s["rate"] = rate
            s["voice"] = voice
            if opts.sort:
                s["sort"] = opts.sort
            if opts.target_minutes:
                s["target_minutes"] = float(opts.target_minutes)
            if opts.run_km and opts.target_minutes:
                # 跑步模式：把距离/配速写进节目，播放端据此显示公里进度、切激进 BGM
                km = round(float(opts.run_km), 2)
                pace_sec = int(round(opts.target_minutes * 60 / km))
                s["run"] = {
                    "km": km,
                    "minutes": float(opts.target_minutes),
                    "pace_sec": pace_sec,
                    "pace": f"{pace_sec // 60}'{pace_sec % 60:02d}\"/km",
                }
            # 记下本期实际用的分类与源，下次打开设置面板能按这期参数预填
            s["categories"] = cats
            s["feed_urls"] = sorted({url for _n, url, _c in plan})
            return s

        script = _apply_meta(script)

        # 文件名只用字母数字：<节目><YYYYMMDD><HHMM>.mp3（不带 - 等符号）
        stamp = now.strftime("%Y%m%d%H%M")
        slug = re.sub(r"[^0-9A-Za-z]", "", str(program.id)) or "episode"
        audio_name = f"{slug}{stamp}.mp3"
        audio_path = AUDIO_DIR / audio_name
        log.info("[%s] 开始合成音频，共 %d 条，语速 %s", program.id, script["item_count"], rate)
        meta = await tts.synthesize(script, cfg, audio_path, rate=rate, voice=voice)

        # 跑步模式硬保障：合成完实测时长仍短于总用时 → 按缺口补条数（必要时再扩窗）重新合成，
        # 宁可超过总用时也不能短；最多补 3 轮，仍不足就按实际结果入库并记日志。
        if run_mode and opts.target_minutes:
            need = float(opts.target_minutes) * 60
            try:
                pct = float(str(rate or "+0%").replace("%", "").replace("+", ""))
            except ValueError:
                pct = 0.0
            spd = max(0.6, 1.0 + pct / 100.0)
            for _round in range(3):
                if meta["duration"] >= need:
                    break
                add = max(2, math.ceil((need - meta["duration"]) * spd / 23.0))
                new_cap = int(max_items) + add
                got = sum(len(v) for v in buckets.values())
                if got < new_cap:
                    for hours in (24, 48, 96, 168):
                        w_start, w_end = now - timedelta(hours=hours), now
                        b2 = await fetcher.collect(plan, w_start, w_end,
                                                   per_category_limit=60)
                        n2 = sum(len(v) for v in b2.values())
                        if n2 > got:
                            buckets, got = b2, n2
                            start, end = w_start, w_end
                        if got >= new_cap:
                            break
                log.info("[%s] 跑步模式实测 %.0f 秒 < 目标 %.0f 秒，补 %d 条（上限 %d）重新合成",
                         program.id, meta["duration"], need, add, new_cap)
                script = summarizer.build_script(
                    program_for_run, cfg, buckets, start, end, now,
                    max_items=new_cap, sort=opts.sort or "category",
                    uncapped=uncapped or None,
                )
                max_items = new_cap
                script = _apply_meta(script)
                meta = await tts.synthesize(script, cfg, audio_path, rate=rate, voice=voice)
            if meta["duration"] < need:
                log.warning("[%s] 跑步模式补 %d 轮后仍 %.0f 秒 < %.0f 秒，素材不足，按实际时长入库",
                            program.id, 3, meta["duration"], need)

        episode_id = store.save(script, meta["audio_file"], meta["duration"], meta["chapters"],
                                user_id=opts.user_id)
        log.info("[%s] 完成：第 %s 期，时长 %.0f 秒", program.id, episode_id, meta["duration"])

        data = store.get(episode_id) or {}
        data["script"] = data.get("script") or script
        return data
    finally:
        _running.discard(key)


def cleanup(cfg: Config, store: Store) -> int:
    """删除超过保留期的节目与音频文件。保留天数可在设置里改（0 = 永久保留）。"""
    days = store.get_setting("retention_days")
    try:
        days = int(days) if days is not None else int(cfg.retention_days)
    except (TypeError, ValueError):
        days = int(cfg.retention_days)
    if days <= 0:
        return 0
    cutoff = (now_local() - timedelta(days=days)).isoformat(timespec="seconds")
    files = store.purge_older_than(cutoff)
    removed = 0
    for name in files:
        path: Path = AUDIO_DIR / name
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass

    # 天气独立音频按天覆盖，但换天/换账号后会残留，顺手清掉 7 天前的
    cutoff_day = (now_local() - timedelta(days=7)).strftime("%Y%m%d")
    for path in AUDIO_DIR.glob("weather*.mp3"):
        try:
            digits = "".join(ch for ch in path.stem if ch.isdigit())
            day = digits[-8:]
            if day.isdigit() and day < cutoff_day:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed
