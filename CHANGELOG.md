# Changelog

本项目版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，更新记录按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式维护。

## [1.0.0] - 2026-11-01

首个公开版本。

- 多新闻源聚合，按分类生成每日语音简报（大模型摘要 + edge-tts 合成），支持定时与手动生成
- 移动端优先的收听页：连续播放、倍速、定时关闭、跨日连播、白天/黑夜主题（跟随系统）
- 多用户账号体系与权限区分；速听分享、播客订阅源（`/feed.xml`）、外部调用接口
- 首页访客可配置（可指向任意账号的公开节目）、全局管理与版本更新提示
- Docker / Docker Compose、裸机、群晖与飞牛 fnOS 部署，Home Assistant 与 Node-RED 集成

