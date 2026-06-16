# -*- coding: utf-8 -*-
"""
SubmitReportTool — Worker 强制结构化报告提交器

设计动机：
  旧方案要求 Worker 在最终文本中输出 JSON，但 LLM 经常在 JSON 前后附加
  说明文字，导致 _extract_report() 的正则提取失败，返回 UNKNOWN 状态。

新方案：
  Worker 通过调用 submit_report **工具** 提交报告。
  工具调用的参数由 LLM 以结构化方式填写，框架层直接拿到 Python dict，
  完全绕过文本解析，从根本上消除 JSON 提取失败的可能。

使用方式：
  1. DelegateTaskTool 为每个 Worker 创建一个 SubmitReportTool 实例
  2. 将该实例注册到 Worker 的工具注册表中
  3. Worker 执行完毕后，调用 instance.get_report() 获取报告
  4. 若 Worker 从未调用该工具（异常退出），get_report() 返回 None，
     DelegateTaskTool 回退到旧的文本解析逻辑
"""

from tools.base import Tool


class SubmitReportTool(Tool):
    """
    Worker 完成任务后必须调用此工具提交结构化报告。

    与旧方案（输出 JSON 文本）的核心区别：
      - 旧方案：LLM 输出文本 → 正则提取 JSON → 可能失败
      - 新方案：LLM 调用工具 → 框架直接拿到 dict → 永远不会解析失败
    """

    name = "submit_report"
    description = (
        "【必须调用】Worker 完成任务后提交结构化报告。"
        "任务结束时必须调用此工具，而不是在文本中输出 JSON。"
        "调用后直接结束，不要再输出任何文字。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["PASS", "FAIL"],
                "description": "任务状态：PASS 表示成功完成，FAIL 表示执行失败",
            },
            "summary": {
                "type": "string",
                "description": "一句话总结完成情况（中文，50字以内）",
            },
            "output_path": {
                "type": "string",
                "description": (
                    "实际产出文件的绝对路径。"
                    "若未生成新文件（原地修改），填写目标文件路径。"
                    "若未涉及文件操作，留空字符串。"
                ),
            },
            "issues_found": {
                "type": "array",
                "items": {"type": "string"},
                "description": "发现的问题列表，每项一句话描述。无问题时传空数组 []",
            },
            "actions_taken": {
                "type": "array",
                "items": {"type": "string"},
                "description": "已执行的操作列表，每项一句话描述。",
            },
        },
        "required": ["status", "summary"],
    }

    def __init__(self):
        super().__init__()
        # 存储 Worker 提交的报告，None 表示尚未提交
        self._report: dict | None = None

    def execute(
        self,
        status: str,
        summary: str,
        output_path: str = "",
        issues_found: list = None,
        actions_taken: list = None,
    ) -> str:
        """
        接收 Worker 提交的报告并持久化到实例变量。

        Returns:
            确认消息（Worker 看到此消息后应立即停止，不再输出任何内容）
        """
        # 规范化 status（防止 LLM 输出 "pass" / "Pass" 等变体）
        status_upper = (status or "FAIL").strip().upper()
        if status_upper not in ("PASS", "FAIL"):
            status_upper = "FAIL"

        self._report = {
            "status": status_upper,
            "summary": summary or "",
            "output_path": output_path or "",
            "issues_found": issues_found if isinstance(issues_found, list) else [],
            "actions_taken": actions_taken if isinstance(actions_taken, list) else [],
        }

        from core.logger import logger
        logger.info(
            "[SubmitReport] ✅ Worker 报告已提交: status=%s, summary=%.60s",
            status_upper, summary,
        )

        return f"报告已提交（{status_upper}）。任务结束，请勿再输出任何内容。"

    def get_report(self) -> dict | None:
        """
        获取 Worker 提交的报告。

        Returns:
            报告 dict，若 Worker 从未调用此工具则返回 None
        """
        return self._report

    def was_called(self) -> bool:
        """Worker 是否已调用过此工具。"""
        return self._report is not None