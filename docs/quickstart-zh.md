# Otlet 快速上手（研究者视角）

5 分钟从零到日常工作流。所有数据存在本机 `~/.otlet/`（SQLite + PDF 副本），
不注册、不联网也能用（AI 功能除外）。

## 1. 装好后先做两件事

```bash
otlet backup        # 养成习惯：重要操作前备份（保留最近 7 份）
otlet settings      # 看一眼配置；需要 AI 时：
otlet settings set api_key sk-...        # 密钥进 OS 钥匙串，配置文件只留占位
otlet settings set model gpt-4o-mini     # 或 api_base 指向本地/自建端点
```

不配密钥也能用：导入、标签、全文检索、`otlet verify`（走免费的
OpenAlex）都不需要 LLM。

## 2. 把文献弄进来

```bash
otlet add ~/Downloads/paper.pdf      # 拖拽 PDF 到终端也行
otlet add --folder ~/papers          # 整个文件夹（坏文件自动跳过）
otlet add --bibtex library.bib       # BibTeX（Zotero 导出）
otlet add --zotero                   # 直读本机 Zotero 库（只读，不改原库）
otlet index                          # 为存量 PDF 建全文索引（一次即可）
```

同一 PDF 重复导入会被文件指纹自动拦截；同一论文不同版本靠 DOI 认出。

## 3. 日常三件事

**找**：GUI 搜索框（元数据 + 全文命中同屏）或 `otlet grep "关键词"`
（哪篇论文第几页 + 上下文，中文直接搜）。

**问**：`otlet chat <paper_id>` 或 GUI 右侧对话——AI 自己翻页读原文，
回答会注明"实际读了第 X–Y 页"；终端里能看到 `→ read_pdf_pages(第3–5页)…`
这样的活动行。

**核**：写综述时最有用的一招——

```bash
otlet verify "冗余库存能提高供应链韧性" \
            --en "Redundant inventory improves supply chain resilience"
```

返回每条声明的三档判定：
- **supports**：找到覆盖关键词的证据句，且极性/方向一致；
- **partial**：只是主题相关（仅标题命中 / 有否定词冲突 / 方向相反）；
- **none**：未找到证据——**是"未判定"，不是"已证伪"**，换英文措辞可再试。

判定由代码规则计算，不经 LLM，可复现。

## 4. 数据安全

```bash
otlet backup                              # 库 + PDF → 校验后的 zip
otlet restore ~/.otlet/backups/xxx.zip    # 恢复（现有库改名让位，永不删除）
otlet log                                 # 报障时把尾部贴进 issue
```

## 5. 桌面应用与终端界面

```bash
otlet          # 桌面 GUI（无子命令即启动）
otlet tui      # 终端界面：过滤 /全文 :v核查 :c聊天 :n笔记 :e补全
```
