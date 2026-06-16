# -*- coding: utf-8 -*-
"""
DocMaster Agent - ReAct Agent 核心
实现 ReAct (Reasoning + Acting) 循环：
  Think → Act → Observe → Think → ... → Finish
"""

import asyncio
import json
import os
import queue as _thread_queue
import re
import time
import traceback
import warnings
from typing import AsyncGenerator

from core.llm import LLM
from core.logger import logger
from core.schema import AgentState, Message, Role, StreamEvent, ToolCall, ToolResult
from tools.base import ToolRegistry

# Word 相关工具名集合（用于 L1 后校验：自动检查 close_word 是否被遗漏）
WORD_TOOLS = {
    "read_document", "inspect_document_format", "format_references",
    "ref_crossref", "fig_crossref", "fig_caption", "acronym_checker",
    "summarize_document", "analyze_document", "index_document",
}

FSM_INTERACTION_TIMEOUT = 600.0


class Agent:
    """
    ReAct Agent —— 核心调度引擎。

    工作流程：
    1. 接收用户指令
    2. 将对话历史 + 工具列表发给 LLM
    3. 若 LLM 返回 tool_calls → 执行工具 → 结果加入对话历史 → 回到步骤 2
    4. 若 LLM 返回纯文本 → 作为最终回答返回给用户
    """

    def __init__(
        self,
        llm: LLM,
        tool_registry: ToolRegistry,
        max_steps: int = 10,
        verbose: bool = True,
        dry_run: bool = False,
        memory=None,
        skill_manager=None,
        api_mode: bool = False,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.max_steps = max_steps
        self.verbose = verbose
        self.dry_run = dry_run
        self.memory = memory
        self.skill_manager = skill_manager
        self.api_mode = api_mode  # API/插件模式：禁止自动关闭 Word
        self.state = AgentState.IDLE
        self.history: list[Message] = []
        self._session_tools: list[str] = []   # 本轮执行过的工具名
        self._session_file: str = ""           # 本轮操作的文件路径
        self._session_literature_folder: str = ""  # 本轮参考文献文件夹路径
        self._retry_counts: dict[str, int] = {}  # 工具重试计数器
        self._active_config: dict = {}           # 本轮 Skill config（会话级）
        self._token_warning = 6000   # Token 水位：触发规则压缩（4000→6000，避免过早压缩稀释 L1）
        self._token_critical = 8000  # Token 水位：触发 LLM 深度压缩

        # FSM Interactive State (Human-in-the-loop)
        import asyncio
        self._approval_event: asyncio.Event | None = None
        self._review_event: asyncio.Event | None = None
        self._user_choice: str | None = None

        # 构建基础系统提示词（无技能上下文，启动时用）
        self._build_system_prompt()

    def resume_fsm(self, action: str):
        """前端通过此方法通知后台用户操作，并恢复挂起的 FSM 任务"""
        logger.info(f"[Agent] FSM resume triggered with action: {action}")
        self._user_choice = action
        if self._approval_event:
            self._approval_event.set()
        if self._review_event:
            self._review_event.set()

    def _build_system_prompt(self, skills_context: str = "",
                             recalled_context: str = ""):
        """
        构建系统提示词（静态根 + 动态叶分离，Prompt Cache 友好）。

        消息布局：
          Message[0]: 静态 System（L1 规则 + 工具 + 行为准则）→ 跨轮次不变，可被缓存
          Message[1]: 动态 System（技能 + 记忆）→ 每轮变化，不影响 [0] 的缓存命中
          Message[2..]: User / Assistant / Tool 对话历史

        💡 设计原理：
          大模型 API 的 Prompt Cache 匹配 Token 序列的最长公共前缀。
          只要 Message[0] 内容不变，前缀就能命中缓存，获得 50%~90% 的降价。
        """
        from core.prompt import build_static_system_prompt, build_dynamic_context

        tool_desc = self.tools.describe()
        memory_context = (
            self.memory.get_context_summary(
                recalled_context,
                current_file=self._session_file,
            )
            if self.memory else ""
        )

        # ── 自动探测人设：有 delegate_task 工具 → Coordinator，否则 Executor ──
        # 主 Agent 的 registry 注册了 delegate_task → 自动成为 Coordinator
        # Worker 的 registry 经 exclude({"delegate_task"}) 过滤 → 自动成为 Executor
        is_coordinator = self.tools.get("delegate_task") is not None

        # ── 静态根（跨轮次不变 → 缓存命中）──
        static_content = build_static_system_prompt(
            tool_desc, is_coordinator=is_coordinator
        )
        static_msg = Message(role=Role.SYSTEM, content=static_content)

        # ── 动态叶（每轮可变 → 独立消息，不污染前缀）──
        dynamic_content = build_dynamic_context(skills_context, memory_context)

        # 清除旧的 system 消息（可能有 1~2 条）
        while self.history and self.history[0].role == Role.SYSTEM:
            self.history.pop(0)

        # 按顺序插入：[0] 静态根, [1] 动态叶（如果有）
        if dynamic_content.strip():
            dynamic_msg = Message(role=Role.SYSTEM, content=dynamic_content)
            self.history.insert(0, dynamic_msg)
        self.history.insert(0, static_msg)

    def run(self, user_input: str) -> str:
        """
        执行一次完整的 Agent 循环。

        .. deprecated::
            同步阻塞接口，仅保留向后兼容。
            新代码请使用 ``async for event in agent.run_async(user_input)``。

        Args:
            user_input: 用户的自然语言指令

        Returns:
            Agent 的最终回答文本
        """
        warnings.warn(
            "Agent.run() 是同步阻塞接口，已废弃。"
            "请迁移到 run_async() 以获得流式进度和可中断能力。",
            DeprecationWarning,
            stacklevel=2,
        )
        # ── 会话级重置：防止跨轮次状态累积 ──
        self._session_tools = []
        self._session_file = (self.memory.get_last_file() if self.memory else "") or ""
        self._retry_counts = {}
        self._active_config = {}

        try:
            return self._run_impl(user_input)
        finally:
            # 👑 确定性清理：无论正常/异常/超步数退出，都兜底关闭 Word 进程
            # （带线程超时保护，防止 close_word 卡死级联冻住主流程）
            self._close_word_safely()

    def _run_impl(self, user_input: str) -> str:
        """run() 的实际业务逻辑（被 try/finally 包裹以保证 Word 兜底关闭）。"""
        # ── 向量记忆召回（RAG 式）──
        recalled = ""
        if self.memory:
            recalled = self.memory.recall_relevant(
                user_input,
                current_file=self._session_file,
            )
            if self.verbose and recalled:
                logger.info("📌 已召回 %d 条相关历史", recalled.count('相关度'))

        # 根据用户输入匹配 Skills，动态重建系统提示词 + 提取 Config
        skills_ctx = ""
        if self.skill_manager:
            matched = self.skill_manager.match(user_input)
            skills_ctx = self.skill_manager.build_skills_context(matched)
            # 合并匹配到的 Skill 的 config 块（高优先级覆盖低优先级）
            self._active_config = self.skill_manager.get_active_config(matched)
            if self.verbose and matched:
                names = [s.name for s in matched]
                logger.info("📚 已加载技能: %s", ', '.join(names))
                if self._active_config:
                    logger.info("⚙️ 已注入 Skill Config: %s",
                                list(self._active_config.keys()))

        # 重建系统提示词（含召回的历史 + 技能）
        self._build_system_prompt(skills_ctx, recalled)

        # ── 策略一：三明治注入 ──
        # 将 L1 规则追加到用户消息末尾（紧贴 LLM 生成起始点）
        # 利用 LLM 近因效应（Recency Bias）最大化规则服从度
        from core.prompt import build_l1_user_suffix
        l1_suffix = build_l1_user_suffix(user_input)
        augmented_input = user_input + l1_suffix if l1_suffix else user_input

        # 添加用户消息（含 L1 后缀）
        self.history.append(Message(role=Role.USER, content=augmented_input))
        self.state = AgentState.THINKING

        if self.verbose:
            logger.info("\n" + "=" * 60)
            logger.info("🧠 Agent 收到指令: %s", user_input)
            logger.info("=" * 60)

        # ── Coordinator 路由分支（同步版）──
        is_coordinator = self.tools.get("delegate_task") is not None
        if is_coordinator:
            try:
                from core.router import classify_intent, TaskIntent, TaskFSM

                intent, router_file, reason = classify_intent(
                    self.llm, user_input,
                    history_context=self._build_router_context(user_input),
                )
                logger.info("🚦 意图: %s — %s", intent.value, reason)

                if router_file:
                    self._session_file = router_file
                    self._try_detect_literature_folder()

                if intent != TaskIntent.TASK_SIMPLE and self._session_file:
                    return self._run_fsm_pipeline_sync(
                        TaskFSM(intent, user_input, self._session_file),
                        user_input,
                    )
                elif intent != TaskIntent.TASK_SIMPLE and not self._session_file:
                    logger.warning("[Router] 需要文件但未指定，降级为 ReAct")
            except Exception as e:
                logger.warning("[Router] 路由异常，降级为 ReAct: %s", e)

        openai_tools = self.tools.to_openai_tools()

        for step in range(1, self.max_steps + 1):
            if self.verbose:
                logger.info("\n--- 第 %d/%d 步 ---", step, self.max_steps)

            # ── Token 水位线压缩：防止上下文爆炸 ──
            if self._estimate_tokens() > self._token_warning:
                self._compress_history()

            # 调用 LLM
            self.state = AgentState.THINKING
            try:
                response = self.llm.chat(self.history, tools=openai_tools)
            except Exception as e:
                error_msg = f"❌ LLM 调用失败: {e}"
                if self.verbose:
                    logger.error(error_msg)
                    traceback.print_exc()
                self.state = AgentState.ERROR
                return error_msg

            # 将 LLM 回复加入历史
            self.history.append(response)

            # 情况1: LLM 返回纯文本（没有工具调用） → 任务完成
            if not response.tool_calls:
                self.state = AgentState.FINISHED
                final_answer = response.content or "(Agent 没有给出回答)"

                # ── L1 规则后校验：自动检测并修正违规行为 ──
                l1_check = self._post_validate_l1()
                if l1_check:
                    final_answer += f"\n\n{l1_check}"
                    if self.verbose:
                        logger.info("🛡️ L1 后校验触发: %s", l1_check)

                if self.verbose:
                    logger.info("\n✅ Agent 回答:\n%s", final_answer)
                # 自动保存本轮操作到记忆（含向量存储）
                self._save_session(user_input, final_answer)
                return final_answer

            # 情况2: LLM 请求调用工具 → 逐一执行
            self.state = AgentState.ACTING
            for tc in response.tool_calls:
                result = self._execute_tool(tc.id, tc.name, tc.arguments)

                # 将工具结果加入对话历史
                tool_msg = Message(
                    role=Role.TOOL,
                    content=result.output,
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                )
                self.history.append(tool_msg)

        # 超过最大步数
        self.state = AgentState.ERROR
        timeout_msg = f"⚠️ Agent 已达到最大步数 ({self.max_steps})，强制停止。"
        if self.verbose:
            logger.warning(timeout_msg)
        return timeout_msg

    # ─────────────────────────────────────────────
    # Hook 2: Token 水位线驱动的上下文压缩
    # ─────────────────────────────────────────────

    def _estimate_tokens(self) -> int:
        """
        估算当前 history 的 Token 数（中英文混合近似）。

        规则: len(text) // 2 对中英混合文本误差 ±20%。
        不引入 tiktoken 依赖，保持零额外依赖。
        """
        total = 0
        for msg in self.history:
            text = msg.text_content
            total += len(text) // 2
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += len(str(tc.arguments)) // 2
        return total

    def _compress_history(self):
        """
        Token 水位线驱动的两级压缩。

        Tier 1 (token < _token_critical): 纯规则截取（零 LLM 成本）
        Tier 2 (token ≥ _token_critical): 调 LLM 做智能摘要

        压缩前: [System, User, LLM₁, Tool₁, ..., LLM₇, Tool₇]
        压缩后: [System, User, Scratchpad, LLM₆, Tool₆, LLM₇, Tool₇]
        """
        tokens = self._estimate_tokens()

        if tokens < self._token_warning:
            return

        # 动态计算 keep_head：保留开头所有连续的 system 消息 + 第一条 user 消息
        keep_head = 0
        for m in self.history:
            if m.role == Role.SYSTEM:
                keep_head += 1
            else:
                break
        # 确保第一条 user 消息也被保留
        if keep_head < len(self.history) and self.history[keep_head].role == Role.USER:
            keep_head += 1
        keep_tail = 4  # 最近 2 轮工具交互

        if len(self.history) <= keep_head + keep_tail:
            return

        middle = self.history[keep_head:-keep_tail]
        if not middle:
            return

        if tokens < self._token_critical:
            # ── Tier 1: 规则压缩（零成本）──
            tool_summaries = []
            for msg in middle:
                if msg.role == Role.TOOL:
                    first_line = msg.text_content.split("\n")[0][:80]
                    tool_name = msg.name or "unknown"
                    tool_summaries.append(f"{tool_name}: {first_line}")

            scratchpad_text = (
                "[已完成步骤] " + "; ".join(tool_summaries)
                if tool_summaries
                else "[已完成若干步骤]"
            )
        else:
            # ── Tier 2: LLM 深度压缩 ──
            middle_text = "\n".join(
                f"[{msg.role.value}] {msg.text_content[:200]}"
                for msg in middle
            )
            try:
                compress_msgs = [
                    Message(
                        role=Role.SYSTEM,
                        content=(
                            "将以下 Agent 执行过程压缩为 3 句话，"
                            "保留关键操作和结论，去除冗余细节。"
                        ),
                    ),
                    Message(
                        role=Role.USER,
                        content=middle_text[:2000],
                    ),
                ]
                resp = self.llm.chat(compress_msgs)
                scratchpad_text = (
                    f"[压缩摘要] {resp.content or '(压缩失败)'}"
                )
                if self.verbose:
                    logger.debug(
                        "  📋 Tier2 LLM 深度压缩: %d Token → ~%d Token",
                        tokens,
                        self._estimate_tokens(),
                    )
            except Exception:
                # LLM 失败时回退到规则压缩
                scratchpad_text = "[已完成若干步骤，因上下文过长已压缩]"

        scratchpad_msg = Message(
            role=Role.ASSISTANT,
            content=scratchpad_text,
        )

        old_len = len(self.history)
        self.history = (
            self.history[:keep_head]
            + [scratchpad_msg]
            + self.history[-keep_tail:]
        )

        if self.verbose:
            logger.debug(
                "  📋 Token 水位压缩: %d 条消息 → %d 条 (≈%d tokens)",
                old_len, len(self.history), self._estimate_tokens(),
            )

    # ─────────────────────────────────────────────
    # 错误分类器（三级分类）
    # ─────────────────────────────────────────────

    def _classify_error(
        self, name: str, error: Exception
    ) -> tuple[str, str, list[str]]:
        """
        对工具执行错误进行三级分类，并生成修正建议。

        Returns:
            (error_level, error_summary, suggestions)
            error_level: "transient" | "correctable" | "fatal"
        """
        error_str = str(error)
        error_type = type(error).__name__

        # ── Level 1: 可自动重试的临时性错误 ──
        transient_patterns = [
            "超时", "timeout", "Timeout",
            "RPC", "网络", "connection", "Connection",
            "-2147",            # COM HRESULT 错误码
            "繁忙", "busy",
            "429", "rate limit",  # API 限流
        ]
        if any(p in error_str for p in transient_patterns):
            return (
                "transient",
                f"临时性错误 ({error_type}): {error_str[:100]}",
                ["系统将自动重试，无需干预"],
            )

        # ── Level 2: LLM 可自修正的错误 ──

        # 2a. 文件路径问题
        if isinstance(error, (FileNotFoundError, OSError)) or re.search(
            r"不存在|找不到|No such file|无法打开", error_str
        ):
            return (
                "correctable",
                f"文件路径错误 ({error_type})",
                [
                    "检查文件路径拼写是否正确（注意中文路径和空格）",
                    "调用 recall_history 查看上次处理过的文件路径",
                    "向用户询问正确的文件路径",
                ],
            )

        # 2b. 参数类型/缺失问题
        if isinstance(error, (TypeError, KeyError)) or "参数" in error_str:
            return (
                "correctable",
                f"工具参数错误 ({error_type})",
                [
                    "检查参数名称和类型是否与工具定义一致",
                    "确认所有 required 参数均已提供",
                    "布尔参数请使用 true/false 而非字符串",
                ],
            )

        # 2c. Word COM 接口问题
        if re.search(r"COM|com_error|Word|word|pywintypes", error_str):
            return (
                "correctable",
                f"Word COM 接口错误 ({error_type})",
                [
                    "确认 Microsoft Word 是否已启动（工具需要 Word 进程）",
                    "检查目标文档是否已被其他程序打开或处于只读状态",
                    "可尝试先调用 read_document 验证文档是否可访问",
                ],
            )

        # 2d. 权限问题
        if isinstance(error, PermissionError) or "权限" in error_str:
            return (
                "correctable",
                f"权限不足 ({error_type})",
                [
                    "文件可能被其他程序占用，请关闭后重试",
                    "检查文件是否为只读属性",
                    "向用户说明需要关闭占用文件的程序",
                ],
            )

        # ── Level 3: 不可恢复的错误 ──
        return (
            "fatal",
            f"不可恢复的错误 ({error_type})",
            [
                "此错误可能是工具内部问题，无法通过修改参数解决",
                "请向用户说明具体错误信息，建议手动处理",
            ],
        )

    # ─────────────────────────────────────────────
    # 结构化 Observation 构造
    # ─────────────────────────────────────────────

    def _build_error_observation(
        self,
        name: str,
        arguments: dict,
        error: Exception,
        attempt: int,
        max_attempts: int,
    ) -> str:
        """
        构造引导 LLM 自修正的结构化 Observation。

        不同于直接传递 traceback，此方法提供：
        1. 简明的错误摘要（减少 token 浪费）
        2. 错误分类（让 LLM 知道这是什么性质的问题）
        3. 具体修正建议（引导下一步推理方向）
        4. 重试计数（防止无限循环）
        """
        level, summary, suggestions = self._classify_error(name, error)

        # 截取错误信息的关键部分（去除冗长 traceback）
        error_detail = str(error)[:200]

        parts = [
            f"⚠️ 工具 {name} 执行失败",
            f"错误分类: {summary}",
            f"错误详情: {error_detail}",
            f"调用参数: {', '.join(f'{k}={v!r}' for k, v in arguments.items())}",
            "",
            "修正建议:",
        ]
        for i, s in enumerate(suggestions, 1):
            parts.append(f"  {i}. {s}")

        parts.append(f"")
        if attempt >= max_attempts:
            parts.append(
                f"⛔ 已对此工具尝试 {attempt}/{max_attempts} 次，"
                f"请换一种策略或向用户说明情况，不要再重复相同的调用。"
            )
        else:
            parts.append(
                f"📌 当前尝试 {attempt}/{max_attempts} 次。"
                f"请先分析失败原因，修正参数后重新调用。"
            )

        return "\n".join(parts)

    def _build_timeout_observation(
        self,
        name: str,
        arguments: dict,
        stall_sec: float,
        stall_timeout: float,
        killed_pids: list[int],
    ) -> str:
        """
        为看门狗熔断/超时构造结构化自修正引导。
        """
        import os
        suggestions = [
            "可能是由于 Microsoft Word 弹出了模态对话框（如“另存为”、“只读提示”等）导致 COM 接口阻塞。",
            "如果是第一次运行，启动 Word 进程可能比较缓慢，可以尝试重新调用以复用已拉起的进程。",
            "检查文件是否已被其他 Word 窗口打开，若是，请手动关闭所有 Word 窗口后重新尝试。"
        ]

        file_path = arguments.get("file_path", "")
        if file_path:
            abs_path = os.path.abspath(file_path)
            suggestions.append(f"检查该文件路径是否正确: {abs_path}")
            if not os.path.exists(abs_path):
                suggestions.append("⚠️ 诊断结果: 该文件实际上不存在！请使用正确且存在的文件路径。")
            else:
                try:
                    with open(abs_path, "a") as f:
                        pass
                except PermissionError:
                    suggestions.append("⚠️ 诊断结果: 该文件目前正被其他程序独占（Permission Denied）。请关闭打开了该文档的所有 Word 实例或其它程序，然后再重试。")
                except Exception:
                    pass

        parts = [
            f"❌ 工具 {name} 执行进度卡死超过 {stall_timeout:.1f} 秒，已被看门狗强杀。",
            f"错误分类: 工具超时/熔断 (Watchdog Timeout)",
            f"错误详情: 连续 {stall_sec:.1f} 秒无心跳，底层 Word 进程已强制熔断（击杀 PID: {killed_pids or '未知'}）",
            f"调用参数: {', '.join(f'{k}={v!r}' for k, v in arguments.items())}",
            "",
            "修正建议:",
        ]
        for i, s in enumerate(suggestions, 1):
            parts.append(f"  {i}. {s}")

        return "\n".join(parts)

    # ─────────────────────────────────────────────
    # 增强版工具执行（含重试跟踪 + 自动重试）
    # ─────────────────────────────────────────────

    def _execute_tool(self, call_id: str, name: str, arguments: dict) -> ToolResult:
        """执行单个工具调用（支持错误分类 + 结构化恢复引导）"""
        tool = self.tools.get(name)
        if tool is None:
            available = ", ".join(t.name for t in self.tools.get_all_tools())
            error = (
                f"⚠️ 未找到工具 '{name}'。\n"
                f"可用工具: {available}\n"
                f"请检查工具名称拼写后重新调用。"
            )
            if self.verbose:
                logger.error("  ❌ %s", error)
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                output=error,
                success=False,
            )

        if self.verbose:
            args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
            logger.info("  🔧 调用工具: %s(%s)", name, args_str)

        # Dry-run 模式：不实际执行
        if self.dry_run:
            output = f"[DRY-RUN] 将调用 {name}，参数: {arguments}"
            if self.verbose:
                logger.info("  🏜️ %s", output)
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                output=output,
                success=True,
            )

        # 重试跟踪：基于工具名计数（同名工具连续失败才计数）
        max_attempts = 3
        attempt = self._retry_counts.get(name, 0)

        # ── Skill Config 注入：将 config 中该工具对应的参数作为默认值注入 ──
        # LLM 显式传递的参数优先级更高，不会被 config 覆盖
        arguments = self._inject_skill_config(tool, arguments)

        # 实际执行
        try:
            output = tool.execute(**arguments)
            if self.verbose:
                # 截断过长输出
                display = output[:300] + "..." if len(output) > 300 else output
                logger.info("  ✅ 结果: %s", display)

            # 执行成功 → 重置该工具的重试计数
            self._retry_counts[name] = 0

            # 记录工具执行（供记忆系统使用）
            self._session_tools.append(name)
            if "file_path" in arguments and not self._session_file:
                self._session_file = arguments["file_path"]
                # 自动检测参考文献文件夹（基于文档路径约定）
                self._try_detect_literature_folder()
            if "literature_folder" in arguments and arguments["literature_folder"]:
                self._session_literature_folder = arguments["literature_folder"]
                self._inject_literature_folder()

            # ── L2 效用反馈：工具成功 → 奖励召回的 L2 记忆 ──
            if self.memory:
                self.memory.reward_recalled_memories(delta=0.1)

            return ToolResult(
                tool_call_id=call_id,
                name=name,
                output=output,
                success=True,
            )
        except Exception as e:
            level, summary, _ = self._classify_error(name, e)

            current_attempt = attempt + 1

            # Level 1 (临时性错误): 自动重试，不消耗 LLM 推理步骤
            if level == "transient" and current_attempt < max_attempts:
                self._retry_counts[name] = current_attempt
                wait_sec = 2 ** attempt  # 指数退避: 1s, 2s
                if self.verbose:
                    logger.warning(
                        "  ⏳ 临时性错误，%d秒后自动重试 (%d/%d)...",
                        wait_sec, current_attempt, max_attempts,
                    )
                # 👑 修复：在 sleep 期间定期发送 progress 心跳，防止触发 watchdog
                for i in range(wait_sec):
                    time.sleep(1.0)
                    tool.report_progress(0, f"临时性错误，自动重试中（等待第 {i+1}/{wait_sec} 秒）...")
                return self._execute_tool(call_id, name, arguments)

            # Level 2 & 3: 构造结构化 Observation 引导 LLM 自修正
            # 工具失败 → 递增重试计数
            self._retry_counts[name] = min(current_attempt, max_attempts)
            error_obs = self._build_error_observation(
                name, arguments, e, min(current_attempt, max_attempts), max_attempts
            )
            if self.verbose:
                logger.error("  ❌ %s", summary)

            # ── L2 效用反馈：工具失败 → 轻度惩罚召回的 L2 记忆 ──
            if self.memory:
                self.memory.penalize_recalled_memories(delta=0.2)

            return ToolResult(
                tool_call_id=call_id,
                name=name,
                output=error_obs,
                success=False,
            )

    # ─────────────────────────────────────────────
    # L1 核心规则后校验器
    # ─────────────────────────────────────────────

    def _post_validate_l1(self) -> str:
        """
        L1 规则后校验：在 Agent 生成最终回答后，检查是否违反了核心规则。

        与 prompt 注入不同，这是代码级别的硬保障：
        - 不依赖 LLM 是否"注意到"了规则
        - 直接检查 _session_tools 里的实际行为
        - 检测到违规时自动执行修正动作

        Returns:
            违规报告文本，如果没有违规则返回空字符串。

        注意：Word 进程关闭已迁移到 run()/run_async() 的 finally 块，
        作为确定性流程处理（带超时保护，不依赖 learned_rules 记忆）。
        当前无活跃校验规则，保留方法签名供未来扩展。
        """
        return ""

    # ─────────────────────────────────────────────
    # 确定性清理：Word 进程兜底关闭（带超时保护）
    # ─────────────────────────────────────────────

    def _needs_close_word(self) -> bool:
        """是否需要兜底关闭 Word 进程。API/插件模式下永远不关闭。"""
        if self.api_mode:
            return False
        used = set(self._session_tools)
        return bool(used & WORD_TOOLS) and "close_word" not in used

    def _close_word_safely(self, timeout: float = 5.0) -> None:
        """
        同步路径的 Word 兜底关闭（用独立线程 + join 超时，防止级联卡死）。

        为什么不直接 tool.execute()：
          - 若 Word 被隐藏对话框卡住，close_word 可能阻塞数十秒，
            整个同步主流程（包括调用方 UI）会一起冻住。
          - 用 daemon 线程 + timeout 做硬隔离：超时后放弃等待，
            线程随进程退出，确保主流程及时返回。
        """
        if not self._needs_close_word():
            return
        tool = self.tools.get("close_word")
        if not tool:
            return

        import threading
        done = threading.Event()
        err: list = []

        def _worker():
            try:
                tool.execute()
            except Exception as e:
                err.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        if done.wait(timeout):
            if err:
                logger.warning("  ⚠️ [finally] close_word 执行失败: %s", err[0])
            else:
                self._session_tools.append("close_word")
                if self.verbose:
                    logger.info("  🧹 [finally] 已自动关闭 Word 进程")
        else:
            logger.warning(
                "  ⏰ [finally] close_word 超过 %.1fs 未响应，放弃等待（daemon 线程随进程退出）",
                timeout,
            )

    async def _close_word_safely_async(self, timeout: float = 5.0) -> None:
        """
        异步路径的 Word 兜底关闭（to_thread + wait_for 硬超时）。

        架构纯洁性要求：同步 COM 接口绝不在主线程直接调用。
        用 asyncio.to_thread 把 close_word 丢进线程池，wait_for 在主事件循环
        做超时守护——即使 close_word 因 Word 卡死僵死，也不会冻结 LLM 流式输出、
        WebSocket 推送等其他协程。
        """
        if not self._needs_close_word():
            return
        tool = self.tools.get("close_word")
        if not tool:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(tool.execute), timeout=timeout)
            self._session_tools.append("close_word")
            if self.verbose:
                logger.info("  🧹 [finally] 已自动关闭 Word 进程")
        except asyncio.TimeoutError:
            logger.warning(
                "  ⏰ [finally] close_word 超过 %.1fs 未响应，放弃等待（不阻塞事件循环）",
                timeout,
            )
        except Exception as e:
            logger.warning("  ⚠️ [finally] close_word 执行失败: %s", e)

    def _save_session(self, user_input: str = "", summary: str = ""):
        """将本轮操作记录保存到记忆（含向量存储）"""
        if self.memory and self._session_tools:
            file_path = self._session_file or "unknown"
            self.memory.add_session(
                file_path=file_path,
                actions=self._session_tools,
                summary=summary[:200],  # 截断过长摘要
            )

        # 向量记忆：存入本轮 Q+A 摘要
        if self.memory and user_input:
            self.memory.add_to_vector(
                user_input,
                summary,
                current_file=self._session_file,
            )

    # ─────────────────────────────────────────────
    # Skill Config → 工具参数注入（Tool-Skill 分离架构）
    # ─────────────────────────────────────────────

    def _inject_skill_config(self, tool, arguments: dict) -> dict:
        """
        将 Skill Config 中的相关参数注入到工具调用中。

        注入原则（Tool-Skill 分离架构 + OCP 开闭原则）：
          - 每个 Tool 通过 injected_configs / required_configs 声明自身依赖
          - Agent 引擎动态读取声明，无需维护硬编码映射表
          - config 参数作为默认值（优先级低于 LLM 显式传参）
          - LLM 已经传递的参数不会被覆盖
          - required_configs 中声明的参数如果缺失，记录警告
            （工具自身会检测缺失并返回友好错误信息）

        Args:
            tool: 工具实例（从中读取 injected_configs / required_configs）
            arguments: LLM 传递的原始参数

        Returns:
            注入 config 后的参数字典
        """
        injected = dict(arguments)  # 浅拷贝，不修改原始参数

        # 动态读取工具声明的配置依赖（OCP：新增工具无需修改 Agent）
        config_keys = getattr(tool, 'injected_configs', [])
        if self._active_config and config_keys:
            for key in config_keys:
                if key not in injected and key in self._active_config:
                    value = self._active_config[key]
                    if value is not None:  # null 表示显式跳过
                        injected[key] = value
                        if self.verbose:
                            logger.debug("  ⚙️ Skill Config 注入: %s.%s",
                                         tool.name, key)

        # 检查必要参数是否就位（仅警告，实际拒绝由工具自身完成）
        required_keys = getattr(tool, 'required_configs', [])
        if required_keys:
            missing = [k for k in required_keys if k not in injected]
            if missing:
                logger.warning(
                    "  ⚠️ 工具 %s 缺少必要的 Skill Config 参数: %s "
                    "— 工具将拒绝执行。请确保用户输入包含触发 Skill 的关键词。",
                    tool.name, missing
                )

        return injected

    def _try_detect_literature_folder(self):
        """
        自动检测参考文献文件夹（基于文档路径约定）。

        当 _session_file 被设置时调用。如果检测到约定文件夹（如同级的
        参考文献/ 或 refs/ 或 {文档名}_refs/），自动记录并注入 _active_config。
        """
        if self._session_literature_folder:
            return  # 已经有了，不覆盖

        if not self._session_file:
            return

        try:
            from tools.citation_verifier import detect_literature_folder
            detected = detect_literature_folder(self._session_file)
            if detected:
                self._session_literature_folder = detected
                self._inject_literature_folder()
                logger.info("📂 自动检测到参考文献文件夹: %s", detected)
        except Exception as e:
            logger.debug("参考文献文件夹自动检测失败（非致命）: %s", e)

    def _inject_literature_folder(self):
        """
        将 _session_literature_folder 注入到 _active_config 中。

        这样声明了 injected_configs = ["literature_folder"] 的工具
        （如 verify_citations）在执行时会自动获得这个参数。
        """
        if self._session_literature_folder:
            self._active_config["literature_folder"] = self._session_literature_folder
            if self.verbose:
                logger.info("⚙️ 参考文献文件夹已注入会话配置: %s",
                            self._session_literature_folder)

    def _allow_memory_file_fallback(self, user_input: str = "") -> bool:
        text = (user_input or "").lower()
        keywords = (
            "上次", "上一", "之前", "继续", "恢复",
            "last", "previous", "continue", "resume",
        )
        return any(k in text for k in keywords)

    def _build_router_context(self, user_input: str = "") -> str:
        """
        为路由器构建丰富的上下文字符串。

        汇聚上下文帮助路由器解析隐式文件引用（如"这个文档"）：
          1. _session_file（当前会话已知文件路径）
          2. 参考文献文件夹（如果当前项目已设置）
          3. 当前文档最近历史；API 模式下不自动注入其它文档路径
        """
        parts = []

        # 1. 当前会话文件
        if self._session_file:
            parts.append(f"current_session_file: {self._session_file}")

        # 1.5 参考文献文件夹
        if self._session_literature_folder:
            parts.append(f"literature_folder: {self._session_literature_folder}")

        # 2. 记忆系统中的上次文件
        if self.memory:
            allow_fallback = (
                not self.api_mode
                or (not self._session_file
                    and self._allow_memory_file_fallback(user_input))
            )
            last_file = self.memory.get_last_file()
            if allow_fallback and last_file and last_file != self._session_file:
                parts.append(f"previous_file: {last_file}")

            # 3. 最近操作摘要（帮助解析"这个""那个"等指示代词）
            recent = self.memory.get_recent_sessions(5)
            if self._session_file:
                try:
                    current_abs = os.path.abspath(self._session_file)
                    recent = [
                        s for s in recent
                        if os.path.abspath(s.get("file", "")) == current_abs
                    ]
                except (TypeError, ValueError):
                    recent = []
            elif self.api_mode and not allow_fallback:
                recent = []

            recent = recent[-2:]
            if recent:
                for s in recent:
                    parts.append(
                        f"[{s['time']}] file: {s['file']}"
                    )

        return "\n".join(parts)

    def reset(self):
        """重置 Agent 状态（保留系统提示词）"""
        # 保留前部所有连续的 System 消息（静态根 + 动态叶，共 1~2 条）
        system_msgs = []
        for msg in self.history:
            if msg.role == Role.SYSTEM:
                system_msgs.append(msg)
            else:
                break
        self.history.clear()
        self.history.extend(system_msgs)
        self.state = AgentState.IDLE
        self._session_tools = []
        # 不清除 _session_file 和 _session_literature_folder，它们应跨对话轮次持久
        self._retry_counts = {}
        self._active_config = {}

    def reset_session(self):
        """完全重置 Agent 状态（包括会话级文件路径，用于全新会话）"""
        self.reset()
        self._session_file = ""
        self._session_literature_folder = ""

    # ─────────────────────────────────────────────
    # FSM Pipeline：Python 状态机驱动的 Worker 调度链
    # ─────────────────────────────────────────────

    def _run_fsm_pipeline_sync(self, fsm, user_input: str) -> str:
        """FSM Pipeline 的同步版（供已废弃的 run() 使用）。"""
        delegate_tool = self.tools.get("delegate_task")
        if not delegate_tool:
            return "❌ FSM 需要 delegate_task 工具，但未注册。"

        logger.info("🚂 FSM 接管: %s → 共 %d 步", fsm.intent.value, fsm.total_steps)

        for role, objective in fsm:
            logger.info("[FSM] Step %d/%d: %s", fsm.current_step + 1, fsm.total_steps, role)
            try:
                raw_report = delegate_tool.execute(
                    role=role,
                    objective=objective,
                    target_file=fsm.target_file,
                )
                import json as _json
                try:
                    report_dict = _json.loads(raw_report)
                except (ValueError, TypeError):
                    report_dict = {"status": "UNKNOWN", "summary": str(raw_report)[:200]}
            except Exception as e:
                report_dict = {"status": "FAIL", "summary": f"Worker 崩溃: {e}"}

            fsm.feed_report(report_dict)

        fsm_summary = fsm.build_summary()
        logger.info(fsm_summary)

        # 让 LLM 生成最终回答
        self.history.append(Message(
            role=Role.TOOL, content=fsm_summary,
            tool_call_id="fsm_pipeline", name="fsm_pipeline",
        ))
        self.history.append(Message(
            role=Role.USER,
            content="上方是 FSM 状态机自动执行的结果摘要。请用简洁的中文向用户汇报。",
        ))
        try:
            response = self.llm.chat(self.history)
            self.history.append(response)
            final_answer = response.content or fsm_summary
        except Exception:
            final_answer = fsm_summary

        self.state = AgentState.FINISHED
        self._save_session(user_input, final_answer)
        return final_answer

    async def _run_fsm_pipeline(
        self,
        fsm,
        user_input: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        FSM 驱动的 Worker 调度链（第二层：Python 硬接管）。

        遍历 FSM 的每一步，程序化调用 delegate_task，
        而非让 LLM 自由决定是否 fork Worker。

        大模型只负责「选轨道」，轨道上有几个检查站由代码焊死。
        """
        delegate_tool = self.tools.get("delegate_task")
        if not delegate_tool:
            yield StreamEvent("error", "FSM 需要 delegate_task 工具，但未注册。")
            return

        # Initialize interactive events
        self._approval_event = asyncio.Event()
        self._review_event = asyncio.Event()
        self._user_choice = None

        import os
        from core.version_manager import VersionManager

        target_file = fsm.target_file
        version_manager = None
        initial_commit_id = None
        if target_file and os.path.exists(target_file):
            try:
                version_manager = VersionManager(target_file)
                initial_commit = version_manager.create_checkpoint(
                    node_id="v_0_start",
                    description="任务开始前的原始文档",
                    plan_state=fsm.to_dag_snapshot()
                )
                if initial_commit:
                    initial_commit_id = initial_commit.get("commit_id")
                    # Yield initial checkpoint event so frontend can render it
                    yield StreamEvent(
                        "checkpoint_created",
                        "已成功创建初始保存点",
                        metadata=initial_commit
                    )
                logger.info("[Agent] FSM init: Created version control checkpoint: %s", initial_commit_id)
            except Exception as e:
                logger.warning("[Agent] FSM init: Checkpoint registry failed: %s", e)

        has_plan_node = any(n.id == "plan" for n in fsm._dag_nodes)
        has_review_node = any(n.id == "review" for n in fsm._dag_nodes)

        yield StreamEvent(
            "text",
            f"🚂 FSM 接管: {fsm.intent.value} → 共 {fsm.total_steps} 步\n\n",
        )

        for role, objective in fsm:
            step_num = fsm.current_step + 1

            if role == "Executor" and has_plan_node and self.api_mode:
                self._approval_event.clear()
                yield StreamEvent(
                    "require_approval",
                    "请确认是否开始执行此计划？",
                    metadata={"plan": fsm.build_summary()}
                )
                logger.info("[Agent] FSM paused. Waiting for user approval...")
                try:
                    await asyncio.wait_for(
                        self._approval_event.wait(),
                        timeout=FSM_INTERACTION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    yield StreamEvent(
                        "text",
                        "\n⏰ 等待计划确认超时，任务已中止。\n",
                    )
                    self.state = AgentState.FINISHED
                    yield StreamEvent("finish", "FSM 任务确认超时。")
                    return

                if self._user_choice == "reject":
                    yield StreamEvent("text", "\n🚫 用户已拒绝计划，任务终止。\n")
                    if version_manager and initial_commit_id:
                        try:
                            version_manager.rollback_to(initial_commit_id)
                            logger.info("[Agent] Reverted target file using version manager.")
                        except Exception as e:
                            logger.error(f"[Agent] Failed to revert backup: {e}")
                    self.state = AgentState.FINISHED
                    yield StreamEvent("finish", "FSM 任务已中止。")
                    return
                else:
                    yield StreamEvent("text", "\n✅ 计划已批准，启动排版引擎...\n\n")

            yield StreamEvent(
                "text",
                f"--- Step {step_num}/{fsm.total_steps}: **{role}** ---\n",
            )

            # ── 程序化调用 delegate_task（通过线程池，不阻塞事件循环）──
            progress_queue: _thread_queue.Queue = _thread_queue.Queue()
            wake_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _progress_cb(pct, msg, metadata=None,
                             _q=progress_queue, _ev=wake_event, _loop=loop):
                _q.put_nowait((pct, msg, metadata or {}))
                _loop.call_soon_threadsafe(_ev.set)

            delegate_tool._progress_callback = _progress_cb

            task = asyncio.create_task(
                asyncio.to_thread(
                    delegate_tool.execute,
                    role=role,
                    objective=objective,
                    target_file=fsm.target_file,
                )
            )
            task.add_done_callback(lambda _: wake_event.set())

            # ── 事件泵：中继 Worker 进度 ──
            try:
                while not task.done():
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    wake_event.clear()

                    while True:
                        try:
                            pct, msg, _meta = progress_queue.get_nowait()
                            yield StreamEvent(
                                "tool_progress",
                                msg or f"进度 {pct}%",
                                metadata={"percent": pct, "tool": "delegate_task"},
                            )
                        except _thread_queue.Empty:
                            break
            finally:
                delegate_tool._progress_callback = None

            # ── 获取 Worker 报告 ──
            try:
                raw_report = task.result()
                import json as _json
                try:
                    report_dict = _json.loads(raw_report)
                except (ValueError, TypeError):
                    report_dict = {
                        "status": "UNKNOWN",
                        "summary": str(raw_report)[:200],
                    }
            except Exception as e:
                report_dict = {
                    "status": "FAIL",
                    "summary": f"Worker 崩溃: {e}",
                }

            fsm.feed_report(report_dict)

            status = report_dict.get("status", "UNKNOWN")
            summary_text = report_dict.get("summary", "无摘要")
            emoji = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
            yield StreamEvent(
                "text",
                f"{emoji} [{role}] {status}: {summary_text}\n\n",
            )

            if version_manager:
                try:
                    commit = version_manager.create_checkpoint(
                        node_id=role,
                        description=f"完成【{role}】: {status} - {summary_text[:60]}",
                        plan_state=fsm.to_dag_snapshot()
                    )
                    if commit:
                        yield StreamEvent(
                            "checkpoint_created",
                            f"已生成步骤【{role}】保存点",
                            metadata=commit
                        )
                except Exception as e:
                    logger.warning("[Agent] Create step checkpoint failed: %s", e)

        # Loop finishes. Review report has been fed to FSM.
        if has_review_node and self.api_mode:
            self._review_event.clear()
            yield StreamEvent(
                "require_review_confirm",
                "请选择应用修改或撤销修改回退。",
                metadata={"summary": fsm.build_summary()}
            )
            logger.info("[Agent] FSM paused. Waiting for review confirmation...")
            try:
                await asyncio.wait_for(
                    self._review_event.wait(),
                    timeout=FSM_INTERACTION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                yield StreamEvent(
                    "text",
                    "\n⏰ 等待审查确认超时，保留当前修改并继续生成汇报。\n",
                )
                self._user_choice = "keep"

            if self._user_choice == "rollback":
                yield StreamEvent("text", "\n🔄 正在撤销所有修改，正在还原文件...\n")
                if version_manager and initial_commit_id:
                    try:
                        version_manager.rollback_to(initial_commit_id)
                        yield StreamEvent("text", "✅ 文档已成功还原至初始状态。\n")
                    except Exception as e:
                        yield StreamEvent("text", f"❌ 还原文件失败: {e}\n")
                else:
                    yield StreamEvent("text", "⚠️ 未找到备份，无法撤销。\n")
            else:
                yield StreamEvent("text", "\n🎉 修改已确认并应用。\n")

        # ── FSM 完成：让 LLM 生成用户友好的汇报 ──
        fsm_summary = fsm.build_summary()
        yield StreamEvent("text", f"\n{fsm_summary}\n\n")

        # 将 FSM 摘要注入历史，让 Coordinator LLM 生成最终回答
        self.history.append(Message(
            role=Role.TOOL,
            content=fsm_summary,
            tool_call_id="fsm_pipeline",
            name="fsm_pipeline",
        ))

        # 一次 LLM 调用生成自然语言汇报
        try:
            self.history.append(Message(
                role=Role.USER,
                content=(
                    "上方是 FSM 状态机自动执行的结果摘要。"
                    "请根据这些报告，用简洁的中文向用户汇报：做了什么、结果如何、是否有问题需要关注。"
                ),
            ))
            response = self.llm.chat(self.history)
            self.history.append(response)
            if response.content:
                yield StreamEvent("text", response.content)
        except Exception as e:
            yield StreamEvent("text", f"（汇报生成失败: {e}）")

        self.state = AgentState.FINISHED
        yield StreamEvent("finish", "FSM 流水线执行完毕。")
        self._save_session(user_input, fsm_summary)

    async def run_async(self, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """
        异步流式状态机 —— Agent 的心脏。

        整个 Think → Act → Observe 循环被切片为一系列 StreamEvent，
        通过 async generator 向外 yield。上层消费者（终端 / WebSocket /
        IDE Bridge）只是"哑终端"，switch-case 事件类型即可渲染。

        关键设计：
          - LLM 流式响应：async for chunk in stream → yield text delta
          - 工具执行：asyncio.to_thread + 线程安全 Queue 事件泵
            工具内部调用 self.report_progress() → 写入 Queue →
            主循环 poll Queue → yield tool_progress 事件
          - 随时可中断：调用方 break 即可终止生成器
        """
        # ── 会话级重置 ──
        self._session_tools = []
        # API 模式：文档路径由前端 /session/document 设置，不从记忆中读取旧路径
        if not self.api_mode:
            self._session_file = (self.memory.get_last_file() if self.memory else "") or ""
        self._retry_counts = {}
        self._active_config = {}

        try:
            async for event in self._run_async_impl(user_input):
                yield event
        finally:
            # 👑 确定性清理：无论正常结束/调用方 break(GeneratorExit)/异常，
            # 都在主事件循环外的线程池里兜底关闭 Word（wait_for 硬超时守护，
            # 绝不阻塞 LLM 流式输出 / WebSocket 推送 / 其他协程）。
            await self._close_word_safely_async()

    async def _run_async_impl(self, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """run_async() 的实际业务逻辑（被 try/finally 包裹以保证 Word 兜底关闭）。"""
        # ── 向量记忆召回 ──
        recalled = ""
        if self.memory:
            recalled = self.memory.recall_relevant(
                user_input,
                current_file=self._session_file,
            )

        # ── 技能匹配 + Config 提取 ──
        skills_ctx = ""
        if self.skill_manager:
            matched = self.skill_manager.match(user_input)
            skills_ctx = self.skill_manager.build_skills_context(matched)
            self._active_config = self.skill_manager.get_active_config(matched)

        # ── 重建系统提示词（含技能 + 记忆） ──
        self._build_system_prompt(skills_ctx, recalled)

        from core.prompt import build_l1_user_suffix
        l1_suffix = build_l1_user_suffix(user_input)
        augmented_input = user_input + l1_suffix if l1_suffix else user_input

        self.history.append(Message(role=Role.USER, content=augmented_input))
        self.state = AgentState.THINKING

        # ══════════════════════════════════════════
        # Coordinator 路由分支：意图分类 → FSM 硬接管
        # 非 Coordinator（Worker / 单 Agent）直接走 ReAct
        # ══════════════════════════════════════════
        is_coordinator = self.tools.get("delegate_task") is not None
        if is_coordinator:
            try:
                from core.router import classify_intent, TaskIntent, TaskFSM

                yield StreamEvent("text", "🚦 正在分析任务意图...\n")
                intent, router_file, reason = classify_intent(
                    self.llm, user_input,
                    history_context=self._build_router_context(user_input),
                )
                yield StreamEvent("text", f"📋 意图: **{intent.value}** — {reason}\n\n")

                # 如果 Router 提取到文件路径，记录到 session，并验证存在性
                if router_file:
                    import os
                    if os.path.exists(router_file):
                        self._session_file = router_file
                        # 自动检测参考文献文件夹（基于文档路径约定）
                        self._try_detect_literature_folder()
                    else:
                        yield StreamEvent("text", f"⚠️ 警告：检测到的目标文件不存在 ({router_file})，正在抛弃该路径...\n")
                        self._session_file = ""

                if intent != TaskIntent.TASK_SIMPLE and self._session_file:
                    fsm = TaskFSM(intent, user_input, self._session_file)
                    async for event in self._run_fsm_pipeline(fsm, user_input):
                        yield event
                    return
                elif intent != TaskIntent.TASK_SIMPLE and not self._session_file:
                    # 需要文件但没有文件 → 降级为 ReAct 让 LLM 询问用户
                    yield StreamEvent(
                        "text",
                        "⚠️ 未检测到文件路径，降级为自由对话模式。\n\n",
                    )
            except Exception as e:
                logger.warning("[Router] 路由异常，降级为 ReAct: %s", e)
                yield StreamEvent("text", f"⚠️ 路由异常，降级为自由模式: {e}\n\n")

        openai_tools = self.tools.to_openai_tools()

        # ══════════════════════════════════════════
        # ReAct 主循环（SIMPLE 任务 / Worker / 降级场景）
        # ══════════════════════════════════════════
        for step in range(1, self.max_steps + 1):
            # Token 水位线压缩防爆
            if self._estimate_tokens() > self._token_warning:
                self._compress_history()

            self.state = AgentState.THINKING

            # ── 1. LLM 流式推理 ──
            try:
                stream = await self.llm.chat_stream(
                    [m.to_dict() for m in self.history],
                    tools=openai_tools,
                )
            except Exception as e:
                yield StreamEvent("error", f"LLM 调用失败: {e}")
                self.state = AgentState.ERROR
                return

            current_text = ""
            current_tool_calls = {}

            # ── 2. 流式解析：一边想，一边吐，一边攒工具参数 ──
            async for chunk in stream:
                delta = chunk.choices[0].delta

                # 文本增量 → 直接推给 UI 渲染（打字机效果）
                if delta.content:
                    current_text += delta.content
                    yield StreamEvent("text", delta.content)

                # 工具调用增量拼接（不阻塞 UI）
                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_chunk.id,
                                "name": tc_chunk.function.name,
                                "args": "",
                            }
                        if tc_chunk.function.arguments:
                            current_tool_calls[idx]["args"] += tc_chunk.function.arguments

            # ── 3. 组装 Assistant 消息 ──
            tool_calls_parsed = []
            for tc_data in current_tool_calls.values():
                try:
                    args = json.loads(tc_data["args"])
                except json.JSONDecodeError:
                    args = {"raw": tc_data["args"]}
                tool_calls_parsed.append(
                    ToolCall(id=tc_data["id"], name=tc_data["name"], arguments=args)
                )

            self.history.append(Message(
                role=Role.ASSISTANT,
                content=current_text,
                tool_calls=tool_calls_parsed,
            ))

            # ══════════════════════════════════════════
            # 路由：纯文本 → 结束 ／ tool_calls → 执行
            # ══════════════════════════════════════════
            if not tool_calls_parsed:
                self.state = AgentState.FINISHED
                l1_check = self._post_validate_l1()
                if l1_check:
                    yield StreamEvent("error", f"L1 后校验触发: {l1_check}")
                yield StreamEvent("finish", "任务执行完毕。")
                self._save_session(user_input, current_text)
                return

            # ── 4. 工具执行（心跳事件泵模式） ──
            self.state = AgentState.ACTING
            for tc in tool_calls_parsed:
                yield StreamEvent(
                    "tool_start",
                    f"正在执行: {tc.name}",
                    metadata={"tool": tc.name, "args": tc.arguments},
                )

                # -- 核心：线程安全队列 + asyncio.Event 信号驱动 --
                progress_queue: _thread_queue.Queue = _thread_queue.Queue()
                wake_event = asyncio.Event()  # 工具线程有新数据时唤醒事件泵
                loop = asyncio.get_running_loop()

                # 注入进度回调：支持 metadata 透传（含 temp_timeout 租约申请）
                tool_obj = self.tools.get(tc.name)
                if tool_obj is not None:
                    def _progress_cb(pct, msg, metadata=None, _q=progress_queue, _ev=wake_event, _loop=loop):
                        _q.put_nowait((pct, msg, metadata or {}))
                        _loop.call_soon_threadsafe(_ev.set)
                    tool_obj._progress_callback = _progress_cb

                # 在线程池中执行同步工具（不阻塞事件循环）
                task = asyncio.create_task(
                    asyncio.to_thread(self._execute_tool, tc.id, tc.name, tc.arguments)
                )
                # task 完成时也唤醒泵（防止 task 在 wait 期间结束但泵在睡觉）
                task.add_done_callback(lambda _: wake_event.set())

                # ── 信号驱动事件泵（非 busy-polling） ──
                STALL_TIMEOUT = 15.0  # 基础心跳停滞阈值（秒）
                last_heartbeat = time.time()
                stall_killed = False

                try:
                    while not task.done():
                        # 挂起等待：工具线程 set event 或超时（=心跳截止时间）
                        remaining = max(0.05, STALL_TIMEOUT - (time.time() - last_heartbeat))
                        try:
                            await asyncio.wait_for(wake_event.wait(), timeout=remaining)
                        except asyncio.TimeoutError:
                            pass  # 超时 → 下面做心跳检测
                        wake_event.clear()

                        # 排空队列中所有进度事件 & 动态更新看门狗
                        while True:
                            try:
                                pct, msg, meta = progress_queue.get_nowait()
                                last_heartbeat = time.time()  # 心跳续命

                                # 👑 核心修复：捕获租约申请，动态修改阈值
                                if "temp_timeout" in meta:
                                    STALL_TIMEOUT = float(meta["temp_timeout"])
                                    logger.debug("  ⏰ 看门狗阈值动态调整为: %.1fs", STALL_TIMEOUT)

                                yield StreamEvent(
                                    "tool_progress",
                                    msg or f"进度 {pct}%",
                                    metadata={"percent": pct, "tool": tc.name},
                                )
                            except _thread_queue.Empty:
                                break

                        # ── 心跳停滞检测 ──
                        stall_sec = time.time() - last_heartbeat
                        if stall_sec > STALL_TIMEOUT:
                            # 精准击杀僵尸 Word 进程（从类级别 PID 注册表获取）
                            from core.com_watchdog import COMSafeLock
                            active_pids = COMSafeLock.get_active_pids()
                            killed = COMSafeLock.kill_pids(active_pids) if active_pids else []

                            logger.warning(f"  💀 触发熔断！等待后台线程释放资源...")

                            # 👑 核心修复：不要 cancel，而是设置极短的超时死等线程自己崩溃并执行完 finally
                            # task.cancel() 只能取消外层 asyncio 包装，杀不掉底层 OS 线程，
                            # 会导致幽灵线程在未来随机时刻执行快照回滚，覆盖新文件。
                            # 正确做法：Word 进程已被杀，底层线程会在 1~2 秒内抛出 com_error，
                            # 给它 3 秒钟时间处理异常并跑完 finally 块的清理逻辑。
                            try:
                                await asyncio.wait_for(task, timeout=3.0)
                            except Exception:
                                pass

                            # 结构化错误回注历史，唤醒 LLM 大脑
                            # 👑 核心改进：生成结构化的自修正引导信息，包含参数诊断
                            timeout_msg = self._build_timeout_observation(
                                tc.name, tc.arguments, stall_sec, STALL_TIMEOUT, killed
                            )
                            self.history.append(Message(
                                role=Role.TOOL,
                                content=timeout_msg,
                                tool_call_id=tc.id,
                                name=tc.name,
                            ))
                            yield StreamEvent(
                                "tool_timeout",
                                timeout_msg,
                                metadata={
                                    "tool": tc.name,
                                    "stall_seconds": round(stall_sec, 1),
                                    "killed_pids": killed,
                                },
                            )
                            stall_killed = True
                            break  # 跳出事件泵循环
                finally:
                    # 防御性清理：无论工具怎样退出，强制弹回基础阈值
                    if STALL_TIMEOUT != 15.0:
                        logger.debug("  🧹 [防御性清理] 工具退出，看门狗从 %.1fs 弹回 15.0s", STALL_TIMEOUT)
                        STALL_TIMEOUT = 15.0
                    # 调用方中断 async generator 时也必须清理回调，避免污染下一轮请求。
                    if tool_obj is not None:
                        tool_obj._progress_callback = None

                if stall_killed:
                    # 清理回调，继续下一轮 ReAct（LLM 读到错误后决定下一步）
                    continue  # 跳过本 tool_call 的结果处理，回到 for tc 循环

                # 排空工具完成后残留的进度事件
                while True:
                    try:
                        pct, msg, _meta = progress_queue.get_nowait()
                        yield StreamEvent(
                            "tool_progress",
                            msg or f"进度 {pct}%",
                            metadata={"percent": pct, "tool": tc.name},
                        )
                    except _thread_queue.Empty:
                        break

                # 获取执行结果
                try:
                    result = task.result()
                    self.history.append(Message(
                        role=Role.TOOL,
                        content=result.output,
                        tool_call_id=result.tool_call_id,
                        name=result.name,
                    ))
                    yield StreamEvent(
                        "tool_end",
                        f"{tc.name} 完成",
                        metadata={"success": result.success, "tool": tc.name},
                    )
                except Exception as e:
                    error_output = f"工具执行崩溃: {e}"
                    self.history.append(Message(
                        role=Role.TOOL,
                        content=error_output,
                        tool_call_id=tc.id,
                        name=tc.name,
                    ))
                    yield StreamEvent(
                        "error", error_output,
                        metadata={"tool": tc.name},
                    )

        # 超过最大步数熔断
        self.state = AgentState.ERROR
        yield StreamEvent("error", f"达到最大步骤上限 ({self.max_steps})，强制停止。")
