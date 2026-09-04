# 协作指南

本仓库用于多人协作，请遵守下面的约定，避免互相踩代码、产生冲突噪音。

## 分支模型

- `main`：稳定主干。所有提交都必须通过 Pull Request 合入，不在 main 上直接提交。
- 新功能：从最新 `main` 拉分支 `feat/功能名`（如 `feat/ics-export`）。
- 修 Bug：分支名 `fix/问题描述`（如 `fix/duplicate-extract`）。

```bash
git checkout main
git pull
git checkout -b feat/my-feature
# 开发、本地验证后：
git add .
git commit -m "feat: 支持导出 .ics 日历"
git push -u origin feat/my-feature
```

然后到 GitHub 上发起 Pull Request，至少由另一位成员 review 后再合入。

## 提交信息

建议使用 Conventional Commits 风格，中英文均可：

- `feat: ...` 新功能
- `fix: ...` 修 Bug
- `docs: ...` 文档
- `refactor: ...` 重构
- `chore: ...` 杂项（依赖、配置、CI）

## 本地验证

代码是纯 Python 标准库实现，改完至少做一次语法/冒烟检查：

```bash
python -m py_compile server.py ai_gateway.py extractor.py kinds.py scheduler.py storage.py wechat_bridge.py
python server.py   # 浏览器打开 http://127.0.0.1:8000 手动过一遍
```

## 注意事项

- `data/` 目录被 `.gitignore` 忽略：里面是本地运行数据，包含 **API Key、微信账号状态和个人日程**，任何情况下都不要提交，也不要在 Issue/PR 里贴出来。
- `wechatauto-replica-main/` 是第三方库 `wechatauto-replica`（Apache-2.0）的随仓库副本，用于「微信自动提取」功能；除非修依赖本身的 Bug，否则不要改动它。
- 所有源码和文档统一为 UTF-8 编码；换行由 `.gitattributes` 统一处理，不要在编辑器里手动改行尾格式。
