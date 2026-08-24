# Agent-Lit

基于 Agent 的文献管理软件——标签驱动的文献管理 + AI 对话。

## 核心特性

- 🏷️ **标签管理** — 基于标签而非文件夹的文献组织方式，支持多标签交叉筛选（AND/OR 逻辑）
- 📄 **PDF 智能导入** — 拖拽 PDF 自动识别元数据（DOI → S2 查询 → 标题搜索），类 Zotero 体验
- 💬 **AI 对话** — 选中论文直接与 AI 对话，快速了解论文内容和方法
- 🔍 **智能搜索** — 对接 Semantic Scholar API，一键搜索导入文献
- 🤖 **自动标签** — LLM 自动为论文生成标签建议
- 🖥️ **TUI 界面** — 终端内分屏交互：左侧文献列表 + 右侧对话面板
- ⌨️ **CLI 命令** — 完整的命令行操作支持

## 安装

```bash
uv sync
```

## 快速开始

### CLI 命令

```bash
# 搜索文献（Semantic Scholar）
uv run --no-sync agent-lit search "transformer attention"

# 通过 DOI 导入文献
uv run --no-sync agent-lit import --doi "10.5555/1234567"

# 搜索导入 + 自动标签
uv run --no-sync agent-lit import "attention is all you need" --auto-tag

# 📄 拖拽 PDF 导入（自动识别元数据）
uv run --no-sync agent-lit import-pdf ~/Downloads/paper.pdf

# PDF 导入 + 自动标签
uv run --no-sync agent-lit import-pdf ~/Downloads/paper.pdf --auto-tag

# 列出文库中的文献
uv run --no-sync agent-lit list

# 管理标签
uv run --no-sync agent-lit tags list
uv run --no-sync agent-lit tags add --name "nlp"
uv run --no-sync agent-lit tag add --paper-id abc123 --tag "nlp"

# 启动 TUI 交互界面
uv run --no-sync agent-lit tui
```

### PDF 智能导入

`import-pdf` 命令模拟 Zotero 的元数据识别流程：

1. **XMP 元数据** → 检查 PDF 内嵌 DOI（置信度 95%）
2. **文本 DOI** → 正则提取前几页中的 DOI → S2 API 查询（90%）
3. **ArXiv ID** → 提取 ArXiv 标识符 → S2 查询（85%）
4. **XMP 标题** → 从 PDF 元数据提取标题 → 搜索（75%）
5. **标题启发式** → 从首页文本提取候选标题 → 搜索（60%）
6. **文本回退** → 构建基础 Paper 对象（30%）

在终端中可以直接将 PDF 文件**拖拽到命令行**（自动生成路径），然后回车即可导入。

### TUI 界面

运行 `agent-lit tui` 后进入终端交互界面：

```
┌───────────────────────────┬────────────────────────────┐
│ 🔍 Search / Paste PDF...  │  💬 Chat — Paper Title     │
│───────────────────────────│                            │
│ 🏷️ Tags                   │  You: 这篇论文讲了什么?     │
│ ☐ ml  ☐ nlp  ☐ cv        │  AI: 这篇论文提出了一种...  │
│───────────────────────────│                            │
│ Paper List                │  📂 Import: paper.pdf      │
│ ▶ Attention Is All You... │  ✅ Imported via DOI (90%) │
│   BERT: Pre-training...   │                            │
│   GPT-4 Technical Report  │                            │
│───────────────────────────│                            │
│ Paper: Attention Is All...│  Ask about this paper...   │
│ Authors: Vaswani et al.   │                            │
│ Tags: nlp, transformer    │                            │
└───────────────────────────┴────────────────────────────┘
```

**TUI 快捷键：**
- `s` — 聚焦搜索框
- `Ctrl+P` — 切换到 PDF 导入模式（在搜索框粘贴路径）
- `t` — 切换标签面板
- `r` — 刷新数据
- `q` — 退出

**TUI PDF 导入方式：**
1. 按 `Ctrl+P` 或点击搜索框
2. 将 PDF 文件从 Finder 拖拽到终端（自动粘贴路径）
3. 按回车，自动识别并导入

## 配置

配置文件位于 `~/.agent-lit/config.yaml`，首次运行自动生成默认配置。

```yaml
data_dir: ~/.agent-lit
lit_model: gpt-4o-mini          # LLM 模型（支持 OpenAI/Anthropic 等）
lit_api_key: null                # LLM API Key（也可通过环境变量设置）
lit_api_base: null               # 自定义 API 地址（用于本地模型）
s2_api_key: null                 # Semantic Scholar API Key（可选）
```

也可通过环境变量配置：
```bash
export LIT_MODEL="gpt-4o-mini"
export LIT_API_KEY="sk-..."
export LIT_API_BASE="https://api.openai.com/v1"
```

## 项目结构

```
agent-lit/
├── src/agent_lit/
│   ├── agents/          # Agent 模块
│   │   ├── base.py      # Agent 基类
│   │   ├── search.py    # 检索 Agent (Semantic Scholar)
│   │   ├── classify.py  # 分类/标签 Agent (LLM)
│   │   └── chat.py      # 对话 Agent (LLM + PDF)
│   ├── models/          # 数据模型
│   │   ├── paper.py     # 论文模型
│   │   ├── author.py    # 作者模型
│   │   └── tag.py       # 标签模型
│   ├── storage/         # 存储层
│   │   ├── database.py  # SQLite 数据库
│   │   ├── pdf_store.py # PDF 文件管理
│   │   └── pdf_metadata.py  # PDF 元数据识别
│   ├── llm/             # LLM 抽象层
│   │   └── provider.py  # 统一 LLM 接口 (litellm)
│   ├── tui/             # Textual TUI
│   │   ├── app.py       # 主应用
│   │   ├── screens/     # 界面
│   │   └── widgets/     # 组件
│   ├── config/          # 配置
│   │   └── settings.py  # 应用设置
│   └── cli.py           # 命令行入口
├── tests/               # 测试
└── docs/                # 文档
```

## 技术栈

| 组件 | 技术 |
|------|------|
| TUI 框架 | Textual |
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
