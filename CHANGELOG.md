# Changelog

本项目版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，更新记录按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式维护。

## [1.0.1] - 2026-11-02

### 修复

- 打开首页超过 6 小时的自动补录（`/api/ensure`）生成时不带账号保存的「源占比」，补出来的期不按占比分配——点 ⚡ 快速生成听到的最新一期正是这期，表现为「占比失效」。现在补录、自动更新、定点定时都会取账号云同步的最新占比
- 自动更新设置里未单独存占比时，回退使用账号最新保存的占比（与定点定时行为一致）
- 请求带了占比但没有任何源命中时，服务端记录 warning，便于发现源地址不匹配类问题

### 变更

- Home Assistant / Node-RED flow 文件更名为 `extras/ha-nodered-tnews-flow.json`；flow 新增暂停键（`input_button.tnews_stop`）、播放方式（`media_type`）与播放直链前缀（`play_prefix`）配置项

## [1.0.0] - 2026-11-01

首个公开版本。

- 多新闻源聚合，按分类生成每日语音简报（大模型摘要 + edge-tts 合成），支持定时与手动生成
- 移动端优先的收听页：连续播放、倍速、定时关闭、跨日连播、白天/黑夜主题（跟随系统）
- 多用户账号体系与权限区分；速听分享、播客订阅源（`/feed.xml`）、外部调用接口
- 首页访客可配置（可指向任意账号的公开节目）、全局管理与版本更新提示
- Docker / Docker Compose、裸机、群晖与飞牛 fnOS 部署，Home Assistant 与 Node-RED 集成
