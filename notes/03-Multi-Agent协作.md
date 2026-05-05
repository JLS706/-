# Multi-Agent 多角色协作是怎么实现的？

## 为什么要用多 Agent 而不是单 Agent？

### 具体回答

单 Agent 的致命伤：
1. **上下文污染**——执行复杂任务时产生几千 Token 的中间日志，导致 LLM"注意力涣散"
2. **角色精神分裂**——同一个 LLM 既要规划又要执行又要审查，认知负荷过重
3. **缺乏验证**——做完就完了，没有"第二双眼睛"检查结果

Multi-Agent 的解决方案——**Coordinator-Worker Swarm（蜂群模式）**：

| 角色 | 职责 | 权限 |
|------|------|------|
| **Coordinator** | 理解需求、拆解任务、收割报告、汇报用户 | 拥有 `delegate_task`，可 Fork Worker |
| **Worker (Planner)** | 分析文档、制定执行计划 | 只读工具 |
| **Worker (Executor)** | 执行具体的文档处理操作 | 所有文档处理工具（无 `delegate_task`） |
| **Worker (Reviewer)** | 读取处理后文档、L1 宪法审查 | 只读工具 + L1 一票否决权 |
| **Worker (Preprocessor)** | 清洗无结构文本、结构推演 | 只读 + 代码分析工具 |
| **Worker (Writer)** | 基于文献撰写学术段落 | 文献搜索 + 引用校验工具 |

**关键设计**：所有 Worker 共享同一个 LLM 实例（复用连接），通过 `core/prompt.py` 中的角色 Prompt（`build_worker_prompt()`）切换身份。Coordinator 自动探测——Agent 的 registry 中有 `delegate_task` 工具就是 Coordinator，没有就是 Worker。

![Uploading 473abafd30a70feb4c2ad34248487b1e.png…]()


## Coordinator-Worker 的架构核心是什么？

### 具体回答

核心在 `tools/delegate.py` 的 `DelegateTaskTool`，它实现了**星形拓扑（Star-Shaped）**的蜂群派发：

```
                    ┌─ Worker(Planner) ──→ JSON 报告 → 销毁
                    │     └─ 隔离工作区
Coordinator ──Fork──┼─ Worker(Executor) ──→ JSON 报告 → 销毁
  (主Agent)         │     └─ 隔离工作区（commit 回写原文件）
                    │
                    └─ Worker(Reviewer) ──→ JSON 报告 → 销毁
                          └─ 隔离工作区（只读）
```

**执行流程**（`DelegateTaskTool.execute()`）：

```
1. 派生工具子集：从主 registry 中 exclude("delegate_task") → 防套娃
2. 创建隔离工作区：workspace.session(task_id, target_file) → 深拷贝文件
3. 构建 Worker Prompt：build_worker_prompt(role, objective, work_path)
4. Fork 子 Agent：Agent(llm, worker_tools, memory=None, skill_manager=None)
5. Worker 在工作区内执行 ReAct 循环 → 输出 JSON 报告
6. status=PASS → commit(回写原文件)；否则 → 丢弃修改
7. Worker 实例 del 销毁，几千 Token 中间日志随之消亡
8. Coordinator 只收到一条清爽的 ToolResult（JSON 报告）
```

## 业界还有哪些 Multi-Agent 编排范式？（面试广度题）

> 面试官问"你还知道哪些多 Agent 机制"时，用行业标准术语回答，展现技术广度。

### 具体回答

| 编排范式 | 代表项目 | 原理 | 优点 | 缺点 |
|---------|---------|------|------|------|
| **星形蜂群** | **我的项目** | Coordinator 动态 Fork Worker，Worker 无状态自杀 | 上下文隔离彻底，扁平化无套娃 | 不适合 Worker 间需要频繁通信的场景 |
| **线性 Pipeline** | 经典编排 | A → B → C 顺序执行 | 简单可控 | 灵活性差，某步失败难回溯 |
| **DAG 状态机** | LangGraph / LangChain | 节点 = Agent，边 = 条件路由，构成有向无环图 | 可视化强，适合有固定 SOP 的业务 | 需要预定义所有状态和转移 |
| **角色扮演辩论** | ChatDev / MetaGPT | 多个 Agent 扮演不同角色，互相提意见整合 | 模拟人类团队，创意发散强 | Token 消耗大、容易跑偏 |
| **黑板模式 (Blackboard)** | 经典分布式 AI 架构 | 所有 Agent 读写同一块"共享黑板"，各自独立决策 | 解耦彻底 | 一致性维护复杂 |
| **竞标/拍卖式** | AutoGen | 任务广播给所有 Agent，能力最匹配的竞标认领 | 任务自动路由 | 需要精确的能力评估机制 |

