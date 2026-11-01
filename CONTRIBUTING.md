# 参与贡献

欢迎 issue 和 PR。这个项目偏「一个人用着顺手」的自托管工具，所以改动请尽量小、可回退、不引入重依赖。

## 提 issue 前

- 先看 [常见问题](docs/faq.md) 和 [部署](docs/deployment.md)；
- 说明版本（`curl http://你的地址:18282/health`）、部署方式（Docker / 裸机）、相关日志片段（`docker compose logs --tail 100 tnews`）；
- **不要贴真实令牌、密码、内网地址**，用 `xxx` 代替。

## 提 PR

1. 一个 PR 做一件事，说明「为什么」而不只是「改了什么」；
2. Python 3.10+，不新增依赖（确实必要请在 PR 里说明理由）；
3. 前端仍是单文件 `web/index.html`，不引入构建工具；
4. 行为有变化就同步改 `docs/`，并在 `CHANGELOG.md` 的「未发布」里加一行；
5. 提交前自查：

```bash
python -m compileall -q app
python -c "import app.main"
node tools/check_flow.cjs      # 动过 extras/ 或 tools/build_flow_config.py 才需要
```

6. 提交信息用 `feat:` / `fix:` / `docs:` / `refactor:` / `chore:` 前缀。

## 不要提交

`data/`（数据库与音频）、`.env`、任何令牌与个人地址。`.gitignore` 已经挡住了，提交前 `git status` 再看一眼。

## 许可证

提交 PR 即表示同意以 [MIT](LICENSE) 许可发布你的贡献。
