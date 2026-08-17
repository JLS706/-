# -*- coding: utf-8 -*-
"""
DocMaster Agent - 主入口
学术论文排版 AI 智能体，通过自然语言指令驱动 Word 文档自动化处理。

用法:
    python main.py                  # 交互式模式
    python main.py --dry-run        # Dry-run 模式（不实际执行工具）
    python main.py --test           # 测试 LLM 连通性
"""

import os
import sys
import argparse
import asyncio

from core.logger import logger

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def load_config() -> dict:
    """
    加载配置文件。

    支持环境变量覆盖 API Key（优先级高于配置文件）：
      DOCMASTER_API_KEY=xxx python main.py
    """
    config_path = os.path.join(PROJECT_ROOT, "config", "config.toml")
    if not os.path.exists(config_path):
        print("❌ 配置文件未找到！")
        print(f"   请复制 config/config.example.toml 为 config/config.toml")
        print(f"   然后填入你的 LLM API Key")
        sys.exit(1)

    # 选择 TOML 解析器：优先 toml 库，回退 tomllib（Python 3.11+）
    try:
        import toml
        with open(config_path, "r", encoding="utf-8") as f:
            config = toml.load(f)
    except ImportError:
        try:
            import tomllib
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
        except ImportError:
            print("❌ 需要安装 toml 库: pip install toml")
            sys.exit(1)

    # 环境变量覆盖 API Key（Docker/CI 场景更安全）
    env_key = os.environ.get("DOCMASTER_API_KEY")
    if env_key:
        config.setdefault("llm", {})["api_key"] = env_key

    return config



