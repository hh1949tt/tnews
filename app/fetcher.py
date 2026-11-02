"""RSS 抓取、清洗、跨源去重与热点排序。"""
from __future__ import annotations

import asyncio
import calendar
import difflib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime

import feedparser
import httpx
from zhconv import convert as zh_convert

from .config import now_local

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TNews/1.0"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")
_REPORTER_RE = re.compile(r"[（(]\s*(记者|通讯员|中新社记者)[^）)]{0,20}[）)]")
_LEAD_RE = re.compile(
    r"^[\s　]*"
    r"(?:中新网|新华网|人民网|央视新闻|新浪财经|华尔街见闻|IT之家|快科技|36氪|少数派|界面新闻)?"
    r"\s*[\u4e00-\u9fa5A-Za-z]{0,8}\s*"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日(?:电|讯|消息)"
    r"(?:[（(][^）)]{0,30}[）)])?"
)
_AUTHOR_RE = re.compile(r"^[（(][\u4e00-\u9fa5]{2,4}(?:\s+[\u4e00-\u9fa5]{2,4}){0,3}[）)]")
_BOILER_RE = re.compile(
    r"(责任编辑|原标题|标题[:：]|来源[:：]|点击进入专题|扫码关注|更多精彩|关注我们|本文来自|作者[:：]).*$", re.S
)
# 括号内容（记者署名、编者注、日期补充）在朗读时多为噪音，整段去掉
_PAREN_RE = re.compile(r"[（(][^（）()]{0,50}[）)]")
_LEAD_DATE_RE = re.compile(
    r"^(?:记者\s*)?(?:\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*日)[，,、]?\s*"
)


@dataclass
class Item:
    title: str
    summary: str
    link: str
    source: str
    category: str
    published: datetime | None = None
    date_known: bool = True
    sources: list[str] = field(default_factory=list)
    feed_url: str = ""   # 来自哪个订阅源（按占比配额时用）

    @property
    def heat(self) -> int:
        return max(1, len(self.sources))


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text


def _tidy(text: str) -> str:
    """去掉未闭合的括号/引号残片，规范空格，避免念出半句话。"""
    pairs = [("（", "）"), ("(", ")"), ("《", "》"), ("【", "】")]
    for op, cl in pairs:
        if text.count(op) > text.count(cl):
            text = text[: text.rfind(op)]
    if text.count("“") > text.count("”"):
        text = text[: text.rfind("“")]
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，。、：；！？）)》])", r"\1", text)
    text = re.sub(r"([（《【])\s+", r"\1", text)
    text = re.sub(r"([，。、：；！？])\1+", r"\1", text)
    return text.strip("，。、：:；;（( \u3000")


def _strip_dup_title(title: str, text: str) -> str:
    """RSS 正文常以标题开头，去掉重复部分。"""
    if not title or not text:
        return text
    import difflib

    head = text[: len(title) + 24]
    m = difflib.SequenceMatcher(None, head, title).find_longest_match(0, len(head), 0, len(title))
    if m.size >= max(8, int(len(title) * 0.5)) and m.a <= 4:
        return text[m.a + m.size :].lstrip("，。、：:；;）) \u3000")
    return text


def clean_summary(raw: str, title: str = "") -> str:
    text = clean_text(raw)
    text = zh_convert(text, "zh-cn")
    text = _PAREN_RE.sub("", text)
    text = _REPORTER_RE.sub("", text)
    text = _LEAD_RE.sub("", text)
    text = _BOILER_RE.sub("", text)
    text = _strip_dup_title(title, text)
    text = _LEAD_RE.sub("", text)
    text = _LEAD_DATE_RE.sub("", text)
    text = _tidy(text)
    return text.strip(" 。;；,，")


def _norm_key(title: str) -> str:
    """标题归一化，用于跨源判重。"""
    t = re.sub(r"[\s\W_]+", "", title)
    return t


