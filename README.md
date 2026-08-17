# 📄 PaperOps · 证据驱动科研写作工作台

**面向研究生和科研团队的科研写作 Copilot** —— 从研究问题、方法、实验素材和参考文献出发，逐步生成论文大纲、证据地图和可编辑章节草稿，再完成引用核验、格式修复和文档导出。

核心价值不是替用户凭空编造整篇论文，而是把“我有实验、有论文、有想法，但不知道怎么组织成论文”变成可执行的分章节流程。用户负责研究事实、数据和最终判断，系统负责快速搭建论文框架、建立引用索引、生成章节草稿，并完成一键排版和交付。

系统不要求用户从一句话直接生成几十页论文，而是让用户在每个章节节点确认方向、补充素材和接受修改，避免把 Agent 变成无法校验的黑盒写作器。

项目内部使用轻量级 **ReAct + Coordinator-Worker + FSM** 组合：LLM 负责意图理解、内容判断和修改建议，确定性的 Python 工作流负责工具执行、状态持久化、文件隔离和版本提交。系统不依赖 LangChain，核心依赖为 openai、python-docx、PyMuPDF 和 numpy。

## 🎯 产品闭环

```text
研究问题 + 方法/实验素材 + 参考文献 + 目标规范
        ↓
研究素材整理 → 论文大纲 → 证据地图 → 分章节草稿
        ↓
用户确认 → 引用核验 → 编辑修订 → 导出文档 + 历史版本
```

Web 工作台入口为 `/`，页面采用“项目列表 + 结构化输入 + 模块进度 + 任务日志”的布局。当前第一版使用服务器可访问的文件路径，后续可接入浏览器上传和对象存储。

## 👤 典型使用场景

用户正在完成一个实验，手上已经有：

- 实验结果、数据表和方法笔记；
- 一批已经阅读或准备引用的论文；
- 对论文主题和创新点的初步想法。

但用户不想从空白 Word 文档开始逐节搭建论文。系统将这些素材组织成论文骨架，并按章节生成可编辑草稿：

```text
研究问题 + 方法笔记 + 实验结果 + 参考论文
        ↓
论文大纲 + 章节写作目标 + 引用证据地图
        ↓
引言 / 相关工作 / 方法等章节草稿
        ↓
引用核验 + 格式修复 + Word / PDF 交付
```

当前产品优先支持“快速搭建和组织”，不承诺替用户编造实验数据、研究结论或完整论文事实。

当前第一版的文档交付以 Word 工具链和版本回滚为主；PDF 专用导出、浏览器文件上传和真正的章节级编辑器属于后续适配，不把它们描述成已经完成的能力。

## ✨ 功能

### 文档处理工具

| 工具 | 功能描述 |
|------|---------|
| `inspect_document_format` | 📐 **格式检查**（样式/字体/缩进/行距/对齐），内置学术规范自动诊断 |
| `format_references` | 参考文献格式修复（字体统一、Sentence Case、期刊名斜体） |
| `create_reference_crossrefs` | 文献交叉引用生成（[1][2] → 可跳转域代码） |
| `create_figure_crossrefs` | 图注交叉引用生成（图X.Y → 可跳转域代码） |
| `convert_handwritten_captions` | 手写图注转 Word 题注（支持自动编号） |
| `check_acronym_definitions` | 缩写定义检测（检测 MIMO 等缩写是否有全称） |
| `convert_latex_to_mathtype` | LaTeX 公式 → MathType 批量转换 |

### 文档分析工具

| 工具 | 功能描述 |
|------|---------|
| `read_document` | 读取文档内容（全文/标题结构/参考文献），只看内容不查格式 |
| `analyze_document` | 扫描文档现状（参考文献/图注/公式/缩写统计） |
| `summarize_document` | Map-Reduce 全文摘要（分块摘要 → 合成总结，覆盖全文） |
| `index_document` + `search_document` | RAG 向量检索（文档索引 → 语义搜索） |

### RAG 文献库

| 工具 | 功能描述 |
|------|---------|
| `index_literature` | 单篇文献索引（PDF/Word → 语义切块 → 向量化） |
| `search_literature` | 文献语义搜索（单篇精查 / 跨库泛搜） |
| `list_literature` | 列出已索引文献清单 |
| `auto_bind_literature` | 自动绑定参考文献编号 → 本地文件路径 |
| `verify_citations` | 引用溯源审计（批量忠实度校验） |
| `check_claim` | 单句级事实核查（Claim → Source 比对） |
| `analyze_figure` | 🖼️ PDF 图表视觉分析（多模态 LLM 驱动） |

