"""把抓到的新闻条目编排成可朗读的口播稿。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import httpx

from .config import Config, Program, now_local
from .fetcher import Item, _norm_key, _similar

log = logging.getLogger("tnews.summarizer")

SORTS = {"category": "按分类", "date": "按时间", "source": "按平台"}

SECTION_LEAD = {
    "domestic": "先来看国内。",
    "world": "国际方面。",
    "finance": "财经市场。",
    "tech": "科技动态。",
    "society": "社会民生。",
    "hot": "最后来看热搜榜。",
}

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")

_CN_NUM = "零一二三四五六七八九"


def cn_num(n: int) -> str:
    """1~99 的中文数字，用于口播编号（第一条、第十二条…）。"""
    if n <= 0 or n >= 100:
        return str(n)
    if n < 10:
        return _CN_NUM[n]
    tens, ones = divmod(n, 10)
    if tens == 1:
        return "十" + (_CN_NUM[ones] if ones else "")
    return f"{_CN_NUM[tens]}十" + (_CN_NUM[ones] if ones else "")


def _trim(text: str, limit: int) -> str:
    """按句子边界截断到 limit 字以内，避免半句话。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    out = ""
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        if len(out) + len(sent) > limit:
            break
        out += sent
    if not out:
        cut = text[:limit]
        # 退到最近的逗号，读起来更自然
        for punct in ("，", "、", " ", ","):
            idx = cut.rfind(punct)
            if idx > limit * 0.6:
                cut = cut[:idx]
                break
        out = cut
    return out.rstrip("，、,。 ")


def _hm(h: int, m: int) -> str:
    return f"{h} 点" if m == 0 else f"{h} 点 {m} 分"


