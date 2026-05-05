# -*- coding: utf-8 -*-
"""
Tool: 标题样式应用（Heading Styler）

原子 API：接收结构推断结果（段落索引→标题层级映射），
          批量将指定段落设置为 Word 内置 Heading 样式。

设计原则：
  - 工具只做"物理操作"（设置样式），不做"语义推理"（哪些是标题）
  - 语义推理由 LLM + structure_inference Skill 完成
  - 工具通过 COMSafeLock 四重防护安全操作 Word 文档
"""

import os

from tools.base import Tool
from core.logger import logger


# Word 内置 Heading 样式常量（wdStyleHeading1 = -2, etc.）
_WD_HEADING_STYLES = {
    1: -2,   # wdStyleHeading1
    2: -3,   # wdStyleHeading2
    3: -4,   # wdStyleHeading3
    4: -5,   # wdStyleHeading4
    5: -6,   # wdStyleHeading5
}


class ApplyHeadingStylesTool(Tool):
    """
    批量应用 Word Heading 样式。

    接收一个 headings 列表，每项包含段落索引（1-based）和目标层级（1-5），
    将对应段落的样式设置为 Word 内置的 Heading 1/2/3/4/5。
    """

    name = "apply_heading_styles"
    description = (
        "将 Word 文档中的指定段落设置为 Heading 样式（H1-H5）。\n"
        "输入：文档路径 + headings 列表（每项含 paragraph_index 和 level）。\n"
        "适用场景：结构推演后自动应用标题层级；或用户手动指定哪些段落是标题。\n"
        "注意：paragraph_index 从 1 开始计数，与 read_document 输出的段落序号一致。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Word 文档的完整文件路径",
            },
            "headings": {
                "type": "array",
                "description": (
                    "标题映射列表。每项包含：\n"
                    "  - paragraph_index (int): 段落序号（1-based）\n"
                    "  - level (int): 标题层级（1=H1, 2=H2, 3=H3, 4=H4, 5=H5）\n"
                    "  - text (string, 可选): 预期的段落文本（用于二次校验，防止索引偏移）\n"
                    '示例: [{"paragraph_index": 3, "level": 1, "text": "1 引言"}, '
                    '{"paragraph_index": 15, "level": 2, "text": "1.1 研究背景"}]'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "paragraph_index": {
                            "type": "integer",
                            "description": "段落序号（1-based）",
                        },
                        "level": {
                            "type": "integer",
                            "description": "标题层级（1-5）",
                        },
                        "text": {
                            "type": "string",
                            "description": "预期段落文本（可选，用于校验）",
                        },
                    },
                    "required": ["paragraph_index", "level"],
                },
            },
        },
        "required": ["file_path", "headings"],
    }

    def execute(self, file_path: str, headings: list, **kwargs) -> str:
        from core.com_watchdog import COMSafeLock

        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return f"❌ 文件不存在: {abs_path}"

        if not headings:
            return "❌ headings 列表为空，没有需要设置的标题。"

        # 参数校验
        for h in headings:
            level = h.get("level", 0)
            if level not in _WD_HEADING_STYLES:
                return f"❌ 不支持的标题层级: {level}（仅支持 1-5）"
            idx = h.get("paragraph_index", 0)
            if idx < 1:
                return f"❌ 段落序号必须 ≥ 1，收到: {idx}"

        self.report_progress(5, f"准备设置 {len(headings)} 个标题样式...")

        applied = []
        skipped = []
        errors = []

        with COMSafeLock(abs_path) as (word_app, doc):
            total_paras = doc.Paragraphs.Count
            self.report_progress(10, f"文档共 {total_paras} 个段落")

            for i, h in enumerate(headings):
                idx = h["paragraph_index"]
                level = h["level"]
                expected_text = h.get("text", "").strip()

                # 索引越界检查
                if idx > total_paras:
                    skipped.append(f"段落 {idx}: 超出范围（共 {total_paras} 段）")
                    continue

                try:
                    para = doc.Paragraphs(idx)
                    actual_text = para.Range.Text.strip()

                    # 可选的文本校验：如果提供了 expected_text，检查是否匹配
                    if expected_text and expected_text not in actual_text:
                        # 尝试容错：在前后 ±2 段中搜索
                        found = False
                        for offset in [-1, 1, -2, 2]:
                            check_idx = idx + offset
                            if 1 <= check_idx <= total_paras:
                                check_para = doc.Paragraphs(check_idx)
                                check_text = check_para.Range.Text.strip()
                                if expected_text in check_text:
                                    para = check_para
                                    actual_text = check_text
                                    found = True
                                    logger.info(
                                        "[HeadingStyler] 段落 %d 文本不匹配，"
                                        "在偏移 %+d 处找到匹配（段落 %d）",
                                        idx, offset, check_idx,
                                    )
                                    break
                        if not found:
                            skipped.append(
                                f"段落 {idx}: 文本校验失败 "
                                f"(预期含'{expected_text[:20]}...', "
                                f"实际='{actual_text[:20]}...')"
                            )
                            continue

                    # 应用 Heading 样式
                    style_id = _WD_HEADING_STYLES[level]
                    para.Style = style_id
                    applied.append(f"段落 {idx} → Heading {level}: {actual_text[:30]}")

                except Exception as e:
                    errors.append(f"段落 {idx}: {e}")

                # 更新进度
                pct = 10 + int(80 * (i + 1) / len(headings))
                self.report_progress(pct, f"已处理 {i + 1}/{len(headings)}")

            # 保存由 COMSafeLock 的 __exit__ 处理
            self.report_progress(95, "样式应用完成，正在保存...")

        self.report_progress(100, "完成")

        # 构建报告
        lines = [f"✅ 标题样式应用完成：{len(applied)} 个成功"]

        if applied:
            lines.append(f"\n已设置的标题（共 {len(applied)} 个）：")
            for a in applied[:15]:  # 最多展示 15 条
                lines.append(f"  ✅ {a}")
            if len(applied) > 15:
                lines.append(f"  ... 及另外 {len(applied) - 15} 个")

        if skipped:
            lines.append(f"\n⚠️ 跳过（共 {len(skipped)} 个）：")
            for s in skipped:
                lines.append(f"  ⚠️ {s}")

        if errors:
            lines.append(f"\n❌ 错误（共 {len(errors)} 个）：")
            for e in errors:
                lines.append(f"  ❌ {e}")

        return "\n".join(lines)