**我的项目为什么选"星形蜂群 + 微观 ReAct"？**

1. **上下文隔离是第一优先级**——排版任务产生大量 COM 日志，Worker 自杀销毁后日志一起消亡，Coordinator 窗口始终干净
2. **微观用 ReAct**——每个 Worker 内部用 Think-Act-Observe 循环自主探索，不需要外部编排
3. **扁平化约束**——Worker 没有 `delegate_task` 工具，物理上不可能套娃派发

面试升级话术：*"单体 ReAct 在长任务中会被中间日志'注意力涣散'所淹没；LangGraph 的 DAG 状态机在无固定 SOP 的排版场景下不够灵活。我选择的是星形蜂群（Star-Shaped Swarm）——Coordinator 动态 Fork 无状态 Worker，Worker 执行完输出 JSON 报告后直接自杀销毁，从物理层面保证 Coordinator 上下文的绝对纯净。"*

## 工作区隔离是怎么做的？

### 具体回答

`sandbox/workspace.py` 实现了 **Copy-on-Write + Commit/Rollback** 的文件隔离：

```
WorkspaceProvider (ABC)
  └── LocalFolderWorkspace ← MVP：本地临时文件夹隔离

用法（在 DelegateTaskTool 中）：
  with workspace.session(task_id, original_file) as ctx:
      # ctx.work_path    → 工作区中的文件副本路径
      # ctx.workspace_dir → 工作区根目录（如 C:\temp\ws_abc123\）
      worker.run(f"请处理文件: {ctx.work_path}")
      ctx.commit()  # 成功：把结果拷贝回原路径
  # with 退出时自动清理工作区（无论成功失败）
```

**安全设计**：
1. **深拷贝隔离**：Worker 永远操作的是副本，原文件毫发无伤
2. **路径逃逸防御**：`commit()` 用 `os.path.commonpath` 校验源路径必须在工作区内
3. **自动核平**：`with` 退出时 `shutil.rmtree` 清理整个工作区
4. **只有 PASS 才回写**：Worker 报告 `status=PASS` 才触发 `commit()`，其他情况丢弃

## Worker 的角色 Prompt 是怎么区分的？

### 具体回答

`core/prompt.py` 的 `build_worker_prompt(role, objective, target_file, tool_descriptions)` 按 role 选择基础 Prompt：

| role（大小写不敏感） | 基础 Prompt | 特殊能力 |
|---------------------|-------------|----------|
| `Planner` | `PLANNER_PROMPT` | 只分析不执行，输出分步执行计划 |
| `Reviewer` | `REVIEWER_PROMPT` | 注入 L1 宪法段落，L1 违规一票否决 |
| `Preprocessor` | `PREPROCESSOR_PROMPT` | 文本清洗 + 结构推演 |
| `Writer` | `WRITER_PROMPT` | 文献检索 + 引用标注 + 忠实度校验 |
| 其他（如 `Executor`） | `GENERIC_WORKER_BASE` | 通用工具执行者 |

**所有 Worker 共享 Swarm 后缀**（`SWARM_WORKER_SUFFIX`）：
1. 专注单一目标，不做范围外的事
2. 没有 `delegate_task` 工具，不能套娃
3. 完成后必须输出结构化 JSON 报告（status/summary/output_path/issues_found/actions_taken）
4. 写入工具必须 `modify_in_place=True`，严禁生成衍生文件
5. Word 操作完成后必须调用 `close_word`

## Worker 的心跳是怎么中继到 Coordinator 的？

### 具体回答

Worker 通过 `run_async()` 异步执行，`DelegateTaskTool` 在一个独立事件循环中消费 Worker 的 `StreamEvent`：

```
Worker 执行工具
  → 工具调用 report_progress() → 写入 Queue → 事件泵 yield StreamEvent("tool_progress")
  → DelegateTaskTool._drive_worker() 监听事件
    → tool_progress → 冒泡给 Coordinator（钳位到 [5, 95]）
    → tool_start / tool_end → 中继进度
    → tool_timeout → Worker 内部已熔断
    → text → 收集最终文本
    → error → 记录警告
```