def _similar(a: str, b: str) -> bool:
    """同一篇报道的转载：标题高度一致。"""
    if a == b:
        return True
    if not a or not b:
        return False
    if len(a) > 8 and len(b) > 8 and (a in b or b in a):
        return True
    r = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    if r >= 0.62:
        return True
    sa, sb = set(a), set(b)
    overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
    return overlap >= 0.85 and abs(len(a) - len(b)) <= max(4, len(a) // 4)


def _same_event(a: str, b: str) -> bool:
    """同一事件的不同角度报道（放宽到主体+相似度）。"""
    if not a or not b:
        return False
    r = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    if r >= 0.42:
        return True
    return len(a) >= 3 and len(b) >= 3 and a[:3] == b[:3] and r >= 0.32


WEIBO_HOT_URL = "https://weibo.com/ajax/side/hotSearch"


async def fetch_weibo_hot(client: httpx.AsyncClient, name: str, category: str, url: str = "weibo://hot") -> list[Item]:
    """微博实时热搜（JSON 接口，需要带 Referer 才不返回 Forbidden）。"""
    try:
        resp = await client.get(
            WEIBO_HOT_URL,
            headers={
                "User-Agent": UA,
                "Referer": "https://weibo.com/",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    items: list[Item] = []
    now = now_local()
    for it in (data.get("data") or {}).get("realtime") or []:
        if it.get("is_ad"):
            continue  # 广告位不播
        word = zh_convert(clean_text(it.get("word") or ""), "zh-cn").strip()
        if len(word) < 4:
            continue
        num = it.get("num")
        try:
            summary = f"微博实时热搜，热度约 {round(int(num) / 10000)} 万。"
        except (TypeError, ValueError):
            summary = "微博实时热搜话题。"
        items.append(
            Item(
                title=word,
                summary=summary,
                link=f"https://s.weibo.com/weibo?q=%23{word}%23",
                source=name,
                category=category,
                published=now,
                date_known=False,
                sources=[name],
                feed_url=url,
            )
        )
    return items[:40]


async def fetch_feed(client: httpx.AsyncClient, name: str, url: str, category: str) -> list[Item]:
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception:
        return []

    items: list[Item] = []
    now = now_local()
    for entry in parsed.entries[:40]:
        title = clean_text(entry.get("title", ""))
        title = zh_convert(title, "zh-cn").strip()
        if len(title) < 6:
            continue

        published = None
        date_known = True
        for key in ("published_parsed", "updated_parsed"):
            st = entry.get(key)
            if st:
                try:
                    published = datetime.fromtimestamp(calendar.timegm(st))
                    break
                except Exception:
                    pass
        if published is None:
            date_known = False
            published = now

        raw_summary = entry.get("summary") or entry.get("description") or ""
        if not raw_summary and entry.get("content"):
            try:
                raw_summary = entry["content"][0].get("value", "")
            except Exception:
                raw_summary = ""

        items.append(
            Item(
                title=title,
                summary=clean_summary(raw_summary, title),
                link=entry.get("link", ""),
                source=name,
                category=category,
                published=published,
                date_known=date_known,
                sources=[name],
                feed_url=url,
            )
        )
    return items


async def collect(
    feed_plan: list[tuple[str, str, str]],
    start: datetime,
    end: datetime,
    timeout: float = 15.0,
    per_category_limit: int = 40,
) -> dict[str, list[Item]]:
    """抓取全部订阅源，按分类返回去重、去旧、按热度排序后的条目。"""
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}) as client:
        tasks = []
        for name, url, cat in feed_plan:
            # weibo:// 是内置特殊源（非 RSS），走单独的 JSON 接口。
            # 必须把配置里的 url 传进去：Item.feed_url 要能对得上配置，
            # 否则后台按占比裁剪时会认为"配置的源一条都没抓到"，占比静默失效。
            tasks.append(
                fetch_weibo_hot(client, name, cat, url)
                if url.startswith("weibo://")
                else fetch_feed(client, name, url, cat)
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)

    buckets: dict[str, list[Item]] = {}
    for res in results:
        if isinstance(res, Exception):
            continue
        for it in res:
            buckets.setdefault(it.category, []).append(it)

    merged: dict[str, list[Item]] = {}
    for cat, items in buckets.items():
        merged[cat] = _dedupe_and_rank(items, start, end, per_category_limit)
    return merged


def _dedupe_and_rank(items: list[Item], start: datetime, end: datetime, limit: int) -> list[Item]:
    # 1) 时间窗口过滤（热搜等无真实发布时间的条目不参与过滤）
    in_window = [i for i in items if not i.date_known or start <= i.published <= end]
    if len(in_window) < 3:
        # 窗口太窄（比如定时任务延迟/时区偏差）时放宽到窗口起点前 6 小时
        from datetime import timedelta

        widened = start - timedelta(hours=6)
        in_window = [i for i in in_window if not i.date_known or widened <= i.published <= end]

    # 2) 跨源聚类
    clusters: list[Item] = []
    for item in in_window:
        key = _norm_key(item.title)
        hit = None
        for c in clusters:
            if _similar(_norm_key(c.title), key):
                hit = c
                break
        if hit is None:
            clusters.append(item)
        else:
            if item.source not in hit.sources:
                hit.sources.append(item.source)
            # 保留正文更完整的那条
            if len(item.summary) > len(hit.summary):
                hit.summary = item.summary
                hit.link = item.link or hit.link
            if item.published and (hit.published is None or item.published < hit.published):
                hit.published = item.published
            if not item.date_known:
                hit.date_known = False

    # 3) 事件级聚合：同一事件的多个后续报道只保留信息量最大的两条，避免一件事念五遍
    ranked = sorted(clusters, key=lambda i: (-i.heat, -(i.published.timestamp() if i.published else 0)))
    kept: list[Item] = []
    prefix_seen: dict[str, int] = {}
    for item in ranked:
        key = _norm_key(item.title)
        if any(_same_event(_norm_key(k.title), key) for k in kept[-8:]):
            continue
        prefix = key[:3]
        if prefix and prefix_seen.get(prefix, 0) >= 2:
            continue
        kept.append(item)
        if prefix:
            prefix_seen[prefix] = prefix_seen.get(prefix, 0) + 1
    return kept[:limit]
