# Agent-Lit 技术设计文档

## 1. 竞品调研

### 1.1 传统文献管理软件

| 软件 | 类型 | 标签支持 | AI 功能 | 缺点 |
|------|------|----------|---------|------|
| **Zotero** | 开源桌面/Web | ✅ 基础标签 | ❌ 无原生 AI | 标签仅平面列表，无层次/关系 |
| **Mendeley** | 免费+商业 | ✅ 基础标签 | ❌ 无 | 已停止更新桌面版 |
| **EndNote** | 商业 | ❌ 文件夹为主 | ❌ 无 | 昂贵、笨重 |
| **Paperpile** | Web 商业 | ✅ 基础标签 | ✅ AI 聊天 | Google Drive 锁定 |
| **Papers (ReadCube)** | 商业 | ✅ 基础标签 | ✅ AI 助手 | 闭源、订阅制 |

### 1.2 AI 辅助研究工具

| 工具 | 功能 | 定位 |
|------|------|------|
| **ChatPDF** | 上传 PDF 对话 | 纯阅读助手，无管理能力 |
| **ScholarAI** | 搜索+总结+引用 | 搜索增强，无本地管理 |
| **Semantic Scholar** | AI 搜索+推荐 | 纯搜索引擎，无本地存储 |
| **GPT-Researcher** | 多源深度调研 | 自动报告生成，非管理工具 |

### 1.3 差异化定位

现有工具的共性不足：
1. **标签系统太弱**：Zotero 等只有平面标签，不支持标签层级和交叉检索
2. **AI 与管理割裂**：要么是管理工具无 AI，要么是 AI 工具无管理
3. **无 Agent 能力**：不能自动分类、自动摘要、自动推荐相关文献
4. **闭源或在线依赖**：多数需要联网、账号、订阅

**Agent-Lit 的差异化**：
- 🏷️ **富标签系统**：层级标签 + 交叉筛选（不只是平面字符串列表）
- 🤖 **内嵌 AI Agent**：聊天对话、自动标签、智能摘要
- 🖥️ **终端 TUI**：研究者友好的 CLI 工具，无需 GUI
- 🔌 **可插拔 LLM**：支持 OpenAI / Anthropic / 本地模型

## 2. 技术选型

### 2.1 核心依赖

| 类别 | 选型 | 理由 |
|------|------|------|
| **TUI 框架** | Textual | Python 最佳 TUI 框架，支持分屏布局、CSS 样式、事件驱动 |
| **PDF 解析** | PyMuPDF | 性能最优（10-20x 快于 pdfplumber），支持文本/元数据/图片提取 |
| **LLM 接入** | litellm | 统一接口支持 OpenAI/Anthropic/本地模型，streaming 开箱即用 |
| **学术搜索** | Semantic Scholar API | 免费、无需 API Key（基础额度），返回结构化学术数据 |
| **数据存储** | SQLite (via sqlite3) | 比 YAML 更适合查询/索引，轻量无需服务，支持标签多对多关系 |
| **数据模型** | Pydantic v2 | 类型安全、验证、序列化 |
| **CLI 框架** | argparse → Textual App | 简单命令用 argparse，交互式用 Textual |

### 2.2 为什么选 Textual

Textual 是一个 Python TUI 框架，可以构建：
- **分屏布局**：左侧文献列表 + 右侧对话面板（类似 VS Code 终端版）
- **CSS 样式**：用 CSS 控制布局和样式
- **事件驱动**：按键、点击、滚动等交互
- **Rich 渲染**：Markdown、表格、语法高亮等
- **运行于终端**：无需浏览器或 GUI 框架

## 3. 架构设计

### 3.1 系统架构

```
┌─────────────────────────────────────────────────┐
│                   Textual TUI                    │
│  ┌──────────────────┬──────────────────────────┐ │
│  │   左侧面板        │      右侧面板             │ │
│  │                  │                          │ │
│  │  [标签筛选栏]     │   [对话区域]              │ │
│  │                  │                          │ │
│  │  [文献列表]       │   [消息气泡]              │ │
│  │   - Paper 1      │    User: 这篇讲了什么?    │ │
│  │   - Paper 2      │    AI: 这篇论文...        │ │
│  │   - Paper 3      │                          │ │
│  │                  │   [输入框]                │ │
│  └──────────────────┴──────────────────────────┘ │
└─────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
┌─────────────┐    ┌──────────────────┐
│  Library    │    │  Chat Agent      │
│  (SQLite)   │    │  (LLM + RAG)     │
│             │    │                  │
│ - papers    │    │ - PDF 提取文本    │
│ - tags      │    │ - 对话历史       │
│ - paper_tag │    │ - 流式响应       │
└─────────────┘    └──────────────────┘
         │                    │
         ▼                    ▼
┌─────────────┐    ┌──────────────────┐
│  Search     │    │  Classify Agent  │
│  Agent      │    │  (自动标签)       │
│  (S2 API)   │    │  (LLM)          │
└─────────────┘    └──────────────────┘
```