### 高级能力

| 能力 | 描述 |
|------|---------|
| `execute_python` | 安全沙盒代码解释器（三层安全防护 + 进程级隔离） |
| `create_tool` / `approve_tool` | 🧬 Agent 动态工具创建能力（旧版可选模块，当前主链路不依赖） |
| `save/forget/list_learned_rules` | Agent 自学习系统（将经验存为规则，持续改进） |
| 三级分层记忆 | L1 核心规则 / L2 长期反思 / L3 短期对话，含召回率淘汰 + L2 融合节点 |
| Coordinator-Worker Swarm | 蜂群派发：Coordinator 动态 Fork Worker（Planner/Executor/Reviewer/Writer），Worker 自杀销毁 |
| `delegate_task` | 结构化任务委派（隔离工作区 + JSON 报告 + commit/rollback） |
| Token 水位压缩 | 基于 Token 估算的两级上下文压缩（规则截取 / LLM 摘要） |
| Prompt Cache 优化 | 静态根 + 动态叶分离架构，减少重复上下文传输（具体收益需按模型实测） |
| Skill 插件系统 | 按需加载技能手册（关键词 + Embedding 双层匹配） |
| 多 Key 自动轮换 | API Key 失效时自动切换到下一个可用 Key |
| 结构化错误恢复 | 三级错误分类 + 自动重试 + 引导 LLM 自修正 |
| 心跳看门狗 (COMSafeLock) | 信号驱动事件泵 + PID 差集精准击杀僵尸 Word 进程 |
| 工作区隔离 (Workspace) | Copy-on-Write 深拷贝 + commit/rollback 事务语义 + 路径逃逸防御 |

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 LLM API

```bash
cp config/config.example.toml config/config.toml
```

编辑 `config/config.toml`，填入你的 API Key。支持多 Key 逗号分隔（自动 Failover）：

```toml
api_key = "key1, key2, key3"    # 失效自动切换到下一个
```

也可以通过环境变量注入（优先级高于配置文件，推荐 Docker/CI 场景）：

```bash
# Windows
set DOCMASTER_API_KEY=your_key && python main.py
# Linux/Mac
DOCMASTER_API_KEY=your_key python main.py
# Docker
DOCMASTER_API_KEY=your_key docker compose up
```

推荐使用免费方案：

