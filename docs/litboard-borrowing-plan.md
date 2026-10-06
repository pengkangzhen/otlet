# LitBoard 深度调研与借鉴方案

> 调研日期：2026-10-06。对象：github.com/L1nze/LitBoard（浅克隆于 /tmp/litboard，
> 引用行号基于当日 main 分支；克隆清理后按 `仓库路径:行号` 回查）。
> 方法：4 个只读子代理分别调研 AI 助手 / 导入管线 / 引用图谱 / 架构数据模型，
> 主会话对照 otlet 源码（约 4100 行）逐项映射。

---

## 一、LitBoard 是什么水平

Electron + Node 内置 `node:sqlite`，零运行时依赖，Windows x64 单机应用。工程纪律
极强：千项 node:test、fail-closed 数据库策略、事务式备份恢复、注释里写满实测数字
与事实边界（如"S2 公开 API 没有文本→向量端点，不冒充语义检索"）。其最核心的设计
哲学是**把"诚实"做成系统契约而非提示词愿望**——证据三档判定、阅读覆盖范围、召回
路线成败全部由可测试的代码规则产出结构化数据，模型只能转述、不能美化。

四个子系统的关键发现（细节见第三节各项内的参考位置）：

| 子系统 | 核心做法 |
|--------|----------|
| AI 助手 | core/loop/proto 三层纯函数 + 工具循环（max 12 步、卡死检测、单工具输出截断）；三协议（OpenAI chat / Anthropic messages / Responses）内部统一 OpenAI 形态、出口转换 |
| 证据核查 | 词面三档判定（supports/partial/none）完全由代码规则产出：否定极性词、方向词、标题命中降级、跨语言双变体 |
| 导入管线 | SHA-256 文件指纹 + 规范化 DOI 双精确键 O(n) 查重；字段级"补缺不覆盖"；3 并发游标式 worker + 内容签名差量落库 |
| 引用图谱 | 引用边存 `refs_json` 列不建边表；BFS 扩展 + 复合重要性截取；Rust(napi AsyncTask) 只做加速件、纯 JS 回退 |

---

## 二、otlet 差距一览

| # | otlet 现状 | LitBoard 对应实现 |
|---|----------------|-------------------|
| 1 | 搜索 = `LIKE` on title/abstract/venue（database.py:891） | FTS5 trigram 逐页全文索引（含笔记/批注） |
| 2 | ChatAgent = 整篇 PDF 前 12000 字符塞 system prompt 单轮调用（agents/chat.py:107） | 工具循环：`read_pdf_pages` 按页读取 + coverageNote 声明实际读过什么 |
| 3 | 无声明核查 | litsearch.js 纯函数三档判定 + `find_literature` 工具 |
| 4 | 查重 = DOI 精确 + 模糊标题全表扫（services/importers.py:22），无文件指纹 | 指纹 + DOI 双精确键 O(n) 索引 |
| 5 | 仅 Semantic Scholar，无限流/多源合并 | OpenAlex/S2/Scopus 多源 + 每主机节流 + 三键合并 |
| 6 | 知识图 = 标签/作者共现（database.py:681），无引用关系 | 引文网络 BFS + 社区着色 + PageRank |
| 7 | Zotero 迁移只导条目/标签/PDF（storage/zotero_import.py），重复即跳过 | 条目/分类/附件/笔记/批注全量 + 幂等补缺 |
| 8 | messages 表 `CHECK(role IN ('user','assistant'))`（database.py:92） | 会话含 tool 消息、步骤、结束原因、事件点落盘 |

---

## 三、借鉴方案（按优先级）

### P0-1 PDF 逐页全文索引 + FTS5 trigram

**为什么**：全文检索是文献管理器的地基，且是后面 P0-2（按页喂给模型）和 P0-3
（本地证据召回）的前置。otlet 用户有中文文献，SQLite FTS5 默认 unicode61
分词器把中文整段当单 token，无法子串检索；**trigram 分词器**（SQLite ≥ 3.34）
是子串等价语义且天然支持中文，LitBoard 选它正是这个理由（electron/db.js:132）。

**怎么做**（落在 `storage/database.py` + `storage/pdf_store.py`）：

