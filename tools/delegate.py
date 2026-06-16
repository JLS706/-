# -*- coding: utf-8 -*-
"""
DelegateTaskTool — Coordinator 的神级工具（蜂群派发器）

当 Coordinator（主 Agent）调用此工具时：
  1. Fork 一个全新的、干净的子 Agent 实例（Worker）
  2. 注入 Worker 角色的 System Prompt + 精简工具箱（无 delegate_task，防套娃）
  3. Worker 在自己完全隔离的 History 窗口中执行任务
  4. Worker 完成后输出结构化 JSON 报告
  5. Worker 自杀销毁，所有中间日志（几千 Token）一起消亡
  6. Coordinator 的 History 里只多了一条清爽的 ToolResult

架构意义：
  - 上下文隔离：Worker 的垃圾日志永远不会污染 Coordinator 的窗口
  - 扁平化：只有 Coordinator 拥有此工具，Worker 绝对无权派发
  - 可水平扩展：未来可改为并行 Fork 多个 Worker
"""

import json
import traceback
import uuid

from core.logger import logger
from sandbox.workspace import WorkspaceProvider, LocalFolderWorkspace
from tools.base import Tool
from tools.submit_report import SubmitReportTool


class DelegateTaskTool(Tool):
    """
    派发子任务给一个无状态 Worker Agent。

    Coordinator 通过 LLM 的 tool_calls 调用此工具，
    指定角色、目标和文件，系统自动 Fork 子 Agent 执行。
    """

    name = "delegate_task"
    description = (
        "将一个子任务派发给独立的 Worker Agent 执行。"
        "Worker 拥有完全隔离的上下文，执行完成后返回结构化 JSON 报告。"
        "只有 Coordinator 有权调用此工具，Worker 不能互相派发（扁平化约束）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": (
                    "Worker 的角色身份，如 Planner, Executor, Reviewer, Preprocessor。"
                    "决定 Worker 的行为模式。"
                ),
            },
            "objective": {
                "type": "string",
                "description": (
                    "具体的任务目标描述。"
                    "例如：'验证第23段到第50段的参考文献格式是否为宋体10.5'"
                ),
            },
            "target_file": {
                "type": "string",
                "description": "要操作的 Word 文档路径",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "（可选）Worker 可使用的工具白名单。"
                    "不指定则使用除 delegate_task 外的全部工具。"
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": "（可选）Worker 最大执行步数，默认 6",
            },
        },
        "required": ["role", "objective", "target_file"],
    }

    def __init__(self, llm, tool_registry, workspace: WorkspaceProvider = None,
                 coordinator_agent=None):
        """
        Args:
            llm: 共享的 LLM 实例（子 Agent 复用，不重新创建连接）
            tool_registry: 主 Agent 的完整工具注册表（子 Agent 会从中派生子集）
            workspace: 工作区供应商（默认 LocalFolderWorkspace）
            coordinator_agent: Coordinator Agent 实例引用（用于透传 _active_config 给 Worker）
        """
        super().__init__()
        self._llm = llm
        self._master_registry = tool_registry
        self._workspace = workspace or LocalFolderWorkspace()
        self._coordinator = coordinator_agent

    def execute(
        self,
        role: str,
        objective: str,
        target_file: str,
        allowed_tools: list = None,
        max_steps: int = 6,
    ) -> str:
        """
        Fork 子 Agent → 执行 → 收割报告 → 销毁。

        整个过程对 Coordinator 来说就是一次普通的工具调用，
        但底层是一个完整的 Agent 生命周期。
        """
        task_id = uuid.uuid4().hex[:12]
        logger.info(
            "[Delegate] 🐝 Fork Worker: role=%s, task=%s, objective=%.60s..., file=%s",
            role, task_id, objective, target_file,
        )

        try:
            # ── 1. 派生工具子集（扁平化：排除 delegate_task 防套娃）──
            if allowed_tools:
                worker_tools = self._master_registry.subset(set(allowed_tools))
            else:
                worker_tools = self._master_registry.exclude({"delegate_task"})

            # 确保 close_word 始终可用（物理层兖底）
            close_word = self._master_registry.get("close_word")
            if close_word and not worker_tools.get("close_word"):
                worker_tools.register(close_word)

            # ── 注入 submit_report 工具（强制结构化报告，绕过文本解析）──
            # 每个 Worker 独享一个实例，执行完后从实例读取报告 dict，
            # 完全避免 LLM 在 JSON 前后附加废话导致的解析失败。
            submit_report_tool = SubmitReportTool()
            worker_tools.register(submit_report_tool)

            # ── 2. 创建隔离工作区（Worker 永远不碰原文件）──
            with self._workspace.session(task_id, target_file) as ctx:
                work_path = ctx.work_path
                logger.info(
                    "[Delegate] 📁 工作区就绪: %s → %s",
                    target_file, work_path,
                )

                # ── 3. 构建 Worker 专属系统提示词（指向工作区副本）──
                from core.prompt import build_worker_prompt
                system_prompt = build_worker_prompt(
                    role=role,
                    objective=objective,
                    target_file=work_path,  # 指向工作区副本，不是原文件
                    tool_descriptions=worker_tools.describe(),
                )

                # ── 4. Fork：创建全新的子 Agent 实例（完全隔离的 History）──
                from core.agent import Agent
                worker = Agent(
                    llm=self._llm,
                    tool_registry=worker_tools,
                    max_steps=max_steps,
                    verbose=True,
                    dry_run=False,
                    memory=None,         # Worker 无记忆（无状态）
                    skill_manager=None,  # Worker 无 Skill（纯执行）
                )

                # 注入 Worker 的系统提示词（替代默认的 Executor Prompt）
                from core.schema import Message, Role
                worker.history.clear()
                worker.history.append(Message(role=Role.SYSTEM, content=system_prompt))

                # ── 关键：透传 Coordinator 的 Skill Config 给 Worker ──
                # Worker 无 SkillManager，但需要 _active_config 来注入工具参数
                # （如 format_rules、ref_format_config 等领域知识）
                if self._coordinator and hasattr(self._coordinator, '_active_config'):
                    worker._active_config = dict(self._coordinator._active_config)
                    if worker._active_config:
                        logger.info(
                            "[Delegate] ⚙️ Skill Config 透传: %s",
                            list(worker._active_config.keys()),
                        )

                # ── 5. Worker 在隔离工作区中执行（异步驱动，心跳中继）──
                worker_input = (
                    f"请执行以下任务：\n"
                    f"角色: {role}\n"
                    f"目标: {objective}\n"
                    f"文件: {work_path}\n\n"
                    f"完成后调用 submit_report 工具提交结构化报告，不要在文本中输出 JSON。"
                )

                # 用 asyncio.run() 在本线程起新事件循环，
                # 消费 Worker 的 StreamEvent，把 tool_progress 冒泡给 Coordinator
                import asyncio as _asyncio

                async def _drive_worker():
                    """
                    驱动 Worker 并中继心跳事件。

                    防御加固：顶层 try/except 捕获所有内层异常
                    （含 Worker 看门狗触发的 CancelledError / COM 异常），
                    防止异常击穿嵌套事件循环导致 Coordinator 静默崩溃。
                    """
                    final_text_parts = []
                    # 让 Coordinator 看门狗立刻知道 Worker 已启动
                    self.report_progress(5, f"[Worker:{role}] 启动")
                    try:
                        async for ev in worker.run_async(worker_input):
                            if ev.type == "tool_progress":
                                # Worker 子工具的心跳 → 冒泡给 Coordinator
                                pct = ev.metadata.get("percent", 50)
                                tool_name = ev.metadata.get("tool", "?")
                                # 钳位到 [5, 95]，避免干扰 Coordinator 自己的 0/100
                                relay_pct = max(5, min(95, pct))
                                self.report_progress(
                                    relay_pct,
                                    f"[Worker:{role}:{tool_name}] {ev.content}",
                                )
                            elif ev.type == "tool_start":
                                self.report_progress(
                                    10, f"[Worker:{role}] {ev.content}"
                                )
                            elif ev.type == "tool_end":
                                self.report_progress(
                                    80, f"[Worker:{role}] {ev.content}"
                                )
                            elif ev.type == "tool_timeout":
                                stall = ev.metadata.get("stall_seconds", "?")
                                self.report_progress(
                                    85,
                                    f"[Worker:{role}] 子工具熔断({stall}s): {ev.content}",
                                )
                            elif ev.type == "text":
                                final_text_parts.append(ev.content)
                            elif ev.type == "error":
                                logger.warning("[Delegate] Worker 错误事件: %s", ev.content)
                            # finish 事件不转发
                    except _asyncio.CancelledError:
                        logger.warning("[Delegate] Worker 内层事件循环被取消")
                        final_text_parts.append(
                            "\n[Worker 被取消（内层看门狗或外层 Coordinator 触发）]"
                        )
                    except Exception as inner_exc:
                        logger.error(
                            "[Delegate] Worker 内层异常: %s\n%s",
                            inner_exc, traceback.format_exc(),
                        )
                        final_text_parts.append(
                            f"\n[Worker 内层异常: {inner_exc}]"
                        )
                    return "".join(final_text_parts)

                # 👑 显式创建新事件循环（不用 asyncio.run）
                # asyncio.run() 在检测到当前线程已有活跃 loop 时会直接 RuntimeError；
                # 本方法被 run_async() 通过 asyncio.to_thread() 调度到工作线程中执行，
                # 该线程通常不应有 loop，但 "通常" 不等于 "必然"——依赖这个隐式不变量
                # 风险高。显式 new_event_loop + set_event_loop + run_until_complete
                # 能在本线程安全建立独立 loop，与外层主循环完全隔离。
                _worker_loop = _asyncio.new_event_loop()
                try:
                    _asyncio.set_event_loop(_worker_loop)
                    raw_result = _worker_loop.run_until_complete(_drive_worker())
                finally:
                    try:
                        # 清理未完成的异步生成器，避免 ResourceWarning
                        _worker_loop.run_until_complete(
                            _worker_loop.shutdown_asyncgens()
                        )
                    except Exception:
                        pass
                    _asyncio.set_event_loop(None)
                    _worker_loop.close()
                self.report_progress(95, f"[Worker:{role}] 输出报告中...")

                # ── 6. 提取报告，决定是否回写原文件 ──
                # 优先：Worker 通过 submit_report 工具提交的结构化报告（零解析风险）
                # 回退：从 Worker 文本输出中正则提取 JSON（旧方案，兜底）
                if submit_report_tool.was_called():
                    report_dict = submit_report_tool.get_report()
                    report = json.dumps(report_dict, ensure_ascii=False, indent=2)
                    logger.info(
                        "[Delegate] 📋 使用 submit_report 工具报告 (status=%s)",
                        report_dict.get("status"),
                    )
                else:
                    # ── 强制重试：用 tool_choice 逼 LLM 调用 submit_report ──
                    # 比直接降级到文本解析可靠得多：LLM 通过 tool_calls 返回
                    # 结构化参数，框架层直接拿到 dict，零解析风险。
                    logger.warning(
                        "[Delegate] ⚠️ Worker 未调用 submit_report，强制重试..."
                    )
                    self.report_progress(96, f"[Worker:{role}] 强制提交报告...")

                    forced_ok = False
                    try:
                        from core.schema import Message as _Msg, Role as _Role
                        retry_messages = [
                            _Msg(role=_Role.SYSTEM, content=(
                                "你是报告提交助手。根据 Worker 的执行结果，"
                                "调用 submit_report 工具提交结构化报告。"
                            )),
                            _Msg(role=_Role.USER, content=(
                                f"Worker 角色: {role}\n"
                                f"Worker 目标: {objective}\n"
                                f"Worker 输出:\n{raw_result[:2000]}\n\n"
                                "请根据上述输出，调用 submit_report 工具提交报告。"
                            )),
                        ]
                        # 只暴露 submit_report 一个工具 + 强制调用
                        retry_response = self._llm.chat(
                            retry_messages,
                            tools=[submit_report_tool.to_openai_tool()],
                            tool_choice={
                                "type": "function",
                                "function": {"name": "submit_report"},
                            },
                        )
                        if retry_response.tool_calls:
                            tc = retry_response.tool_calls[0]
                            if tc.name == "submit_report":
                                submit_report_tool.execute(**tc.arguments)
                                forced_ok = True
                                logger.info(
                                    "[Delegate] ✅ 强制重试成功，submit_report 已调用"
                                )
                    except Exception as e:
                        logger.warning("[Delegate] 强制重试失败: %s", e)

                    if forced_ok and submit_report_tool.was_called():
                        report_dict = submit_report_tool.get_report()
                        report = json.dumps(report_dict, ensure_ascii=False, indent=2)
                        logger.info(
                            "[Delegate] 📋 使用强制重试报告 (status=%s)",
                            report_dict.get("status"),
                        )
                    else:
                        # 最后兜底：文本解析（极端情况）
                        logger.warning(
                            "[Delegate] ⚠️ 强制重试也失败，降级到文本解析"
                        )
                        report = self._extract_report(raw_result, role, objective)
                        report_dict = json.loads(report)

                if report_dict.get("status") == "PASS":
                    # 双重保险：优先用 Worker 报告的 output_path，回退到 work_path
                    reported_out = report_dict.get("output_path") or ""
                    ctx.commit(output_path=reported_out if reported_out else None)
                    logger.info(
                        "[Delegate] ✅ Worker PASS，已回写（output_path=%s）",
                        reported_out or "(默认 work_path)",
                    )
                else:
                    logger.info("[Delegate] ⚠️ Worker 未 PASS，丢弃工作区修改")

                # ── 7. Worker 自杀销毁 ──
                worker_history_len = len(worker.history)
                del worker

            # with 退出时工作区自动核平销毁

            logger.info(
                "[Delegate] ✅ Worker 完成并销毁 (消耗 %d 条历史，"
                "Coordinator 只收到 1 条报告)",
                worker_history_len,
            )

            return report

        except Exception as e:
            error_report = json.dumps({
                "status": "FAIL",
                "summary": f"Worker 执行崩溃: {e}",
                "issues_found": [traceback.format_exc()[-500:]],
                "actions_taken": [],
            }, ensure_ascii=False, indent=2)
            logger.error("[Delegate] ❌ Worker 崩溃: %s", e)
            return error_report

    @staticmethod
    def _extract_report(raw_result: str, role: str, objective: str) -> str:
        """
        从 Worker 的原始输出中提取 JSON 报告。

        Worker 被要求输出 JSON，但 LLM 可能在前后加了废话。
        策略：从尾部向前扫描，尝试找到包含 "status" 字段的最后一个
        完整 JSON 对象。这比贪心正则更鲁棒——贪心 `{[\\s\\S]*}` 会
        从第一个 `{` 匹配到最后一个 `}`，当 Worker 输出中包含多个
        JSON 片段（如工具中间日志 + 最终报告）时必然失败。
        """
        # ── 策略 1: 从尾部向前查找包含 "status" 的 JSON 块 ──
        # 先用非贪心正则提取所有独立的 {...} 块，然后从后往前尝试解析
        import re
        # 非贪心匹配 + 允许嵌套（通过匹配平衡括号的方式）
        # 回退方案：逐个尝试从每个 `{` 开始的子串
        candidates = []
        depth = 0
        start = -1
        for i, ch in enumerate(raw_result):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(raw_result[start:i + 1])
                    start = -1

        # 从后往前尝试解析（最后一个 JSON 块最可能是最终报告）
        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "status" in parsed:
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, ValueError):
                continue

        # ── 策略 2: 兜底 — 尝试非贪心正则（处理简单情况）──
        json_match = re.search(r'\{[^{}]*"status"\s*:\s*"[^"]*"[^{}]*\}', raw_result)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if "status" in parsed:
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, ValueError):
                pass

        # JSON 提取失败 → 降级为文本报告
        return json.dumps({
            "status": "UNKNOWN",
            "summary": f"Worker({role}) 未返回标准 JSON 报告",
            "raw_output": raw_result[:1000],
            "issues_found": [],
            "actions_taken": [],
        }, ensure_ascii=False, indent=2)