| 方案 | 申请地址 | 免费额度 |
|------|---------|---------|
| **Google Gemini** | [aistudio.google.com](https://aistudio.google.com/apikey) | 每天 1500 次请求 |
| **智谱 GLM-4-Flash** | [open.bigmodel.cn](https://open.bigmodel.cn/) | 永久免费 |
| **硅基流动** | [siliconflow.cn](https://cloud.siliconflow.cn/) | 注册送 2000 万 Token |

### 3. 测试连接

```bash
python main.py --test
```

### 4. 开始使用

```bash
python api.py
```

打开浏览器访问 `http://localhost:8000/`，创建科研项目并填写研究问题、方法素材、实验素材和参考文献目录。

也可以直接运行命令行 Agent，执行单项工具任务：

```bash
python main.py
```

例如：检查论文格式、检索参考文献、核验某条引用或生成文档摘要。

## 🏗️ 项目架构

```
agent/
├── config/
│   ├── config.example.toml     # 配置模板（多方案示例）
│   └── config.toml             # 实际配置（.gitignore）
├── core/
│   ├── schema.py               # 数据模型（Message, ToolCall, StreamEvent, AgentState）
│   ├── llm.py                  # LLM 接口封装（多 Key 自动轮换 + 流式 chat_stream）
│   ├── agent.py                # ReAct 引擎（run_async 流式状态机 + Token 压缩 + 心跳事件泵）
│   ├── prompt.py               # Prompt 工厂（Coordinator/Worker 角色 Prompt + Prompt Cache 分离）
│   ├── memory.py               # 三级分层记忆（L1核心/L2长期/L3短期 + 融合节点）
│   ├── embeddings.py           # Embedding + 向量存储（纯 numpy 实现）
│   ├── skills.py               # Skill 插件管理器（关键词 + Embedding 双层匹配）
│   ├── sandbox.py              # 安全沙盒（AST + builtins + 进程隔离 + Docker）
│   └── com_watchdog.py         # COM 安全锁（心跳看门狗 + PID 差集法 + 快照回滚）
├── tools/
│   ├── base.py                 # Tool 基类 + ToolRegistry
│   ├── doc_format_inspector.py # 📐 文档格式检查（样式/字体/缩进/行距诊断）
│   ├── doc_reader.py           # 文档内容读取
│   ├── doc_summarizer.py       # Map-Reduce 全文摘要
│   ├── rag.py                  # RAG 文献库（文档/文献索引 + 语义搜索 + 自动绑定）
│   ├── citation_verifier.py    # 引用溯源审计（批量忠实度 + 单句校验）
│   ├── figure_analyzer.py      # 多模态图表分析（PDF 图表 → 视觉 LLM）
│   ├── delegate.py             # 🐝 蜂群派发器（Fork Worker → 隔离执行 → 收割报告）
│   ├── pipeline.py             # 文档分析（宏观扫描）
│   ├── ref_formatter.py        # 参考文献格式化
│   ├── ref_crossref.py         # 文献交叉引用
│   ├── fig_crossref.py         # 图注交叉引用
│   ├── fig_caption.py          # 手写图注转题注
│   ├── acronym_checker.py      # 缩写检测
│   ├── latex_converter.py      # LaTeX 转换
│   ├── code_interpreter.py     # 安全沙盒代码解释器
│   ├── learned_rules.py        # Agent 自学习规则
│   ├── tool_creator.py         # 🧬 动态工具创建引擎
│   ├── memory_tool.py          # 记忆查询/保存
│   └── word_cleanup.py         # Word 进程清理
├── sandbox/
│   └── workspace.py            # 工作区隔离（Copy-on-Write + commit/rollback）
├── skills/                     # Skill 插件目录（.md 格式，热加载）
│   ├── paper_formatting.md     # 论文排版标准流
│   ├── doc_summary.md          # 文档摘要技能
│   ├── rag_search.md           # RAG 搜索技能
│   └── data_analysis.md        # 数据分析技能
├── memory/                     # 持久化存储
│   ├── history.json            # 操作历史（最近50条）
│   ├── learned_rules.json      # L1 核心规则（永不过期）
│   └── memory_vectors.json     # L2 + L3 向量记忆（含召回追踪）
├── docker/                     # Docker 沙盒微服务
└── main.py                     # 入口（工具注册 + Coordinator/Worker 初始化）
```

## 🧠 工作原理

### 单 Agent 模式（ReAct 循环）

```
用户: "帮我检查论文格式并处理参考文献"
  ↓
[Skill 匹配] "论文排版标准流" → 注入动态上下文
  ↓
[记忆召回] 向量搜索相关历史 → 注入动态上下文
  ↓
Agent (Think): 先用 inspect_document_format 检查格式，再处理参考文献
  ↓
Agent (Act): inspect_document_format → 发现3处缩进问题
  ↓
Agent (Act): format_references → 修复25条参考文献
  ↓
Agent (Observe): 成功 → 继续 | 失败 → 结构化错误引导自修正
  ↓
Agent: "已完成！" → 保存 Q+A 到 L3 短期记忆
```

### Prompt Cache 优化（静态根 + 动态叶）

```
┌────────────────────────────────────────────────────────┐
│ Message[0]: SYSTEM（静态根 — 跨轮次不变，KV Cache 缓存） │
│   L1 核心规则 + 工具描述 + 行为准则 + L1 尾部重复         │
├────────────────────────────────────────────────────────┤
│ Message[1]: SYSTEM（动态叶 — 每轮变化，不污染前缀缓存）   │
│   匹配到的 Skill 手册 + RAG 召回的历史记忆               │
├────────────────────────────────────────────────────────┤
│ Message[2..n]: USER / ASSISTANT / TOOL                │
│   用户消息（含 L1 三明治注入） + 工具调用和结果            │
└────────────────────────────────────────────────────────┘

设计原理：Prompt Cache 匹配 Token 序列的最长公共前缀。系统将稳定的系统规则和动态的项目素材分离，减少不必要的前缀变化；实际命中率和成本收益需要结合具体模型、请求长度和调用方式实测。
```

### Coordinator-Worker Swarm（蜂群协作）

```
用户: "帮我全面排版这篇论文"
  ↓
Coordinator（主 Agent，持有 delegate_task）
  ├── 🔍 delegate_task(role=Planner)  → JSON 报告（执行计划）→ Worker 销毁
  ├── ⚙️ delegate_task(role=Executor) → 隔离工作区执行 → PASS → commit 回写
  ├── ⚙️ delegate_task(role=Executor) → 隔离工作区执行 → PASS → commit 回写
  ├── 📝 delegate_task(role=Reviewer) → 独立审查 + L1 宪法校验 → JSON 报告
  └── 综合各 Worker 报告 → 用清晰中文向用户汇报

关键设计：
  - Worker 无 delegate_task 工具（防套娃）
  - Worker 在隔离工作区操作副本（status≠PASS → 丢弃，原文件毫发无伤）
  - Worker 自杀销毁后，几千 Token 垃圾日志随之消亡
  - Coordinator 只收到一条清爽的 JSON 报告
```

### 三级分层记忆 (Hierarchical Memory)

```
┌─────────────────────────────────────────────────────────────────┐
│  L1: Core Memory (核心规则库)                                    │
│  learned_rules.json                                             │
│  永不压缩 · 永不过期 · System Prompt 强制注入                      │
│  例: "工具执行后关闭Word进程"                                      │
├─────────────────────────────────────────────────────────────────┤
│  L2: Long-term RAG (长期向量记忆)                                 │
│  memory_vectors.json (type=reflection)                          │
│  来源: 子任务反思 / L3 晋升                                       │
│  FIFO 上限 50 条 · 冲突时 LLM 融合判断(REPLACE/MERGE)             │
│  例: "交叉引用断裂时，先转纯文本再挂书签；仍失败则删旧书签重建"        │
├─────────────────────────────────────────────────────────────────┤
│  L3: Short-term Conversation (短期对话记忆)                      │
│  memory_vectors.json (type=conversation)                        │
│  30天 TTL + 召回率末尾淘汰                                        │
│  eviction_score = recall_count × exp(-days_since_recall / 30)   │
│  recall_count ≥ 5 → 自动晋升到 L2                                │
└─────────────────────────────────────────────────────────────────┘
```

冲突消解规则（层感知）：

| 新记忆 \ 命中旧记忆 | L3 | L2 | L1 |
|---|---|---|---|
| **L3** | ✅ 同层替换 | ❌ 不动 L2 | ❌ 不动 L1 |
| **L2** | 🗑️ 清理冗余 L3 | 🔀 LLM 融合判断 | ❌ 不动 L1 |

### Token 水位线压缩 (Context Overflow Hook)

```
估算 Token ─── < 6000 ───→ 不压缩
    │
    ├── 6000~8000 ──→ Tier 1: 纯规则截取（零 LLM 成本）
    │
    └── ≥ 8000 ────→ Tier 2: LLM 智能摘要（~600 Token 成本）
```

### 格式感知架构（查格式 vs 查内容，职责分离）

```
                    ┌─ read_document ──→ 纯文本内容（适合摘要/搜索/问答）
用户文档 ──→ COM ──┤
                    └─ inspect_format ──→ 格式属性报告（适合排版诊断/修复验证）
                         ↑
                    只输出20字定位标记
                    不输出段落全文（省 Token）
```

### 安全沙盒（三层防护 + Docker 隔离）

```
Agent 生成的代码 → Layer 1: AST 静态分析（拦截危险模式）
                → Layer 2: builtins 白名单（运行时纵深防御）
                → Layer 3: 子进程隔离 + 超时强杀（资源兜底）
                → Layer 4: Docker 容器隔离（OS 级沙盒）
```

## 🐳 Docker 部署

### 单容器运行

```bash
docker build -t docmaster-agent .
docker run -d -p 8000:8000 -v ./config:/app/config docmaster-agent
```

### 微服务架构（Docker Compose）

```bash
docker compose up -d    # 一键启动 Agent + Sandbox 两个容器
docker compose down     # 停止并清理
```

```
┌─── Docker 内部网络 ───────────────────┐
│                                       │
│  agent (8000 对外)                    │
│    ├── FastAPI + ReAct Agent          │
│    └── 调用 sandbox 执行代码           │
│           ↕ 内部通信                   │
│  sandbox (8001 仅内部)                │
│    ├── 独立沙盒微服务                  │
│    └── 资源限制: 1 CPU / 256MB        │
│                                       │
└───────────────────────────────────────┘
```

## 📄 License

MIT
