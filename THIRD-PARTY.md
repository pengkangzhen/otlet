# 第三方依赖与许可证

Otlet 自身以 **AGPL-3.0-or-later** 发布（全文见 [LICENSE](LICENSE)）。
选择 AGPL 的唯一强制原因是核心依赖 **PyMuPDF**（AGPL-3.0 / Artifex 商业双许可）：
分发包含 PyMuPDF 的构建时，组合作品必须以 AGPL 兼容许可证提供源码。

## 运行时依赖

| 依赖 | 许可证 | 用途 |
|------|--------|------|
| pymupdf | AGPL-3.0（或 Artifex 商业许可） | PDF 解析、逐页文本提取 |
| litellm | MIT | 统一 LLM 提供商接口 |
| pydantic | MIT | 数据模型 |
| httpx | BSD-3-Clause | HTTP 客户端（OpenAlex/S2/LLM） |
| pyyaml | MIT | 配置文件 |
| rich | MIT | CLI 输出 |
| pywebview | BSD-3-Clause | 桌面 GUI 窗口 |
| qtpy | MIT | Qt 后端抽象（pywebview Linux/Qt） |
| textual | MIT | 终端 TUI |

## 声明

- PyMuPDF 及其内嵌的 MuPDF 属 Artifex Software，按 AGPL-3.0 使用；若你需要
  闭源集成，请自行向 Artifex 获取商业许可。
- 各依赖的完整许可证文本随其分发包携带（`*.dist-info/LICENSE*`）。