def create_agent(config: dict, dry_run: bool = False):
    """根据配置创建 Coordinator Agent 实例（蜂群模式，Worker 由 delegate_task 按需 fork）"""
    from core.llm import LLM
    from core.agent import Agent
    from core.memory import Memory
    from tools.base import ToolRegistry

    # 导入所有工具
    from tools.ref_formatter import RefFormatterTool
    from tools.ref_crossref import RefCrossRefTool
    from tools.fig_crossref import FigCrossRefTool
    from tools.fig_caption import FigCaptionTool
    from tools.acronym_checker import AcronymCheckerTool
    from tools.latex_converter import LatexConverterTool
    from tools.doc_reader import DocReaderTool
    from tools.pipeline import AnalyzeDocumentTool
    from tools.memory_tool import RecallHistoryTool, SavePreferenceTool
    from tools.code_interpreter import CodeInterpreterTool
    from tools.learned_rules import (
        SaveLearnedRuleTool, ForgetLearnedRuleTool, ListLearnedRulesTool,
        UpdateProfileTool, ViewProfileTool,
    )
    from tools.rag import IndexDocumentTool, SearchDocumentTool
    from tools.rag import IndexLiteratureTool, SearchLiteratureTool, ListLiteratureTool, AutoBindLiteratureTool
    from tools.citation_verifier import VerifyCitationsTool, CheckClaimTool
    from tools.figure_analyzer import AnalyzeFigureTool
    from tools.doc_summarizer import SummarizeDocumentTool
    from tools.doc_format_inspector import InspectDocFormatTool
    from tools.word_cleanup import CloseWordTool
    from tools.heading_styler import ApplyHeadingStylesTool
    from tools.delegate import DelegateTaskTool
    # 动态工具创建属于旧版实验能力。当前副本没有 tool_creator.py，
    # PaperOps 主链路也不依赖它，因此按可选能力加载，避免阻塞 Agent 启动。
    try:
        from tools.tool_creator import (
            CreateToolTool, ApproveToolTool, ListCustomToolsTool, RejectToolTool,
            load_custom_tools,
        )
        tool_creator_available = True
    except ModuleNotFoundError:
        tool_creator_available = False
        logger.warning("未找到 tools.tool_creator，跳过动态工具创建能力")

    # 初始化 LLM
    llm_config = config.get("llm", {})
    llm = LLM(**llm_config)

    # 初始化 Embedding 客户端（Memory 和 Skills 共用）
    embed_client = None
    try:
        from core.embeddings import EmbeddingClient
        embed_client = EmbeddingClient(
            api_key=llm_config.get("api_key", ""),
            base_url=llm_config.get("base_url", ""),
            model=llm_config.get("embedding_model", "gemini-embedding-001"),
            timeout=llm_config.get("embedding_timeout", 30.0),
        )
    except Exception as e:
        logger.warning("⚠️ Embedding 客户端初始化失败（向量记忆和 RAG 将不可用）: %s", e)

    # 初始化本地记忆（含向量记忆）
    memory_dir = os.path.join(PROJECT_ROOT, "memory")
    memory = Memory(memory_dir, embed_client=embed_client, llm=llm)

    # 注册所有工具
    registry = ToolRegistry()
    registry.register(AnalyzeDocumentTool())      # 文档分析（Pipeline规划）
    registry.register(DocReaderTool())             # 文档读取（供LLM分析）
    registry.register(InspectDocFormatTool())       # 文档格式检查（样式/字体/缩进）
    registry.register(RecallHistoryTool(memory))   # 记忆查询
    registry.register(SavePreferenceTool(memory))  # 偏好保存
    registry.register(RefFormatterTool())
    registry.register(RefCrossRefTool())
    registry.register(FigCrossRefTool())
    registry.register(FigCaptionTool())
    registry.register(AcronymCheckerTool())
    registry.register(LatexConverterTool())
    registry.register(CodeInterpreterTool())        # 安全沙盒代码解释器
    registry.register(SaveLearnedRuleTool())         # 自学习：保存规则（旧版兼容）
    registry.register(ForgetLearnedRuleTool())       # 自学习：删除规则（旧版兼容）
    registry.register(ListLearnedRulesTool())        # 自学习：列出规则（旧版兼容）
    registry.register(UpdateProfileTool())           # L1 画像：增量更新
    registry.register(ViewProfileTool())             # L1 画像：查看
    registry.register(IndexDocumentTool())           # RAG：文档索引
    registry.register(SearchDocumentTool())          # RAG：语义搜索
    registry.register(IndexLiteratureTool())          # 文献库：索引单篇文献（PDF/Word）
    registry.register(SearchLiteratureTool())         # 文献库：语义搜索（单篇/跨库）
    registry.register(ListLiteratureTool())           # 文献库：列出已索引文献
    registry.register(AutoBindLiteratureTool())        # 文献库：自动绑定参考文献→本地文件
    registry.register(VerifyCitationsTool(llm=llm))  # 引用溯源审计（批量）
    registry.register(CheckClaimTool(llm=llm))       # 引用校验（单句级，用户主动触发）
    registry.register(AnalyzeFigureTool(llm=llm))     # 多模态：PDF 图表视觉分析
    registry.register(CloseWordTool())                # Word 进程清理
    registry.register(SummarizeDocumentTool())         # 全文摘要(Map-Reduce)
    registry.register(ApplyHeadingStylesTool())        # 标题样式批量应用（结构推演后使用）
    if tool_creator_available:
        registry.register(CreateToolTool())             # 🧬 动态工具创建（旧版可选）
        registry.register(ApproveToolTool(registry))    # 🧬 审批工具
        registry.register(ListCustomToolsTool())        # 🧬 列出自定义工具
        registry.register(RejectToolTool())             # 🧬 否决工具

        # 自动加载已激活的自定义工具
        custom_count = load_custom_tools(registry)
        logger.info("已成功加载 %d 个自定义工具", custom_count)

    # 🐝 蜂群派发器：只有 Coordinator（主 Agent）拥有此工具
    # Worker 通过 registry.exclude({"delegate_task"}) 获得无此工具的子集
    # coordinator_agent 在 Agent 创建后回填（见下方）
    delegate_tool = DelegateTaskTool(llm, registry)
    registry.register(delegate_tool)

    # 初始化 Skills 管理器（复用同一 embed_client）
    from core.skills import SkillManager
    skills_dir = os.path.join(PROJECT_ROOT, "skills")
    skill_manager = SkillManager(skills_dir, embed_client=embed_client)
    logger.info("已加载 %d 个技能: %s",
                len(skill_manager.skills),
                [s.name for s in skill_manager.skills])

    # 创建 Coordinator Agent（registry 含 delegate_task 时自动激活蜂群指挥官人设）
    agent_config = config.get("agent", {})

    # 根据配置调整日志级别
    from core.logger import setup_logger
    verbose = agent_config.get("verbose", True)
    setup_logger(verbose=verbose)

    agent = Agent(
        llm=llm,
        tool_registry=registry,
        max_steps=agent_config.get("max_steps", 10),
        verbose=agent_config.get("verbose", True),
        dry_run=dry_run,
        memory=memory,
        skill_manager=skill_manager,
    )

    # 回填 Coordinator 引用：让 DelegateTaskTool 能透传 _active_config 给 Worker
    delegate_tool._coordinator = agent

    return agent


