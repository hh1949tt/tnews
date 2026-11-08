# 第三方依赖与素材

本仓库的**代码**按 [PolyForm Noncommercial 1.0.0](LICENSE) 许可发布（**禁止商用**，个人与非商业用途自由使用）；下面这些内容各有自己的许可，随仓库分发时需一并保留声明。相关合规分析见 [docs/legal.md](docs/legal.md)。

## 运行依赖

| 依赖 | 版本 | 许可 | 用途 |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | 0.115.6 | MIT | Web 框架 |
| [uvicorn](https://github.com/encode/uvicorn) | 0.34.0 | BSD-3-Clause | ASGI 服务器 |
| [feedparser](https://github.com/kurtmckee/feedparser) | 6.0.11 | BSD-2-Clause | RSS / Atom 解析 |
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | HTTP 客户端 |
| [PyYAML](https://github.com/yaml/pyyaml) | 6.0.2 | MIT | 配置解析 |
| [APScheduler](https://github.com/agronholm/apscheduler) | 3.11.0 | MIT | 定时生成 |
| [python-dateutil](https://github.com/dateutil/dateutil) | 2.9.0 | Apache-2.0 / BSD-3-Clause（双许可） | 时间计算 |
| [edge-tts](https://github.com/rany2/edge-tts) | ≥7.3.1 | **LGPL-3.0**（其中 `srt_composer.py` 为 MIT） | 语音合成 |
| [zhconv](https://github.com/gumblex/zhconv) | 1.4.3 | MIT（见下方说明） | 繁简转换 |

> **关于 edge-tts**：LGPL-3.0 允许在采用其它许可（含本项目这种非商业许可）的项目中作为独立依赖使用，条件是使用者可以自行替换这个库（本项目直接 `pip install`，满足这一条）。修改 `edge_tts` 源码再分发时，修改部分需按 LGPL 公开。
>
> **关于 zhconv**：发布包（wheel）里的 `LICENSE` 文件是 MIT，但包元数据的 classifier 写的是 `GPLv2+`，其转换表部分源自 MediaWiki 的中文转换表。这种「声明不一致」属于上游历史遗留，实际项目普遍按 MIT 对待；如果所在组织对许可证审计严格，可以换成 Apache-2.0 的 [OpenCC](https://github.com/BYVoid/OpenCC)（`opencc-python-reimplemented`）。

## 素材

| 素材 | 位置 | 许可 | 来源 |
|---|---|---|---|
| 背景音乐 6 首 | `web/bgm/*.mp3` | **CC BY 4.0** | Kevin MacLeod（[incompetech.com](https://incompetech.com)） |
| 图标 | `web/icon.svg` | 随本项目 | 自制 SVG，无第三方图形 |
| 内置背景音乐生成脚本 | `tools/make_bgm.py` | 随本项目 | 本地正弦/包络合成，输出可自由分发 |

> **背景音乐注意事项**：CC BY 4.0 要求署名包含「作者 + 作品名 + 来源 + 许可」四项。当前这一组 mp3 的 ID3 里只有专辑名 `Royalty Free` 和流派（Electronic / Contemporary / Orchestra / Country），**没有曲名和作者**，README 里写的 "Kevin MacLeod" 来自早期记录、无法从文件本身核实。发布前请二选一：
>
> 1. 按当初的下载来源补齐每首曲子的曲名与页面地址（把上表「来源」列换成具体链接）；
> 2. 或者换成自制音乐：`python tools/make_bgm.py` 生成 `bgm1~4.mp3`，改 `config.yaml` 里的背景音乐列表即可，彻底没有第三方权利问题。
>
> 找不到出处时的保守做法：不要声称是 CC BY 作品，直接换掉。

## 数据与服务

| 内容 | 许可 / 条款 |
|---|---|
| 新闻内容 | 版权归各原媒体所有，本项目只做摘要转述，不主张任何权利；音频仅供个人收听 |
| 天气数据 | [Open-Meteo](https://open-meteo.com)，CC BY 4.0，需署名 |
| 语音合成服务 | 微软 Edge「大声朗读」在线接口，受微软服务条款约束，非官方 API |
| 字体 | 不加载外部字体，使用系统字体 |

新闻源列表（`config.yaml` 的 `programs.*.sources`）只是 URL 引用，不包含任何媒体稿件正文；抓取频率、单源条数上限由程序控制，遵守各站 RSS 的用途约定。
