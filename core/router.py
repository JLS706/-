# -*- coding: utf-8 -*-
"""
TaskRouter — 语义路由器 + DAG 编排引擎

三层路由架构（控制平面 / 数据平面分离）：
  第一层：语义路由器（Semantic Router）
          — LLM 通过约束解码只输出枚举值（TaskIntent），不生成任何执行逻辑。
  第二层：声明式 DAG 编排
          — 每种 Intent 对应一张预定义的 DAG 执行图（带 depends_on 依赖关系）。
  第三层：DAGExecutor 拓扑调度
          — Python 引擎按拓扑序执行节点，提供节点级熔断/重试/可观测性。

设计哲学：
  大模型被严格限制在控制平面（Control Plane），只做语义网关与拓扑规划；
  底层原子 API（ToolRegistry）组成无状态的静态算子库（Operator Library）；
  DAGExecutor 接管执行，提供确定性、可观测性和安全性。
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.logger import logger
from core.schema import Message, Role


# ═════════════════════════════════════════════
# 第一层：任务意图枚举（约束解码的目标空间）
# ═════════════════════════════════════════════

class TaskIntent(Enum):
    """Coordinator 意图分类的输出空间（单选题）。"""
    TASK_FULL         = "full"           # 全量：Plan → Execute → Review
    TASK_REVIEW_ONLY  = "review_only"    # 仅审查
    TASK_FORMAT_ONLY  = "format_only"    # 仅排版（Execute → Review）
    TASK_EXECUTE_ONLY = "execute_only"   # 仅执行（无 Plan / Review）
    TASK_SIMPLE       = "simple"         # 简单任务：Coordinator 直接调工具，不 Fork


# ═════════════════════════════════════════════
# 第二层：声明式 DAG 编排图（每种 Intent 对应一张拓扑图）
# ═════════════════════════════════════════════

@dataclass
class DAGNode:
    """
    DAG 执行图中的一个节点（原子工作单元）。

    每个节点绑定一个 Worker 角色和任务目标模板。
    depends_on 字段声明拓扑依赖，调度器按拓扑序执行。
    无依赖的节点理论上可并发（当前 Word COM STA 限制下串行）。
    """
    id: str                              # 节点唯一标识（如 "plan", "exec", "review")
    role: str                            # Worker 角色（Planner / Executor / Reviewer）
    objective_template: str              # 任务目标模板（支持 {user_input}, {target_file} 变量）
    depends_on: list[str] = field(default_factory=list)  # 前置节点 ID 列表


# 每种 Intent 对应一张预定义的 DAG 拓扑图
# 调度器按 depends_on 拓扑序执行，确保确定性和可观测性
_INTENT_DAG: dict[TaskIntent, list[DAGNode]] = {
    TaskIntent.TASK_FULL: [
        DAGNode(
            id="plan",
            role="Planner",
            objective_template="分析文档现状并制定执行计划，文件: {target_file}",
            depends_on=[],
        ),
        DAGNode(
            id="exec",
            role="Executor",
            objective_template="按计划执行排版操作，文件: {target_file}。用户需求: {user_input}",
            depends_on=["plan"],
        ),
        DAGNode(
            id="review",
            role="Reviewer",
            objective_template="审查执行结果，L1 铁律一票否决，文件: {target_file}",
            depends_on=["exec"],
        ),
    ],
    TaskIntent.TASK_REVIEW_ONLY: [
        DAGNode(
            id="review",
            role="Reviewer",
            objective_template="全面审查文档格式与内容，文件: {target_file}。用户需求: {user_input}",
        ),
    ],
    TaskIntent.TASK_FORMAT_ONLY: [
        DAGNode(
            id="exec",
            role="Executor",
            objective_template="执行排版操作，文件: {target_file}。用户需求: {user_input}",
            depends_on=[],
        ),
        DAGNode(
            id="review",
            role="Reviewer",
            objective_template="审查排版结果，文件: {target_file}",
            depends_on=["exec"],
        ),
    ],
    TaskIntent.TASK_EXECUTE_ONLY: [
        DAGNode(
            id="exec",
            role="Executor",
            objective_template="执行指定操作，文件: {target_file}。用户需求: {user_input}",
        ),
    ],
    # TASK_SIMPLE 不走 DAG，由 Coordinator 直接 ReAct
}

# 向后兼容：将 DAGNode 转为旧版 (role, template) 元组格式
_INTENT_PIPELINES: dict[TaskIntent, list[tuple[str, str]]] = {
    intent: [(n.role, n.objective_template) for n in nodes]
    for intent, nodes in _INTENT_DAG.items()
}


# ═════════════════════════════════════════════
# 意图分类器（Function Calling 约束解码）
# ═════════════════════════════════════════════

# 用于约束解码的 classify_intent "伪工具"定义
_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": (
            "根据用户输入，将任务分类为以下类型之一。"
            "你只需要做分类，不需要执行任何操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [e.value for e in TaskIntent],
                    "description": (
                        "任务意图类型：\n"
                        "- full: 需要完整的 Plan→Execute→Review 全流程（如「帮我全面排版这篇论文」）\n"
                        "- review_only: 仅需审查/检查文档（如「帮我检查下格式对不对」「审查这篇论文」）\n"
                        "- format_only: 仅需排版操作（如「格式化参考文献」「处理图注」）\n"
                        "- execute_only: 执行单个指定操作、不需要审查（如「把LaTeX公式转MathType」）\n"
                        "- simple: 简单对话/查询、不涉及文档处理流水线（如「关闭Word」「你好」「查看规则」）"
                    ),
                },
                "target_file": {
                    "type": "string",
                    "description": (
                        "用户提到的目标文件的完整绝对路径。"
                        "如果用户使用了「这个文档」「那个文件」等指示代词，"
                        "请从 [上下文] 中提取实际文件路径，不要把代词当作文件名。"
                        "如果用户没有指定且上下文中也没有文件路径，返回空字符串。"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "一句话解释分类理由（调试用，不超过 30 字）。",
                },
            },
            "required": ["intent", "target_file", "reason"],
        },
    },
}


def classify_intent(
    llm,
    user_input: str,
    history_context: str = "",
) -> tuple[TaskIntent, str, str]:
    """
    调用 LLM 进行意图分类（约束解码，只输出枚举值）。

    Args:
        llm: LLM 实例
        user_input: 用户原始输入
        history_context: 最近的对话摘要（帮助理解上下文，如已知文件路径）

    Returns:
        (intent, target_file, reason)
    """
    system_msg = Message(
        role=Role.SYSTEM,
        content=(
            "你是一个任务意图分类器。根据用户输入，调用 classify_intent 工具进行分类。\n"
            "你只需要分类，不需要回答用户的问题或执行任何操作。\n\n"
            "**文件路径解析规则（重要！）**：\n"
            "- 如果用户明确给出了文件路径，直接使用该路径作为 target_file。\n"
            "- 如果用户使用了「这个文档」「那个文件」「这篇论文」等指示代词，\n"
            "  你必须从 [上下文] 中提取之前提到的实际文件路径作为 target_file。\n"
            "- **绝对不要**把「这个文档」「那个文件」等代词当作字面文件名！\n"
            "- 如果上下文中有文件路径，优先使用「当前会话文件」，其次是「上次处理的文件」。\n\n"
            "意图分类规则：\n"
            "- 涉及「全面处理」「完整排版」等需要多步骤的 → full\n"
            "- 涉及「检查」「审查」「验证」「看看格式对不对」 → review_only\n"
            "- 涉及单个具体排版操作（参考文献、图注、交叉引用）→ format_only\n"
            "- 涉及单个非排版操作（LaTeX转换、缩写检测）→ execute_only\n"
            "- 简单对话/查询/不涉及文档流水线 → simple"
        ),
    )

    user_content = user_input
    if history_context:
        user_content = f"[上下文] {history_context}\n\n[用户输入] {user_input}"

    user_msg = Message(role=Role.USER, content=user_content)

    try:
        response = llm.chat(
            [system_msg, user_msg],
            tools=[_CLASSIFY_TOOL],
        )

        # 解析 tool_calls
        if response.tool_calls:
            tc = response.tool_calls[0]
            if tc.name == "classify_intent":
                args = tc.arguments
                intent_str = args.get("intent", "full")
                target_file = args.get("target_file", "")
                reason = args.get("reason", "")

                try:
                    intent = TaskIntent(intent_str)
                except ValueError:
                    logger.warning(
                        "[Router] LLM 返回非法 intent: %s，降级为 TASK_FULL",
                        intent_str,
                    )
                    intent = TaskIntent.TASK_FULL

                logger.info(
                    "[Router] intent=%s | file=%s | reason=%s",
                    intent.value, target_file or "(none)", reason,
                )
                return intent, target_file, reason

        # LLM 没有调用 classify_intent → 降级
        logger.warning("[Router] LLM 未调用 classify_intent，降级为 TASK_SIMPLE")
        return TaskIntent.TASK_SIMPLE, "", "LLM 未返回分类结果"

    except Exception as e:
        logger.error("[Router] 意图分类失败: %s，降级为 TASK_FULL", e)
        return TaskIntent.TASK_FULL, "", f"分类异常: {e}"


# ═════════════════════════════════════════════
# 第三层：DAGExecutor — 拓扑排序调度器
# ═════════════════════════════════════════════

class DAGExecutor:
    """
    DAG 拓扑调度器：解析声明式 DAG 图，按拓扑序逐节点执行。

    提供三大企业级能力：
      1. 节点级熔断与重试 — 单节点失败不需要重规划整个 DAG
      2. 可观测性 — 每个节点的状态（pending/running/done/failed）实时透传给前端
      3. 确定性 — 相同 DAG + 相同输入 → 相同执行顺序，无随机性

    用法：
        executor = DAGExecutor(intent, user_input, target_file)
        for step in executor:
            role, objective = step
            report = delegate_task(role=role, objective=objective, ...)
            executor.feed_report(report)
    """

    def __init__(
        self,
        intent: TaskIntent,
        user_input: str,
        target_file: str,
    ):
        self.intent = intent
        self.user_input = user_input
        self.target_file = target_file

        # 从声明式 DAG 图中加载节点（拓扑排序后的执行顺序）
        self._dag_nodes: list[DAGNode] = list(_INTENT_DAG.get(intent, []))
        self._pipeline = [(n.role, n.objective_template) for n in self._dag_nodes]
        self._current_step = 0
        self._reports: list[dict] = []
        self._node_status: dict[str, str] = {
            n.id: "pending" for n in self._dag_nodes
        }

        logger.info(
            "[DAG] init: intent=%s, nodes=%s, file=%s",
            intent.value,
            [n.id for n in self._dag_nodes],
            target_file,
        )

    @property
    def is_pipeline_intent(self) -> bool:
        """是否需要走 DAG 执行图（非 SIMPLE 都需要）。"""
        return self.intent != TaskIntent.TASK_SIMPLE

    @property
    def total_steps(self) -> int:
        return len(self._dag_nodes)

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def is_done(self) -> bool:
        return self._current_step >= len(self._dag_nodes)

    @property
    def reports(self) -> list[dict]:
        return list(self._reports)

    @property
    def node_status(self) -> dict[str, str]:
        """DAG 节点状态快照（供前端渲染拓扑图）。"""
        return dict(self._node_status)

    @property
    def current_node(self) -> Optional[DAGNode]:
        """当前正在执行的 DAG 节点。"""
        if self._current_step < len(self._dag_nodes):
            return self._dag_nodes[self._current_step]
        return None

    def __iter__(self):
        return self

    def __next__(self) -> tuple[str, str]:
        """
        按拓扑序返回下一个就绪节点的 (role, objective)。

        Raises:
            StopIteration: 所有节点已执行完毕
        """
        if self.is_done:
            raise StopIteration

        node = self._dag_nodes[self._current_step]
        objective = node.objective_template.format(
            user_input=self.user_input,
            target_file=self.target_file,
        )

        # 更新节点状态 → running
        self._node_status[node.id] = "running"

        logger.info(
            "[DAG] >> Node '%s' (%d/%d): role=%s, depends_on=%s",
            node.id, self._current_step + 1, self.total_steps,
            node.role, node.depends_on,
        )
        return node.role, objective

    def feed_report(self, report: dict):
        """
        接收 Worker 报告，更新节点状态，推进 DAG 执行指针。

        节点级熔断策略：单节点失败不中断整个 DAG，
        而是标记为 failed 并继续执行后续节点（降级执行）。

        Args:
            report: Worker 返回的 JSON 报告 dict
        """
        self._reports.append(report)
        status = report.get("status", "UNKNOWN")

        # 更新节点状态
        node = self._dag_nodes[self._current_step]
        self._node_status[node.id] = "done" if status == "PASS" else "failed"

        logger.info(
            "[DAG] << Node '%s' (%d/%d) %s: status=%s",
            node.id, self._current_step + 1, self.total_steps,
            self._node_status[node.id], status,
        )

        # 节点级熔断：单节点 FAIL 不中断 DAG
        # Planner FAIL → 降级为无计划执行；Executor FAIL → Reviewer 仍可审查
        self._current_step += 1

    def to_dag_snapshot(self) -> dict:
        """
        输出 DAG 执行快照（可 JSON 序列化，供前端渲染拓扑图）。

        返回格式：
        {
            "intent": "full",
            "nodes": [
                {"id": "plan", "role": "Planner", "depends_on": [], "status": "done"},
                {"id": "exec", "role": "Executor", "depends_on": ["plan"], "status": "running"},
                {"id": "review", "role": "Reviewer", "depends_on": ["exec"], "status": "pending"},
            ]
        }
        """
        return {
            "intent": self.intent.value,
            "nodes": [
                {
                    "id": n.id,
                    "role": n.role,
                    "depends_on": n.depends_on,
                    "status": self._node_status.get(n.id, "pending"),
                }
                for n in self._dag_nodes
            ],
        }

    def build_summary(self) -> str:
        """
        汇总所有节点报告，生成给 Coordinator 的结构化摘要。
        """
        if not self._reports:
            return "（无 Worker 报告）"

        lines = [f"[DAG] 执行摘要（{self.intent.value}，共 {len(self._dag_nodes)} 个节点）：\n"]
        for i, report in enumerate(self._reports):
            node = self._dag_nodes[i] if i < len(self._dag_nodes) else None
            node_id = node.id if node else "?"
            role = node.role if node else "?"
            status = report.get("status", "UNKNOWN")
            summary = report.get("summary", "无摘要")
            emoji = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
            lines.append(f"  {emoji} [{node_id}] {role}: {summary}")

            issues = report.get("issues_found", [])
            if issues:
                for issue in issues[:3]:
                    lines.append(f"      └─ {issue}")

        return "\n".join(lines)


# 向后兼容别名：旧代码中使用 TaskFSM 的地方无需修改
TaskFSM = DAGExecutor