**防御加固**：`_drive_worker()` 顶层 `try/except` 捕获所有内层异常（含 Worker 看门狗触发的 `CancelledError` / COM 异常），防止异常击穿嵌套事件循环导致 Coordinator 静默崩溃。

## Skill Config 是怎么透传给 Worker 的？

### 具体回答

Coordinator 通过 `SkillManager.match()` 提取 `_active_config`（如格式化规则、引用格式等领域知识）。`DelegateTaskTool` 在 Fork Worker 时透传：

```python
# DelegateTaskTool.execute() 中：
if self._coordinator and hasattr(self._coordinator, '_active_config'):
    worker._active_config = dict(self._coordinator._active_config)
```

Worker 没有 `SkillManager`（无状态），但拥有 `_active_config`，在 `_inject_skill_config()` 中按工具声明的 `injected_configs` 注入参数。这实现了 **Skill 知识从 Coordinator 到 Worker 的单向透传**。

---

## 【深水区】单 Agent vs. 多 Agent：深度博弈

### 具体回答

| 架构模式 | 优势 (Pros) | 劣势 (Cons) |
| :--- | :--- | :--- |
| **单 Agent** | **状态集中**：单一上下文，无信息衰减；**调试简单**：单一日志链，归因清晰；**评估方便**：容易进行端到端测试 | **上下文瓶颈**：长文档处理时注意力涣散；**角色冲突**：自审自写导致质量下降；**工具冗余**：工具超过 10 个时模型选错工具概率激增 |
| **多 Agent** | **职责解耦**：Planner/Executor/Reviewer 角色分明；**上下文隔离**：子任务日志不污染主窗口；**并行潜力**：可扩展异步并行处理 | **通信损耗**：自然语言 Handoff 会丢失关键约束；**状态碎片化**：状态丢失难以定位是谁的责任；**复杂性爆炸**：增加通信开销与失败节点 |

## 【深水区】架构决策框架：何时该拆分？

### 具体回答

**默认先做强单 Agent**。只有当满足以下 **"四个前提"** 中的至少两条时，才值得考虑拆分：

1. **角色天然分离**：如 Coder 与 Reviewer。审稿人必须拥有独立的系统提示词与评判标准，避免"人格分裂"导致的质量打折
2. **工具集差异大**：当工具超过 10 个，模型调错工具的概率显著增加。应按业务域拆分，使每个 Agent 仅管理 3-5 个垂直工具
3. **上下文冲突明显**：某些环节需要全局视野（如万字文档结构），某些环节需要精细局部细节。合在一起会导致关键信息被"淹没"
4. **并行探索收益**：当拆分能带来真实的端到端延迟缩减（非假并行）

**本项目命中了前三条**：Reviewer 需要独立 L1 宪法标准（条件 1）；全量工具 30+ 个（条件 2）；Coordinator 只需全局摘要而 Worker 需要 COM 级细节（条件 3）。

## 【深水区】进化案例：从"一竿子到底"到"蜂群派发"

### 具体回答

**第一阶段：单 Agent 全干模式**
- 架构：一个 Agent 配备所有工具（`read_document`、`format_references`、`fig_crossref` 等）
- 痛点：处理 1500 行以上文档时上下文逼近 Token 上限；Agent 倾向于对自己写的东西打高分，Review 形同虚设

**第二阶段：Coordinator-Worker 蜂群模式（本项目现状）**
- 架构：Coordinator（管家）+ Ephemeral Workers（临时工）
- 改进点：
  - **结构化 Handoff**：不传自然语言摘要，传 JSON 格式的任务 Spec（`role` + `objective` + `target_file`）
  - **Worker 自杀机制**：Worker 执行完即 `del` 销毁，几千 Token 的垃圾日志不回流主窗口，仅返回结构化 JSON 报告
  - **Trace ID 链路**：通过 `task_id`（UUID 前 12 位）串联 Coordinator 与 Worker 日志，解决归因难问题

## 【深水区】硬核工程实践总结（本项目特色）

### 具体回答

**4.1 语义断层检测 (Semantic Cliff Detection)**

