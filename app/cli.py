"""命令行工具：手动生成一期节目，便于排查问题或补录。

用法：
    python -m app.cli list                 查看节目配置
    python -m app.cli gen morning          生成“要闻速听”
    python -m app.cli gen evening --cats domestic,world
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from . import pipeline
from .config import BASE_DIR, load_config
from .store import Store


async def _gen(program_id: str, cats: list[str] | None):
    cfg = load_config()
    program = cfg.get_program(program_id)
    if not program:
        print(f"找不到节目 {program_id}，可选：{[p.id for p in cfg.programs]}")
        return 1
    store = Store(Path(BASE_DIR / "data" / "tnews.db"))
    ep = await pipeline.generate_episode(program, cfg, store, cats)
    print(f"\n第 {ep['id']} 期已生成：{ep['title']}")
    print(f"音频：data/audio/{ep['audio_file']}  时长 {ep['duration']:.0f} 秒  共 {ep['script']['item_count']} 条")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="tnews")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出节目配置")
    g = sub.add_parser("gen", help="生成一期节目")
    g.add_argument("program", help="节目 id")
    g.add_argument("--cats", default="", help="分类，逗号分隔，默认用配置里的")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
    cfg = load_config()

    if args.cmd == "gen":
        cats = [c.strip() for c in args.cats.split(",") if c.strip()] or None
        return asyncio.run(_gen(args.program, cats))

    print("节目配置：")
    for p in cfg.programs:
        win = p.window or {"lookback_hours": p.lookback_hours}
        print(f"  {p.id:10} {p.name:10} cron={p.cron:12} 分类={p.categories} 窗口={win}")
    print("\n分类：")
    for k, c in cfg.categories.items():
        print(f"  {k:10} {c.label:6} {len(c.feeds)} 个源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