```sql
CREATE TABLE pdf_text (
    paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    fingerprint TEXT,          -- PDF 内容指纹，判 stale 用
    page_count INTEGER,
    pages BLOB,                -- zlib 压缩的 ['第1页文本', ...] JSON
    updated_at TEXT
);
CREATE VIRTUAL TABLE pdf_fts USING fts5(
    paper_id UNINDEXED, page UNINDEXED, text, tokenize='trigram'
);
```

- 建索引：导入后后台线程逐篇提取（LitBoard：3 并发、单篇失败不阻塞、按指纹判
  stale 只 reindex 变化条目，js/pdfsearch.js:115-170）。
- 查询：≥3 字符走 FTS MATCH 返回 (paper_id, page)；<3 字符回退解压全扫
  （LitBoard db.js:1184-1258 有 has_cjk 优化，otlet 规模先不做）。
- zlib level 1 压缩整篇页数组（LitBoard 实测体积约减半，db.js:247）。
- 兼容性：启动时探测 `fts5('trigram')`，不可用则降级 unicode61 + 中文按需回退。

### P0-2 ChatAgent 升级为工具循环 + "诚实覆盖声明"

**为什么**：现状"截断 12000 字符"对长论文等于蒙眼作答，且无法回答"模型到底读了
哪几页"。LitBoard 的解法是模型按需翻页、每次工具返回自带覆盖声明。

**怎么做**（`agents/` 拆分，参照其分层）：

```
agents/
├── core.py      # 纯函数：消息落账、工具调用解析、窗口裁剪、serialize
├── loop.py      # run_turn 循环：chat → 工具执行前落盘 → 执行 → 回填 → 续跑
├── tools.py     # 工具注册表（OpenAI function-calling schema）
└── chat.py      # 兼容入口，内部走 loop
```

核心护栏（LitBoard js/agentcore.js:18-19, 216-225；agentloop.js:132-291）：

- 单轮 max_steps=12；最近 4 次工具调用里同签名 ≥2 次即判 stuck 停止；
- 单工具输出截断 12000 字符；
- **执行工具前先 await 持久化**（崩溃后不出现"工具执行了但没记录"）；
- 取消的工具体面回填："用户取消了操作"作为 tool result，tool_calls/tool 配对
  永不破坏。

首批工具：

1. `read_pdf_pages(paper_id, from_page, to_page, from_char?)`
   单次 ≤8 页、总预算 10000 字符；返回每页 `{page, text, char_offset, char_total}`
   + `coverage_note`："本次读取 X–Y 页共 Z 页，未读完，用 from=N 续读"
   （agenttools.js:592-680）。超长页用"同页码 + from_char"续读而非重读整页。
2. `search_library(query)` → 走 P0-1 的 FTS。
3. `find_literature(claims, claims_en)` → 见 P0-3。

系统提示写入诚实规则（agentui.js:628-631 原文精神）：

> 回答 PDF 相关问题时注明实际读过的页码范围；未读全篇不得宣称已通读全文；
> 把 evidence.verdict=partial/none 的条目说成"有文献支持"属于编造依据，禁止。

**数据模型**：`messages` 表 CHECK 放宽为 `('user','assistant','tool')`，加
`tool_call_id`、`tool_name` 列；`conversations` 加 `end_reason`、`steps`。

### P0-3 声明核查（claim checking）——最值得整体移植的模块

**为什么**：这是 LitBoard 与"AI 总结玩具"的本质区别，对写文献综述/引言时
"有没有文献支持 X"的场景价值极高；且它核心是**可测试的纯代码规则**，与语言栈
无关，Python 移植成本低（litsearch.js 约 430 行）。

**移植要点**（js/litsearch.js）：

1. **论点拆分** `splitClaims`（:111-137）：按句末标点切句、滤 <8 字符、词重叠
   ≥70% 的句子合并取更长者；模型若自己拆好 claims 则优先（上限 12 条）。
2. **词项提取**（:72-97）：拉丁词 ≥3 字符 + CJK 单字 + 相邻 bigram——中文论点
   靠 bigram 才能匹配英文摘要不行，所以工具参数设计里**强烈建议模型同时给英文
   版论点**，双变体各查一遍取最好判定（:244-266）。
3. **证据句判定** `assessEvidence`（:158-237）：摘要/标题切句（≥12 字符），
   论点词覆盖率 ≥0.34 才算证据句。