针对"胃病与小米粥"等逻辑突变引发的误切：
- 算法：不依赖硬阈值，采用 `μ + c·σ` 异常检测。只有当余弦距离显著偏离全篇均值时才下刀
- 平滑处理：引入卷积滑动窗口消除局部过渡句噪音，确保语义块的内聚性

**4.2 物理级沙盒防线**

- 进程级隔离：Worker 永远在隔离的 `Workspace` 中操作副本。若 `status` 不为 `PASS`，则直接核平工作区，回滚快照
- AST 静态拦截：在代码执行前，利用 `ast.NodeVisitor` 拦截危险调用（如 `os.remove`），在第一层就消灭攻击向量

**4.3 跨执行绪心跳熔断**

- 心跳中继：子 Agent 的 `tool_progress` 会通过异步事件冒泡给主监控
- 精准狙击：看门狗监控 `stall_seconds`，一旦黑盒 COM 接口假死，直接通过 PID 差集锁进行物理强杀，杜绝僵尸进程

## 【深水区】给面试官的锦囊

### 具体回答

**一句话定性**：
> "单 Agent 的核心优势是状态集中与调试简单。我们之所以在 DocMaster Agent 中引入多 Agent 派发模式，是为了解决角色互斥与上下文冲突，并利用结构化 Schema 彻底解决了自然语言摘要带来的'信息污染'问题。"

**面试追问路由**：

| 追问方向 | 应答要点 |
|---------|---------|
| "为什么不用 LangGraph？" | DAG 状态机需要预定义所有状态转移，排版任务无固定 SOP；星形蜂群动态 Fork，更灵活 |
| "Worker 之间怎么通信？" | 不通信——星形拓扑，所有信息汇聚到 Coordinator，Worker 间物理隔离 |
| "怎么保证 Worker 不失控？" | 三层防线：无 `delegate_task`（防套娃）+ 隔离工作区（防破坏）+ 心跳熔断（防僵死） |
| "状态丢了怎么办？" | Worker 无状态，崩溃即丢弃；Coordinator 只看 JSON 报告，状态天然集中 |
| "怎么评估多 Agent 效果？" | 比较相同任务下单 Agent vs. Swarm 的 Token 消耗、成功率、最终文档质量评分 |

---

## 【深水区】Workflow 还是 Multi-Agent？工业级架构选型

> 被滥用的"Agent"概念：把"步骤多、流程复杂"等同于"需要上 Agent"是业界普遍的认知误区。真正的架构师必须具备在"能做"和"该做"之间划线的能力。

### 具体回答

**架构选型四要素 (Decision Framework)**：

| 维度 | 判断 | 违反后果 |
|------|------|---------|
| **路径是否已知？** | 已知 → Workflow。能在白板上画出完整流程图，就不需要模型来决定下一步 | 把确定路径交给模型"重新发明"，延迟翻倍、调试成本暴涨 |
| **错误成本多高？** | 高 → Workflow 更安全。Agent 的决策不确定性意味着同样的输入可能走不同路径 | 一次坏决策触发不可逆操作（如改错格式、删错数据） |
| **是否需要运行时判断？** | 是 → Agent。下一步做什么取决于前一步的非结构化结果（如报错后决定重试还是换工具） | Workflow 写不出来 |
| **对时延和稳定性的要求？** | 要求高 → Workflow。Agent 每次循环都多一次模型思考调用 | 用户等 15 秒只为了得到一个本来 2 秒就能出的答案 |

> **黄金法则**：默认先做 Workflow，只有当任务必须依赖大模型在运行时收集新信息做动态决策时，才值得上 Agent。

## 【深水区】常见的"伪 Agent"：五大经典 Workflow 模式

### 具体回答

很多被包装成"多 Agent"的系统，本质上只是以下五种 Workflow 模式的变体：

1. **Prompt Chaining（分步串联）**：一步做完再进下一步（提取 → 验证 → 生成）
2. **Routing（路由分流）**：基于输入分类，再走不同处理链
3. **Parallelization（并行处理）**：独立子任务同时跑，或同一任务多次执行取最优
4. **Orchestrator-Workers（中心调度）**：中心 LLM 拆任务并分配 Worker。注意：若分配逻辑和路径可枚举（能用 if-else 写出），本质仍是 Workflow
5. **Evaluator-Optimizer（循环优化）**：一个 LLM 生成，另一个评审，循环迭代直到达标