def test_connection(config: dict):
    """测试 LLM 连通性"""
    from core.llm import LLM

    llm_config = config.get("llm", {})
    logger.info("🔗 正在测试 LLM 连接...")
    logger.info("   模型: %s", llm_config.get('model', '未指定'))
    logger.info("   地址: %s", llm_config.get('base_url', '未指定'))

    try:
        llm = LLM(**llm_config)
        reply = llm.test_connection()
        logger.info("✅ 连接成功！模型回复: %s", reply)
    except Exception as e:
        logger.error("❌ 连接失败: %s", e)
        import traceback
        traceback.print_exc()


async def interactive_loop_async(agent):
    """异步交互循环：实时消费 Agent 发射的 StreamEvent"""
    print("\n" + "=" * 60)
    print("  🤖 DocMaster Agent [异步流式引擎版]")
    print("  输入 'quit' 或 'exit' 退出")
    print("=" * 60)

    while True:
        # 接收用户输入（使用 asyncio.to_thread 防止阻塞事件循环）
        try:
            user_input = await asyncio.to_thread(input, "\n🧑 你: ")
            user_input = user_input.strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("👋 再见！")
            break

        print("\n🤖 Agent: ", end="", flush=True)

        # 【核心】消费异步生成器！就像接住水管里流出来的水
        try:
            async for event in agent.run_async(user_input):
                if event.type == "text":
                    # 实时打印大模型思考的文字（打字机效果）
                    print(event.content, end="", flush=True)
                elif event.type == "tool_start":
                    # 打印高亮的工具调用提示
                    print(f"\n\n[🔧 {event.content}]", end="", flush=True)
                elif event.type == "tool_progress":
                    # 终端进度条：覆写当前行
                    pct = event.metadata.get("percent", 0)
                    bar_len = 20
                    filled = int(bar_len * pct / 100)
                    bar = "█" * filled + "░" * (bar_len - filled)
                    print(f"\r  [{bar}] {pct:3d}% {event.content}", end="", flush=True)
                elif event.type == "tool_end":
                    # 清除进度条行，打印完成状态
                    print(f"\r  ✅ {event.content}" + " " * 40, flush=True)
                elif event.type == "tool_timeout":
                    # 心跳停滞熔断：清除进度条，打印醒目警告
                    stall = event.metadata.get("stall_seconds", "?")
                    print(f"\r  ⏰ 进度卡死 {stall}s，已强制熔断" + " " * 30, flush=True)
                elif event.type == "error":
                    print(f"\n[❌ 错误] {event.content}", flush=True)
                elif event.type == "finish":
                    print(f"\n[✅ {event.content}]", end="", flush=True)
            print() # 换行收尾
        except Exception as e:
            print(f"\n[💥 引擎崩溃] {e}")

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="DocMaster Agent - 学术论文排版 AI 智能体",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Dry-run 模式（不实际执行工具）",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="测试 LLM 连通性后退出",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=None,
        help="打印详细调试信息（覆盖配置文件）",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="静默模式，减少日志输出（覆盖配置文件）",
    )
    return parser.parse_args()

# 将入口函数也改为异步
async def main_async():
    args = parse_args()
    config = load_config()

    # --verbose / --quiet 覆盖配置文件中的 verbose 设置
    if args.quiet:
        config.setdefault("agent", {})["verbose"] = False
    elif args.verbose is not None:
        config.setdefault("agent", {})["verbose"] = True

    # --test: 测试 LLM 连通性后退出
    if args.test:
        test_connection(config)
        return

    agent = create_agent(config, dry_run=args.dry_run)

    # 启动异步交互循环
    await interactive_loop_async(agent)

def cli():
    """CLI 入口点（供 setup.py 的 console_scripts 调用）"""
    asyncio.run(main_async())

if __name__ == "__main__":
    # 使用 asyncio.run 启动整个异步系统
    cli()