4. **三档判定全靠代码规则**（:218-235），模型无权决定 verdict：
   - 标题命中 → 一律 `partial`（标题永远不可能承载证据）；
   - 极性相反（否定词表 :28 / 方向词表 :34-35，如"提高 vs 降低"）→ `partial`；
   - 论点有方向性结论而证据句无同向方向词 → `partial`；
   - 否则 `supports`；无命中句 → `none`，附跨语言提示："这是未判定，不等于不存在"。
5. **多源合并** `mergeCandidates`（:268-344）：DOI → id → 规范化标题三键去重、
   字段级择优（有摘要胜无、被引取最大、记 sources）。
6. **召回路由**（electron/ipc/research.js:157-266）：本地 FTS 关键词 + OpenAlex
   semantic + OpenAlex keyword + S2，每路独立成败、失败原因逐路回报给模型；
   远端只覆盖前 4 条论点并如实注明。
7. **结果不隐藏 none**（:362-431）："模型需要看到'召回到但不支撑'与'根本没
   召回'的区别"。

### P0-4 查重升级：文件指纹 + 规范化 DOI 双精确键

**为什么**：otlet 的模糊标题匹配是全表扫 + 纯等值比较（`_norm` 后 ==，
database.py:440-461），既慢又会漏（同一 PDF 不同元数据来源标题写法不同时靠不上）。
LitBoard 证明**精确键匹配足够，不需要模糊阈值**。

**怎么做**（`services/importers.py` + `database.py`）：

1. papers 加列 `pdf_fingerprint TEXT`；导入时**一次读文件字节，同时算 SHA-256
   和喂解析器**（js/pdfimport.js:829-841，避免二次读盘）。
2. DOI 归一化补强（js/dedupe.js:17-19）：trim + 剥
   `https?://(dx.)?doi.org/` 前缀 + 转小写——otlet 现在只做了 LOWER。
3. 建索引 `create_match_index(papers) → {by_fingerprint, by_doi}`，整批导入
   建一次、每条 O(1) 查 + 增量登记（js/dedupe.js:212-243）。
4. 查重顺序：指纹 → DOI →（都无）新增；模糊标题匹配降级为**手动查重分组建议**
   （LitBoard 的 `isTitleCandidate` 也没接入自动流，仅导出供 UI 用）。
5. 墓碑（is_deleted=1）不进索引，避免复活已删条目。

### P1-5 OpenAlex 客户端 + 每主机节流 + 多源合并

**为什么**：otlet 只有 S2 一源，无节流；S2 无向量端点、OpenAlex
`search.semantic` 才是真正的自然语言语义检索。OpenAlex 亦是 P1-6 引文数据
与 P1-8 元数据补全的共用底座。

**要点**（electron/research-net.js）：

- 每主机节流队列：并发上限 + 最小发起间隔（"防触发"优先于"退避"），仅
  429/5xx 指数退避且尊重 Retry-After（:49-165）。配额：OpenAlex 3 并发/200ms、
  S2 1/1200ms（otlet 现在裸 httpx，易被封）。
- polite pool：`mailto` 参数（:356-365），免费额度显著更稳。
- 批量：按 OpenAlex ID 100/批、按 DOI 反查 25/批分块并发（:455-501）。
- 归一化：倒排索引摘要重组（js/research.js:41-59）；tldr 放 snippet 字段
  **不冒充 abstract**（:127-133）。
- 多源合并复用 P0-3 的三键合并器。

### P1-6 引用图谱（引文网络，非标签共现）

**为什么**：otlet 已有知识图（标签/作者共现），但研究者做 survey 时真正
需要的是**引文网络**：从几篇种子出发看领域结构、找奠基论文。LitBoard 这套
"BFS 扩展 + 复合重要性截取 + 社区着色"可以直接平移。

**怎么做**：

1. 数据：papers 加列 `refs_json TEXT DEFAULT '[]'` 存 OpenAlex
   `referenced_works` ID 数组，**不建边表**——万级文献全库取两列组装邻接表
   毫秒级（research-db.js:526-535），与单文件 SQLite 哲学自洽。库内 DOI ↔
   OpenAlex ID 映射存 `openalex_id` 列。
2. 构建 `build_graph(seeds, depth≤3, max_nodes≤500)`（js/graphgen.js:668-787）：
   BFS 逐深度按"邻居被引频次"排序、按剩余预算截取，缺的节点联网补库（**检索/
   建图即入库**，持久缓存就是 SQLite 本身）；超上限时按
   `0.50×被引 + 0.25×PageRank + 0.15×集合内入度 + 0.10×奠基年份` 择优
   （:243-293），截断数如实写入返回的 meta。
