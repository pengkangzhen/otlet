# Changelog

工程日志体例：每个版本的条目记**决策理由与验证证据**，不只是清单。

## 0.1.0-beta.1 (2026-10-06)

首个公开 beta。核心定位：**标签驱动的本地文献管理 + 代码规则化的 AI 声明核查**。

### 核心能力
- **导入**：PDF 六级元数据识别（XMP DOI → 文本 DOI → arXiv → XMP 标题 →
  标题启发式 → 文本回退）；SHA-256 文件指纹 + 规范化 DOI 双精确键查重
  （O(n) 索引，重复导入自动把 PDF 附到既有条目）；BibTeX / DOI / 在线检索 /
  文件夹批量 / Zotero（只读快照，不动原库）；OpenAlex → S2 元数据补全链
  （只补空字段，标题 0.9 前缀校验防错配）
- **检索**：FTS5 trigram 逐页全文索引——中文/英文子串等价检索；<3 字符
  自动回退扫描；GUI 搜索框显示"全文命中"区块（论文+页码+片段）
- **AI 工具循环**（ChatAgent）：模型亲自调用 `read_pdf_pages`（≤8 页/次、
  超长页 from_char 续读、coverage_note 声明实际读了哪些页）、
  `search_library`、`find_literature`；护栏：max 12 步、卡死检测
  （含乒乓循环）、单工具输出 12k 截断、**执行工具前先落盘**；
  会话持久化含完整工具流量，重开可回放
- **声明核查**（招牌）：`find_literature` / `otlet verify` 对
  "有没有文献支持 X"给出 supports / partial / none 三档判定——判定全部由
  可测试代码规则计算（否定极性、方向词、标题降级、跨语言双变体），
  模型只转述；not_found 明示为"未判定"而非"证伪"；远端召回只覆盖
  前 4 条论点并如实注明
- **三前端**：pywebview 桌面 GUI（macOS/Windows）、rich CLI、
  textual TUI；聊天三端实时流式并显示工具活动
- **生产加固**：`otlet backup/restore`（校验后发布、7 份轮换、恢复
  永不删原文件）；损库 fail-closed（quick_check 探针 + 恢复指引）；
  坏 PDF 不炸批量导入；滚动日志 + `otlet log`
- **隐私**：密钥进 OS keychain（yaml 只存占位符）；数据全部本地
  （~/.otlet/）；备份刻意排除 config

### 许可证
AGPL-3.0-or-later（因 PyMuPDF；详见 THIRD-PARTY.md）。

### 验证证据
- 194 项 pytest 全绿；ruff 无告警
- 真实库副本冒烟：253 篇迁移、备份 340KB → 恢复 → 校验一致；
  OpenAlex 真实检索命中"冗余→韧性"证据链与中文声明英文变体