### 3.2 数据模型

```sql
-- 文献表
CREATE TABLE papers (
    id          TEXT PRIMARY KEY,  -- UUID
    title       TEXT NOT NULL,
    year        INTEGER,
    venue       TEXT,
    doi         TEXT UNIQUE,
    url         TEXT,
    abstract    TEXT,
    pdf_path    TEXT,
    bibtex_key  TEXT,
    notes       TEXT,
    added_date  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 标签表（支持层级）
CREATE TABLE tags (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent_id   TEXT REFERENCES tags(id),  -- 层级支持
    color       TEXT,                       -- 显示颜色
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 文献-标签 多对多关系
CREATE TABLE paper_tags (
    paper_id    TEXT REFERENCES papers(id),
    tag_id      TEXT REFERENCES tags(id),
    PRIMARY KEY (paper_id, tag_id)
);

-- 对话历史
CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT REFERENCES papers(id),
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id),
    role            TEXT NOT NULL,  -- 'user' | 'assistant'
    content         TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 作者
CREATE TABLE authors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    affiliation TEXT,
    orcid       TEXT
);

CREATE TABLE paper_authors (
    paper_id    TEXT REFERENCES papers(id),
    author_id   TEXT REFERENCES authors(id),
    position    INTEGER,
    PRIMARY KEY (paper_id, author_id)
);
```

### 3.3 模块结构

```
src/agent_lit/
├── __init__.py
├── cli.py                  # CLI 入口（简单命令 + 启动 TUI）
├── config/
│   ├── __init__.py
│   └── settings.py         # 应用配置
├── models/
│   ├── __init__.py
│   ├── paper.py            # Paper Pydantic 模型
│   ├── author.py           # Author 模型
│   └── tag.py              # Tag 模型（新增）
├── storage/
│   ├── __init__.py
│   ├── database.py         # SQLite 数据库管理（新，替代 library.py）
│   └── pdf_store.py        # PDF 文件存储管理（新增）
├── agents/
│   ├── __init__.py
│   ├── base.py             # Agent 基类
│   ├── search.py           # 检索 Agent (Semantic Scholar)
│   ├── classify.py         # 分类/标签 Agent (LLM)
│   └── chat.py             # 对话 Agent (LLM + PDF context)
├── tui/                    # Textual TUI 界面（新增）
│   ├── __init__.py
│   ├── app.py              # 主应用
│   ├── screens/
│   │   ├── __init__.py
│   │   ├── main_screen.py  # 主分屏布局
│   │   └── search_screen.py # 搜索弹窗
│   └── widgets/
│       ├── __init__.py
│       ├── paper_list.py   # 文献列表组件
│       ├── tag_filter.py   # 标签筛选组件
│       ├── chat_panel.py   # 对话面板组件
│       └── paper_detail.py # 文献详情组件
└── llm/                    # LLM 抽象层（新增）
    ├── __init__.py
    └── provider.py         # 统一 LLM 接口 (litellm)
```

### 3.4 核心工作流

#### 添加文献流程
```
用户输入 DOI/标题 → SearchAgent 查询 S2 API → 返回结构化 Paper
→ 用户确认添加 → ClassifyAgent (LLM) 自动生成标签建议
→ 用户确认/修改标签 → 存入 SQLite + 下载 PDF
```

#### 对话流程
```
用户选中文献 → 右侧加载对话面板
→ 用户输入问题 → ChatAgent:
   1. 从 PDF 提取全文（PyMuPDF）
   2. 构造 system prompt（论文内容 + 对话历史）
   3. 调用 LLM 流式生成回答
   4. 渲染 Markdown 到对话区域
```

#### 标签筛选流程
```
左侧标签栏 → 用户选择标签 A → 过滤显示包含标签 A 的文献
→ 用户再选择标签 B（AND 逻辑）→ 显示同时包含 A 和 B 的文献
→ 用户点击文献 → 右侧显示详情/对话
```