## 【深水区】混合架构实战：DocMaster Agent 的 Plan → Execute → Review

### 具体回答

DocMaster Agent 的核心链路 `Plan → Execute → Review` 是一个经典的混合架构案例：

**战略上的确定性（宏观 Workflow 兜底）**

文档排版属于典型的"错误成本极高"场景。系统**用代码状态机锁死了 Plan-Execute-Review 的流转路径**（Evaluator-Optimizer 模式），硬性剥夺了模型"偷懒跳过 Review 环节"的自由意志。

**战术上的自主性（微观 Agent 探索）**

虽然宏观路径被锁死，但在此工作流管道中被派发出来的每一个 Worker，都是一个完整的独立 Agent：
- **独立的上下文**：Worker 在隔离沙盒中产生数千 Token 试错日志，销毁时直接清空，绝不污染主窗口
- **专用的工具集**：Reviewer 被限制只能使用只读工具，从物理层面防止"既当裁判又当运动员"
- **自主的 ReAct 推理**：当 Executor 遇到 Word COM 接口报错时，它需要自主分析 `Error Observation` 并重试，而非等待硬编码的死板指令

**⚠️ 当前代码现状 vs. 架构目标的差距**

当前 `core/agent.py` 的主循环仍然是纯 ReAct 自由循环（`for step in range(...)`），`AgentState` 只有 5 个状态（IDLE/THINKING/ACTING/FINISHED/ERROR），仅作为仪表盘展示而不参与路由决策。"Plan → Execute → Review" 的三步走完全依赖 `COORDINATOR_PROMPT_STATIC` 的自然语言建议，代码层面零拦截——Coordinator 可以跳过 Review、先 Review 再 Execute、或连续派 3 个 Executor 不 Review。

**演进方向**：从纯 ReAct 走向"路由 + 状态机"混合驱动——凡是路径确定的流水线，应从大模型的自由意志中剥离出来，用代码级 if-else 或状态机进行硬性拦截和锁死。

### 三层路由改造方案（Intent Classifier + FSM）

核心思想：**主 Agent 不是自由发挥写工作流的创作者，而是一个极其轻量级的意图分类器（Intent Classifier）。一旦分类完成，Python 硬编码的状态机接管全部流转。**

#### 第一层：约束解码（Constrained Decoding / Structured Output）

主 Agent 接收用户输入后，通过 Function Calling 的结构化输出强制只返回枚举值：

```python
class TaskIntent(Enum):
    TASK_FULL         = "full"          # 全量：Plan → Execute → Review
    TASK_REVIEW_ONLY  = "review_only"   # 仅审查
    TASK_FORMAT_ONLY  = "format_only"   # 仅排版（跳过 Plan）
    TASK_EXECUTE_ONLY = "execute_only"  # 仅执行（跳过 Plan + Review）
    TASK_SIMPLE       = "simple"        # 简单任务：Coordinator 直接调工具，不 Fork
```

LLM 不输出冗长计划，只做**单选题**。将发散的生成任务降维成确定性的分类任务，极大降低幻觉。

#### 第二层：Python 状态机硬接管（防遗漏的核心）

一旦 LLM 吐出枚举值，大模型的路由任务就**结束了**。后续状态流转完全由 Python FSM 强行接管：

```python
# 每种 Intent 对应一条硬编码的 Worker 调度链
_INTENT_PIPELINES = {
    TaskIntent.TASK_FULL: [
        ("Planner",  "分析文档现状并制定执行计划"),
        ("Executor", "按 Planner 计划执行排版操作"),
        ("Reviewer", "审查执行结果，L1 铁律一票否决"),
    ],
    TaskIntent.TASK_REVIEW_ONLY: [
        ("Reviewer", "全面审查文档格式与内容"),
    ],
    TaskIntent.TASK_FORMAT_ONLY: [
        ("Executor", "执行排版操作"),
        ("Reviewer", "审查排版结果"),
    ],
    TaskIntent.TASK_EXECUTE_ONLY: [
        ("Executor", "执行指定操作"),
    ],
}
```

大模型只负责**选轨道**，而**轨道上有几个检查站**是由传统硬编码决定的，绝对不可能遗漏。

#### 第三层：悲观降级（Pessimistic Fallback 兜底）