3. 算法：`networkx`（`uv add networkx`）——pagerank + louvain
   （`nx.community.louvain_communities`）+ spring_layout，seed 固定保证确定性
   （LitBoard 用 seed=42 mulberry32 同图同坐标，graphgen.js:468-478，预计算
   坐标可随图缓存）。500 节点规模纯 Python 秒级，**不需要 Rust**。
4. 后台计算放 `threading`（Database 已有 RLock 模式）或 FastAPI/FastAPI 风格
   `run_in_executor`，不阻塞 UI 请求。
5. 前端：static/index.html 引入 vis-network 单文件（vendored），视觉编码全部
   可解释——**颜色=年份色阶、大小=log1p(被引)、线深=局部引用强度**
   （graphgen.js:827-889）；标签配额 `max(18, 2.5√n)`；物理稳定即冻结
   （graphview.js:239-264）。

### P1-7 Zotero 迁移补齐：幂等补缺 + 全实体

**为什么**：otlet 已做对两件事（只读临时副本、跳过 deletedItems），但只导
条目/标签/PDF，且重复=整体跳过——用户在 otlet 里补过的元数据永远等不到
Zotero 侧的更新。

**要点**（electron/integrations.js:2464-2805；js/app/zotero-wizard.js）：

1. 加导 **collections（映射为 is_top 主题标签）、笔记（itemNotes HTML→Markdown）、
   批注（Zotero 7 itemAnnotations 表，防御式列探测）**。
2. **幂等键**：papers 加列 `zotero_key TEXT`（LitBoard 用 sourceLibraryId+
   zoteroKey 防多库串号；otlet 单库场景 zotero_key 足够）。重复运行时：
   元数据只补空字段；用户笔记/阅读状态/标签一律本地赢；双方非空且不一致进
   **只读 conflicts 报告**供人工核对，不弹窗打断。
3. 向导四步：选来源 → 扫描预览（不复制任何文件）→ 导入（进度事件）→ 核对
   报告（缺失/失败/差异可导出 JSON）。

### P1-8 元数据补全降级链

**要点**（js/enrich.js）：

- 链：OpenAlex → S2 → Crossref；PubMed(PMID)/OpenLibrary(ISBN) 特例；中文期刊
  DOI 不在 Crossref 时用 OpenAlex sources 按 ISSN 反查刊名兜底（:628-647）。
- **匹配校验防张冠李戴**：DOI 命中直接采信；标题检索命中须过
  `norm(标题)相等 或 互为前缀且短/长≥0.9`（:124-131）。
- 429 限流后该源冷却 10 分钟；断点台账（24h）让中断/重启不重查整批。
- 写回 `apply_patch` **只补空字段**，绝不覆盖已有值（citation_count/openalex_id
  例外，取新值）。
- 落到 otlet：`services/importers.py` 旁新建 `services/enrich.py`，CLI 加
  `otlet enrich` 子命令，GUI 加"一键补全"开关（可暂停/中断）。

### P2-9 会话持久化增强

- 流式增量只进内存；落盘只在事件点：轮开始、**执行工具前**、工具结果后、轮收尾
  （agentloop.js:144-287）。
- 崩溃恢复靠 running 标记 → 残留流恢复为"已中断"卡 + 可重试（:75-121）。
- 重试/编辑重发：按轮定位真实用户消息截断重跑，被删历史进 editHistory（最近
  20 份），沿用原轮冻结的上下文（:297-335）。

### P2-10 查询语言 + 保存搜索

LitBoard 的查询语法（js/query.js）值得抄核心子集：`field:value`、
`year>=2020 citations>10`、`has:pdf`、`is:unread|trash`、`missing:doi`、AND/OR/NOT
与括号；**版本化 AST 落库**（saved_searches 表同时存 query 文本与序列化 AST，
解析器升级后旧搜索仍可执行）；`ann(text:"..." color:...)` 的"同一批注内满足"
语义等真有批注功能再上。可视化构建器编译到同一文本语法，两前端一 AST。
otlet 可先做解析器 + `saved_searches` 表，构建器缓行。

### P2-11 多提供商配置

