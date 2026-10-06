# Otlet

基于 Agent 的文献管理软件——标签驱动的文献管理 + AI 对话。

> 命名致敬 Paul Otlet（1868–1944，比利时文献学家、信息科学之父）：他在 1930 年代就用卡片分类网络把知识互相链接起来，比万维网早了半个多世纪——这正是本软件想为你做的事。

## 核心特性

- 🏷️ **标签管理** — 基于标签而非文件夹的文献组织方式，支持多标签交叉筛选（AND/OR 逻辑）
- 📄 **PDF 智能导入** — 拖拽 PDF 自动识别元数据（DOI → S2 查询 → 标题搜索），类 Zotero 体验
- 💬 **AI 对话** — 选中论文直接与 AI 对话，快速了解论文内容和方法
- 🔍 **智能搜索** — 对接 Semantic Scholar API，一键搜索导入文献
- 🤖 **自动标签** — 离线关键词提取或 LLM 自动为论文生成标签建议
- 🖥️ **桌面 GUI** — pywebview 原生窗口（无子命令直接启动）
- ⌨️ **完整 CLI** — 导入/检索/标签/笔记/AI 对话/导出全流程命令行操作

## 安装

```bash
uv sync
```

## 快速开始

### CLI 命令

```bash
# 在线搜索文献（Semantic Scholar）
otlet search "transformer attention" --limit 5

# 导入 —— 六种来源统一走 add 命令
otlet add paper.pdf                    # PDF 文件（自动识别元数据 + 去重）
otlet add ~/Downloads/*.pdf            # 多个 PDF
otlet add --folder ~/papers            # 递归导入文件夹下所有 PDF
otlet add --doi "10.1287/opre.2020.1"  # 通过 DOI
otlet add --query "attention is all you need"  # 在线搜索取第一条
otlet add --bibtex library.bib         # BibTeX 文件（Zotero 导出）
otlet add --zotero                     # 本机 Zotero 库（--collections 1,2 选分类）
otlet add --doi "10.1/x" --auto-tag    # 导入时追加 LLM 标签建议

# 文库管理
otlet list                             # 列出全部文献
otlet list -q "supply chain"           # 本地全文过滤
otlet list --tags resilience,location  # 标签过滤（AND；--any 切 OR）
otlet list --json                      # JSON 输出（供脚本使用）
otlet show <paper_id>                  # 详情：元数据 + 摘要 + 笔记
otlet open <paper_id>                  # 系统默认阅读器打开 PDF
otlet rm <paper_id>                    # 移入回收站
otlet trash                            # 回收站：list / restore / purge / empty

# 标签
otlet tag add <paper_id> resilience location   # 打标签（可多个）
otlet tag remove <paper_id> resilience
otlet tags                             # 标签列表（--auto 含自动标签）
otlet tags rename old-name new-name
otlet autotag <paper_id>               # 离线关键词打标（--method llm 用大模型）

# 笔记与 AI 对话
otlet note add <paper_id> "关键基线论文"
otlet note list <paper_id>
otlet chat <paper_id>                  # 交互式对话（流式输出，历史按论文保存）
otlet chat <paper_id> -m "这篇论文的方法是什么？"  # 单次提问

# 导出与配置
otlet export -o library.bib            # 导出 BibTeX（--ids 选部分论文）
otlet settings                         # 查看配置（密钥脱敏）
otlet settings set model glm-4.7   # 修改配置（写入 ~/.otlet/config.yaml）

# 启动桌面 GUI（无子命令时同样默认启动 GUI）
otlet gui
```

### PDF 智能导入

`otlet add <pdf>` 模拟 Zotero 的元数据识别流程：

1. **XMP 元数据** → 检查 PDF 内嵌 DOI（置信度 95%）
2. **文本 DOI** → 正则提取前几页中的 DOI → S2 API 查询（90%）
3. **ArXiv ID** → 提取 ArXiv 标识符 → S2 查询（85%）
4. **XMP 标题** → 从 PDF 元数据提取标题 → 搜索（75%）
5. **标题启发式** → 从首页文本提取候选标题 → 搜索（60%）
6. **文本回退** → 构建基础 Paper 对象（30%）

在终端中可以直接将 PDF 文件**拖拽到命令行**（自动生成路径），然后回车即可导入。

## 配置

配置文件位于 `~/.otlet/config.yaml`，首次运行自动生成默认配置。

```yaml
data_dir: ~/.otlet
model: gpt-4o-mini          # LLM 模型（支持 OpenAI/Anthropic 等）
api_key: null                # LLM API Key（也可通过环境变量设置）
api_base: null               # 自定义 API 地址（用于本地模型）
s2_api_key: null                 # Semantic Scholar API Key（可选）
```

也可通过环境变量配置：
```bash
export OTLET_MODEL="gpt-4o-mini"
export OTLET_API_KEY="sk-..."
export OTLET_API_BASE="https://api.openai.com/v1"
```

## 项目结构

```
otlet/
├── src/otlet/
│   ├── agents/          # Agent 模块
│   │   ├── base.py      # Agent 基类
│   │   ├── search.py    # 检索 Agent (Semantic Scholar)
│   │   ├── classify.py  # 分类/标签 Agent (LLM)
│   │   └── chat.py      # 对话 Agent (LLM + PDF)
│   ├── models/          # 数据模型
│   │   ├── paper.py     # 论文模型
│   │   ├── author.py    # 作者模型
│   │   └── tag.py       # 标签模型
│   ├── services/        # 服务层（GUI 与 CLI 共享）
│   │   └── importers.py # 导入管线：去重 / 自动标签 / BibTeX 解析
│   ├── storage/         # 存储层
│   │   ├── database.py  # SQLite 数据库
│   │   ├── pdf_store.py # PDF 文件管理
│   │   ├── pdf_metadata.py  # PDF 元数据识别
│   │   ├── zotero_import.py # Zotero 数据库读取
│   │   └── bibtex_export.py # BibTeX 导出
│   ├── llm/             # LLM 抽象层
│   │   └── provider.py  # 统一 LLM 接口 (litellm)
│   ├── config/          # 配置
│   │   └── settings.py  # 应用设置
│   ├── web/             # 桌面 GUI (pywebview)
│   │   ├── app.py       # 窗口与原生菜单
│   │   ├── api.py       # JS→Python 桥接 API
│   │   └── static/      # 前端（单文件 HTML）
│   └── cli.py           # 命令行入口
├── tests/               # 测试
└── docs/                # 文档
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 桌面 GUI | pywebview |
| CLI 输出 | rich |
| PDF 解析 | PyMuPDF |
| LLM 接入 | litellm（支持 OpenAI/Anthropic/本地模型） |
| 学术搜索 | Semantic Scholar API |
| 数据存储 | SQLite (WAL) |
| 数据模型 | Pydantic v2 |

## 开发

```bash
uv sync
uv run --no-sync pytest
uv run --no-sync ruff check .
```

## License

MIT