如果 LLM 的意图分类器犯傻（返回非法值、超时、JSON 解析失败），系统默认拨向最安全的 `TASK_FULL`，宁可多花 Token 跑完整 Plan→Execute→Review，也绝不漏掉用户的潜在需求。

#### 实现位置

| 文件 | 职责 |
|------|------|
| `core/router.py`（新建） | `TaskIntent` 枚举 + `classify_intent()` 意图分类 + `TaskFSM` 状态机 |
| `core/agent.py` | `run()` 主循环接入 Router：先分类 → FSM 驱动 `delegate_task` 调度链 |
| `tools/delegate.py` | 保留，但不再由 Coordinator LLM 自由调用，改为 FSM 程序化调用 |

#### 架构图

```
用户输入
  ↓
Coordinator LLM（约束解码，只输出枚举）
  ↓
┌─────────────────────────────────────────────────┐
│  Python FSM 硬接管（代码级 if-else，不可跳过）     │
│                                                   │
│  TASK_FULL:                                       │
│    Planner ──→ Executor ──→ Reviewer（强制三步）   │
│                                                   │
│  TASK_REVIEW_ONLY:                                │
│    Reviewer（只走这一步）                          │
│                                                   │
│  TASK_FORMAT_ONLY:                                │
│    Executor ──→ Reviewer（强制两步）               │
│                                                   │
│  TASK_SIMPLE:                                     │
│    Coordinator 直接调工具，不 Fork Worker           │
└─────────────────────────────────────────────────┘
  ↓
向用户汇报
```

> 面试话术：*"我没有把命脉交给大模型的'自主决策'。我把主 Agent 降维成了一个极其轻量级的意图分类器，只做单选题——TASK_FULL、TASK_REVIEW_ONLY 还是 TASK_FORMAT_ONLY。一旦分类完成，接下来的状态流转完全由 Python 状态机硬接管。大模型只是一个聪明的'道岔开关'，而下面跑的火车和铁轨，全是用死板的工程代码焊死的。"*

## 【深水区】面试高频连环追问

### 具体回答

**追问 1："你做过的系统里，有没有一开始上了 Agent 后来改回 Workflow 的？为什么？"**

在 DocMaster Agent 项目中，最初的 `Plan → Execute → Review` 质量保障链路完全依赖主控 Agent 的 ReAct 自由意志驱动。上线后暴露了严重的"LLM 惰性"问题：当执行节点返回"成功"时，主控 Agent 为了省事，极大概率会直接输出"任务完成"，从而跳过关键的 Review 审查环节。由于学术排版的错误成本极高，后续重构将宏观流转全面降级为代码硬编码的 Evaluator-Optimizer 工作流（状态机）。一旦进入修改阶段，系统强制接管路径，必须经过 Reviewer 节点的独立校验。

*关键得分点*：明确指出"因为路径确定、错误成本高、且需要用代码规避大模型的不可靠惰性"。

**追问 2："Orchestrator-Workers 和 Agent 的核心区别是什么？"**

Orchestrator-Workers 属于 Workflow 模式，不是纯 Agent。核心区别：Orchestrator 的子任务由输入决定，但路径是**可枚举的**（可以用 if-else 或 Prompt Routing 穷举）。而纯 Agent 的下一步由模型在运行时动态规划，路径不可预知。混淆两者的原因往往是为了包装概念。

**追问 3："如果时延要求在 2 秒以内，你还会用 Agent 吗？"**

绝对不会。Agent 至少需要一次"决定下一步"的模型推理调用，加上工具执行、状态观测的闭环，2 秒几乎不可能完成。在严格的时延约束下，Workflow 是唯一具备工业级可行性的选择。

## 【深水区】降维反击：被质疑"你这不就是个工作流"时

### 具体回答

> "业界经常把多个大模型自由对话的玩具系统等同于 Multi-Agent，但在工业落地中，失控的自由意志是一场灾难。
> 我的系统是一个 **Workflow-Orchestrated Multi-Agent** 架构。在宏观战略上，我用 Evaluator-Optimizer 工作流锁死了流转路径，剥夺了模型跳过质量检查的自由意志；但在微观战术上，每一个 Worker 都是基于 ReAct 驱动的独立 Agent，需要自主推理解决底层执行报错。
> 我不追求拟人化的多 Agent 聊天，我追求的是用进程管理的哲学实现极端的上下文解耦，用确定性的 Workflow 去兜底概率性的智能幻觉。"
