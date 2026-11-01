"""配置加载与时间窗口计算。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
# 路径都可用环境变量覆盖（容器里常用）；NEWSRADIO_* 是旧版名字，保留兼容一段时间
CONFIG_PATH = Path(os.environ.get("TNEWS_CONFIG", os.environ.get("NEWSRADIO_CONFIG", BASE_DIR / "config.yaml")))
DATA_DIR = Path(os.environ.get("TNEWS_DATA", os.environ.get("NEWSRADIO_DATA", BASE_DIR / "data")))
AUDIO_DIR = DATA_DIR / "audio"
WEB_DIR = BASE_DIR / "web"

# 宿主机目录挂载进来时容器内的 /app/data/audio 可能不存在，这里兜底建一次
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# 固定东八区：国内无夏令时，不依赖系统 tzdata，容器里也不会跑偏
CST = timezone(timedelta(hours=8))


def now_local() -> datetime:
    """当前北京时间（naive，便于与 RSS 中解析出的本地时间比较）。"""
    return datetime.now(CST).replace(tzinfo=None)


@dataclass
class Feed:
    name: str
    url: str
    share: int = 0   # 生成占比（%），0/缺省 = 不参考


@dataclass
class Category:
    key: str
    label: str
    desc: str = ""
    feeds: list[Feed] = field(default_factory=list)


def resolve_hhmm_window(start_hhmm: str, end_hhmm: str, now: datetime) -> tuple[datetime, datetime]:
    """由 "08:00" / "18:00" 推出具体日期：end 取最近一次过去的时刻，start 早于 end。"""
    end_t = datetime.strptime(end_hhmm, "%H:%M").time()
    start_t = datetime.strptime(start_hhmm, "%H:%M").time()
    end_dt = now.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
    if end_dt > now:
        end_dt -= timedelta(days=1)
    start_dt = end_dt.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
    if start_dt >= end_dt:
        start_dt -= timedelta(days=1)
    return start_dt, end_dt


@dataclass
class Program:
    id: str
    name: str
    cron: str
    categories: list[str]
    lookback_hours: float = 12
    window: dict[str, str] | None = None
    crons: list[str] = field(default_factory=list)

    @property
    def cron_list(self) -> list[str]:
        """一天里所有生成时刻：配了 crons 就用 crons，否则退回单个 cron。"""
        return [c for c in (self.crons or []) if c] or [self.cron]

    def resolve_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """返回 (start, end)：本期节目要抓取的时间范围。"""
        now = now or now_local()
        if self.window and self.window.get("start") and self.window.get("end"):
            return resolve_hhmm_window(self.window["start"], self.window["end"], now)
        return now - timedelta(hours=float(self.lookback_hours)), now


class Config:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.app_name = str(raw.get("app_name") or "TNews").strip() or "TNews"
        # 项目主页：显示在首页底部，同时也是管理员「检查更新」的仓库来源；留空则都不显示
        self.repo_url = str(raw.get("repo_url") or "").strip().rstrip("/")
        self.server = raw.get("server", {}) or {}
        self.tts = raw.get("tts", {}) or {}
        self.digest = raw.get("digest", {}) or {}
        self.auto_update = raw.get("auto_update", {}) or {}
        self.retention_days = int(raw.get("retention_days", 14))
        # 生成完成后可选的 webhook 通知（Home Assistant 等外部系统用）
        self.home_assistant = raw.get("home_assistant", {}) or {}
        # 首页访客：未登录打开首页看谁的节目（None = 老配置没写这一段，保持旧行为）
        self.guest = raw.get("guest")

        self.categories: dict[str, Category] = {}
        for key, val in (raw.get("categories") or {}).items():
            feeds = [
                Feed(
                    name=f["name"],
                    url=f["url"],
                    share=int(f.get("share") or 0),
                )
                for f in val.get("feeds", [])
            ]
            self.categories[key] = Category(
                key=key, label=val.get("label", key), desc=val.get("desc", ""), feeds=feeds
            )

        self.programs: list[Program] = []
        for p in raw.get("programs") or []:
            self.programs.append(
                Program(
                    id=p["id"],
                    name=p.get("name", p["id"]),
                    cron=p.get("cron", "0 8 * * *"),
                    categories=[c for c in p.get("categories", []) if c in self.categories],
                    lookback_hours=float(p.get("lookback_hours", 12)),
                    window=p.get("window"),
                    crons=[str(c) for c in (p.get("crons") or []) if c],
                )
            )

    @property
    def public_base_url(self) -> str:
        return (self.server.get("public_base_url") or "").rstrip("/")

    def get_program(self, program_id: str) -> Program | None:
        for p in self.programs:
            if p.id == program_id:
                return p
        return None

    def category_label(self, key: str) -> str:
        c = self.categories.get(key)
        return c.label if c else key


_cached: Config | None = None


def load_config(force: bool = False) -> Config:
    global _cached
    if _cached is None or force:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"找不到配置文件：{CONFIG_PATH}\n"
                "请把仓库里的 config.yaml 放到该位置，或用环境变量 TNEWS_CONFIG 指定路径。"
            )
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _cached = Config(raw)
    return _cached