- `Settings` 加 `providers: list[ProviderConfig]`（id/name/base_url/model/
  api_key），扁平字段 `model/api_key/api_base` 降级为"当前生效
  服务商的镜像"，写盘前重算——老消费方零改动（js/agentcfg.js:130-138）。
- ⚠️ 记忆教训：改 Settings 的测试必须 monkeypatch `save`，防真实
  `~/.otlet/config.yaml` 被覆盖。
- embedding 独立配置（embed_base_url/key/model），**半填状态直接报错不静默
  回退**（js/embedcfg.js:63-93——"半填状态下偷偷用别的账号计费比报错更糟"）。

### P2-12 备份快照（轻量版）

不做内容寻址对象仓，取其协议纪律（electron/backup.js）：

- gzip 整库（剥离无关注入）+ 资产清单 manifest → staging → 校验通过才原子
  发布 → 与最近一份逐字节相同则跳过 → 轮换保留 7 份。
- 恢复：校验 → 原库改名 `.corrupt-<token>` 隔离 → 临时位置还原复核 → 原子
  切换，失败全链回滚，**永不删除原文件**。
- CLI：`otlet backup` / `otlet restore`。

### P2-13 agent 写类工具确认门

agent 工具凡写库（collect_papers 之类）执行前弹确认，描述**带来源与目标**
（"来自会话 X，将收入 3 篇到库"）；确认通过后、真正提交前各再核一次取消；
长异步内部每个 await 边界复查 is_cancelled（js/agenttools.js:70-78;
agentui.js:178-217）。otlet 落到 web/api.py：pywebview 的 `evaluate_js`
确认框 + async 工具协议。

---

## 四、工程纪律层面（不改代码也该学的）

1. **纯函数 + 依赖注入分层**：core/loop/proto 零依赖可单测，`create_runner
   ({chat, execute_tool, persist, emit})` 注入一切副作用。otlet 现有
   agents/ 已有雏形，重构成 P0-2 结构时照此办理。
2. **注释写事实边界**：每个外部数据源的能力限制（S2 无向量端点、Scopus 无
   摘要、trigram 的 <3 字符回退）写进注释并如实透传给模型与 UI。
3. **fail-closed**：数据库损坏先 `PRAGMA quick_check` 探针，失败抛错并走备份
   恢复，绝不在损坏文件上建空库（db.js:312-369）。
4. **changelog 即工程日志**：决策理由 + 回归证据数字（"npm test 1062 项通过"）。
5. **迁移前先备份**：每个 schema 迁移版本前 `wal_checkpoint(TRUNCATE)+copy`
   生成不覆盖的 `pre-vN.bak`（db.js:175-182）——otlet 已有 `.pre-v2.bak`
   先例，固化成惯例。
6. **每轮改动的验证证据**：分阶段验证规则在 LitBoard 体现为 perf-baseline
   寄生在冒烟管线上（同轮既出功能断言又出计时）。

## 五、明确不照搬的部分

| 项 | 理由 |
|----|------|
| Rust napi 加速模块 | 500 节点规模 networkx 秒级；Python 侧"不阻塞 UI"用线程池即可。LitBoard 自己也是"Rust 是加速件不是依赖、纯 JS 回退" |
| 零运行时依赖哲学 | Electron 无包管理器约束下的选择；Python/uv 生态正常用依赖 |
| app.js 12000 行巨石 | 其反面教材；otlet 保持模块拆分 |
| 三协议适配（Anthropic/Responses） | otlet 用 litellm，协议转换已由 litellm 承担；只需学"内部统一 OpenAI 消息形态" |
| 内容签名差量落库 | otlet 直接写 SQLite 无内存全量模型，不存在该问题 |
| Windows 专属构建/SmartScreen 流程 | 平台不同 |

## 六、建议落地顺序

| 里程碑 | 内容 | 依赖 |
|--------|------|------|
| M1（地基） | P0-4 指纹查重 → P0-1 FTS 索引（含 messages 表 role 放宽迁移） | 无 |
| M2（AI 升级） | P0-2 工具循环 + read_pdf_pages；P1-5 OpenAlex+节流 | M1 |
| M3（差异化） | P0-3 声明核查 find_literature；P1-8 enrich 链 | M2 |
| M4（图谱与迁移） | P1-6 引文网络；P1-7 Zotero 补齐 | M2 |
| M5（打磨） | P2 按需 | — |

每个里程碑收尾跑全量 pytest + 一条真实论文导入冒烟，changelog 记录证据数字。
