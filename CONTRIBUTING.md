# 贡献指南

感谢关注 Otlet！这是一个 AGPL-3.0 开源项目（许可证选择的原因见
[THIRD-PARTY.md](THIRD-PARTY.md)——核心依赖 PyMuPDF 是 AGPL）。

## 开发环境

```bash
git clone <repo>
cd otlet
uv sync                 # Python ≥3.11，包管理只用 uv
uv run --no-sync pytest # 全量测试
uv run --no-sync ruff check src/ tests/
```

## 工作约定

- **包管理只用 uv**（`uv add` / `uv sync` / `uv run`），不使用 pip /
  requirements.txt；
- **提交前**：`pytest` 全绿 + `ruff check` 干净；提交信息遵循
  Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` …）；
- **新功能必须带测试**：纯函数逻辑优先（参考 `agents/claims.py` 及其
  测试——判定规则全部可单测，不依赖网络或模型）；
- **网络相关代码要可离线测试**：用 `httpx.MockTransport` 注入
  （参考 `tests/agents/test_claims.py`）、假 LLM/假客户端注入
  （参考 `tests/agents/test_loop.py`）；
- **涉及 schema 变更**：走 `Database` 的迁移机制，迁移前自动写
  `.pre-vN.bak` 备份；损坏数据库必须 fail-closed，绝不在坏文件上跑迁移；
- **诚实性契约**：AI 相关功能里，verdict / 覆盖范围 / 路由成败等
  "事实类"输出必须由代码计算、结构化返回，不得交给模型自由发挥
  （背景见 `docs/litboard-borrowing-plan.md`）。

## 报告问题

请附上 `otlet log` 的输出尾部、操作步骤与（如相关）`otlet backup`
备份文件的验证结果。模板见 `.github/ISSUE_TEMPLATE/`。