def _fmt_window(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"今天 {_hm(start.hour, start.minute)}到 {_hm(end.hour, end.minute)}"
    return (
        f"{start.month} 月 {start.day} 日 {_hm(start.hour, start.minute)}，"
        f"到 {end.month} 月 {end.day} 日 {_hm(end.hour, end.minute)}"
    )


def _covers_title(title: str, body: str) -> bool:
    """判断正文是否已经复述了标题，避免播报时同一句话念两遍。"""
    import difflib

    t = re.sub(r"[\s\W_]+", "", title)
    b = re.sub(r"[\s\W_]+", "", body)
    if not t or not b:
        return False
    if t in b:
        return True
    m = difflib.SequenceMatcher(None, t, b).find_longest_match(0, len(t), 0, len(b))
    return m.size >= max(8, int(len(t) * 0.6))


def _llm_rewrite(items: list[Item], cfg: Config) -> dict[str, str]:
    """可选：用大模型把新闻改写成口语播报句。失败返回空字典。"""
    llm = cfg.digest.get("llm") or {}
    if not llm.get("enabled"):
        return {}
    api_key = os.environ.get(llm.get("api_key_env", "LLM_API_KEY"), "")
    if not api_key:
        return {}

    payload_items = [
        {"id": i, "title": it.title, "summary": it.summary[:200], "source": it.source}
        for i, it in enumerate(items)
    ]
    prompt = (
        "你是中文新闻播报编辑。把下面的新闻逐条改写成一句适合开车收听的口语播报，"
        "每条 40 到 80 字，客观、简洁、不带评论，不要出现“这条新闻”“据报道”等赘词，"
        "保留关键数字与主体。只输出 JSON 数组，元素为 {\"id\": 序号, \"text\": \"播报句\"}。\n\n"
        + json.dumps(payload_items, ensure_ascii=False)
    )
    try:
        resp = httpx.post(
            llm["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": llm.get("model", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        data = json.loads(content)
        return {
            str(d["id"]): str(d["text"]).strip()
            for d in data
            if isinstance(d, dict) and "id" in d and "text" in d
        }
    except Exception:
        return {}


def build_script(
    program: Program,
    cfg: Config,
    buckets: dict[str, list[Item]],
    start: datetime,
    end: datetime,
    now: datetime | None = None,
    max_items: int | None = None,
    sort: str = "category",
    weather: dict | None = None,  # 今日天气播报（未开启为 None）
    uncapped: set[str] | None = None,  # 设置了占比的源名，不受「同一平台最多 N 条」限制
) -> dict:
    now = now or now_local()
    max_items = int(max_items or cfg.digest.get("max_items", 14))
    max_chars = int(cfg.digest.get("max_chars_per_item", 120))
    min_chars = int(cfg.digest.get("min_chars_per_item", 24))
    sort = sort if sort in SORTS else "category"

    # 按分类配额分配条数，保证同一分类连续播报、章节不交叉
    active_cats = [c for c in program.categories if buckets.get(c)]
    if not active_cats:
        active_cats = [c for c in buckets if buckets[c]]

    per_cat: dict[str, list[Item]] = {}
    for cat in active_cats:
        # 只有标题没有正文的条目播出来太空，优先排除；不够用时再放宽
        solid = [it for it in buckets.get(cat, []) if len(it.summary) >= min_chars]
        loose = [it for it in buckets.get(cat, []) if len(it.summary) >= 8]
        per_cat[cat] = solid or loose

    def _cap_sources(pairs: list[tuple[str, Item]], cap: int, counts: dict[str, int]):
        """同一平台最多 cap 条（counts 跨轮共享），超出顺延；保持原有顺序。
        设置了占比的源（uncapped）不参与该上限，否则占比会被砍掉。"""
        kept: list[tuple[str, Item]] = []
        rest: list[tuple[str, Item]] = []
        for kv in pairs:
            src = kv[1].source or kv[0] or "其他"
            if uncapped and src in uncapped:
                kept.append(kv)
                continue
            if counts.get(src, 0) >= cap:
                rest.append(kv)
            else:
                counts[src] = counts.get(src, 0) + 1
                kept.append(kv)
        return kept, rest

    if sort in ("date", "source"):
        # 全部候选合并，跨分类去重后统一排序
        pool: list[tuple[str, Item]] = []
        seen: list[str] = []
        for cat in active_cats:
            for it in per_cat[cat]:
                key = _norm_key(it.title)
                if key and any(_similar(key, s) for s in seen):
                    continue
                if key:
                    seen.append(key)
                if sort == "date":
                    pool.append((it.category or cat, it))
                else:
                    pool.append((it.source or "其他来源", it))

        # 同一平台条数设上限，避免一期节目被单一来源（如微博热搜）刷屏；
        # 只有当条目严重不足（<5 条）时才放宽一次，宁可短一点也要来源多样
        src_cap = max(2, max_items // 3)
        counts: dict[str, int] = {}
        picked_list, rest = _cap_sources(pool, src_cap, counts)
        if len(picked_list) < 5 and rest:
            more, rest = _cap_sources(rest, src_cap + 3, counts)
            picked_list.extend(more)
        pool = picked_list

        if sort == "date":
            pool.sort(key=lambda kv: kv[1].published or datetime.min, reverse=True)
            picked = pool[:max_items]
        else:
            groups: dict[str, list[Item]] = {}
            for src, it in pool:
                groups.setdefault(src, []).append(it)
            ordered: list[tuple[str, Item]] = []
            for src, items in sorted(
                groups.items(),
                key=lambda kv: (
                    -len(kv[1]),
                    max((i.published or datetime.min) for i in kv[1]),
                ),
            ):
                items.sort(key=lambda i: i.published or datetime.min, reverse=True)
                ordered.extend((src, i) for i in items)
            picked = ordered[:max_items]
    else:
        base = max_items // max(1, len(active_cats))
        extra = max_items - base * len(active_cats)
        limits = {cat: base + (1 if i < extra else 0) for i, cat in enumerate(active_cats)}

        # 分类内按热度顺序取，**跨分类按标题判重**：同一篇稿子常同时出现在
        # 「中国新闻网·时政」（国内）和「中国新闻网·滚动」（社会）里，
        # 以前只有 date / source 排序做了去重，按分类播报的口播稿会把同一条念两遍。
        seen: list[str] = []
        cursors = {cat: 0 for cat in active_cats}
        taken: dict[str, list[Item]] = {cat: [] for cat in active_cats}
        dup_dropped = 0

        def _take(cat: str, quota: int) -> int:
            """从 cat 的候选里按顺序取至多 quota 条，跳过与其他分类重复的标题。"""
            nonlocal dup_dropped
            lst = per_cat[cat]
            i, got = cursors[cat], 0
            while i < len(lst) and got < quota:
                it = lst[i]
                i += 1
                key = _norm_key(it.title)
                if key and any(_similar(key, s) for s in seen):
                    dup_dropped += 1
                    continue
                if key:
                    seen.append(key)
                taken[cat].append(it)
                got += 1
            cursors[cat] = i
            return got

        for cat in active_cats:
            _take(cat, limits[cat])

        # 某些分类条目不足（或全是重复稿）时空出的名额，轮流补给还有余量的分类
        total_taken = sum(len(v) for v in taken.values())
        while total_taken < max_items:
            progressed = False
            for cat in active_cats:
                if total_taken >= max_items:
                    break
                if cursors[cat] >= len(per_cat[cat]):
                    continue
                if _take(cat, 1):
                    total_taken += 1
                    progressed = True
            if not progressed:
                break

        picked = [(cat, it) for cat in active_cats for it in taken[cat]]

        # 按分类同样防单一平台刷屏（如热榜全是微博）
        src_cap = max(2, max_items // 3)
        counts: dict[str, int] = {}
        capped, rest = _cap_sources(picked, src_cap, counts)
        if len(capped) < max_items and rest:
            # 被「同一平台上限」挤下来的条目按顺序补回来（同样跳过重复标题），
            # 否则目标条数会因为防刷屏而缩水
            kept = [_norm_key(it.title) for _c, it in capped]
            more: list[tuple[str, Item]] = []
            for i, (cat, it) in enumerate(rest):
                if len(capped) + len(more) >= max_items:
                    break
                key = _norm_key(it.title)
                if key and any(_similar(key, k) for k in kept if k):
                    dup_dropped += 1
                    continue
                if key:
                    kept.append(key)
                more.append((cat, it))
            capped.extend(more)
        picked = capped
        # 补位是从各分类交叉挑的，按分类顺序稳定排序，保证同一分类连续播报
        order = {cat: i for i, cat in enumerate(active_cats)}
        picked.sort(key=lambda kv: order.get(kv[0], len(order)))
        if dup_dropped:
            log.info("跨分类去重：跳过 %d 条标题重复的转载稿", dup_dropped)

    items = [it for _, it in picked]
    rewritten = _llm_rewrite(items, cfg)

    segments: list[dict] = []
    date_str = f"{now.month} 月 {now.day} 日"
    segments.append(
        {
            "type": "intro",
            "text": (
                f"这里是{program.name}。"
                f"{date_str}，为您汇总 {_fmt_window(start, end)} 的 {len(picked)} 条要闻。"
            ),
        }
    )

    if weather and weather.get("text"):
        # 天气段紧跟开场白，像主播随口带一句今天天气
        segments.append({"type": "weather", "text": weather["text"]})

    current_key = None
    section_no = 0
    for idx, (key, it) in enumerate(picked):
        if sort != "date" and key != current_key:
            current_key = key
            section_no = 0
            count = sum(1 for k, _ in picked if k == key)
            cnt_txt = cn_num(count) if count < 100 else str(count)
            if sort == "source":
                # 按平台排序：段落引言报来源名
                text = f"接下来是{key}的报道，共{cnt_txt}条。"
                label = key
            else:
                label = cfg.category_label(key)
                base = SECTION_LEAD.get(key, f"{label}方面。").rstrip("。")
                text = f"{base}，共{cnt_txt}条。"
            segments.append(
                {
                    "type": "section",
                    "text": text,
                    "category": it.category or key,
                    "label": label,
                }
            )
        section_no += 1
        if (it.category or key) == "hot":
            # 热搜词条本身就是内容，不再拼「微博实时热搜，热度约…」的说明
            body = it.title if len(it.title) >= 12 else f"{it.title}登上热搜"
        else:
            body = rewritten.get(str(idx)) or _trim(it.summary, max_chars)
            if not body or len(body) < 12:
                body = it.title
            elif not _covers_title(it.title, body):
                # 正文没复述标题时，先把标题念出来，听众才知道在说什么事
                sep = "" if it.title[-1] in "。！？!?；;" else "。"
                body = f"{it.title}{sep}{body}"

        segments.append(
            {
                "type": "item",
                "text": f"第{cn_num(section_no)}条，{body}",
                "no": section_no,
                "title": it.title,
                "source": it.source,
                "sources": it.sources,
                "link": it.link,
                "category": it.category or key,
                "published": it.published.isoformat() if it.published else None,
            }
        )

    segments.append(
        {
            "type": "outro",
            "text": "以上就是本期全部内容。愿你今天顺利愉快，我们下一期见。",
        }
    )

    total_chars = sum(len(s["text"]) for s in segments)
    result = {
        "title": f"{program.name} · {date_str}",
        "program_id": program.id,
        "program_name": program.name,
        "sort": sort,
        "segments": segments,
        "item_count": len(picked),
        "chars": total_chars,
        "est_seconds": int(total_chars / 5.2),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "created_at": now.isoformat(timespec="seconds"),
    }
    if weather and weather.get("text"):
        result["weather"] = weather
    return result
