# 安全说明

## 设计边界

TNews 是**局域网自用的单进程服务**，安全模型很朴素：

- 账号密码用 PBKDF2-SHA256（12 万次迭代 + 随机盐）哈希存储；
- 登录后下发随机令牌，存 SQLite（明文），客户端放在 `localStorage`；
- 分享 / 播客 / 外部调用各有独立令牌，可随时吊销；
- **没有 HTTPS、没有失败次数限制、没有 CSRF token、没有请求限流。**

因此：

- **不要把 18282 端口直接映射到公网。**
- 必须外网访问时，请走反向代理 + HTTPS，并额外加一层访问控制（Basic Auth / IP 白名单 / 仅 Tailscale 网段）。
- 首次启动后立刻修改管理员密码（初始密码随机生成，见部署文档）。
- 分享链接等同于「只读钥匙」，发给谁就等于允许谁听该账号的节目，注意范围。

## 已知限制

| 项 | 说明 |
|---|---|
| 令牌存明文 | 数据库被拿走即等于令牌泄露。防护靠 `data/` 目录权限与备份加密 |
| 无登录限速 | 弱密码可被暴力尝试。请用强密码，且不要暴露到公网 |
| `/api/latest?user=&id=` | 账号名 + 6 位 ID 的组合相当于半公开凭据，仅适合内网；对外请用 `ext` / `token` |
| 音频直链 | `/audio/<文件名>` 不校验身份（文件名含时间戳，不易猜），被转发出去即可被收听 |

## 报告漏洞

请**不要**在公开 issue 里贴利用细节。用 GitHub 的 [Security Advisories](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities)（仓库 → Security → Report a vulnerability）私下报告，或在 issue 里只描述大致范围并留下联系方式。

我会在确认后尽快修复，并在 CHANGELOG 里致谢（如果你愿意署名）。
