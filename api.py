# -*- coding: utf-8 -*-
"""
DocMaster Agent — FastAPI Web 接口层（SSE 流式版）

将命令行 Agent 包装为 HTTP 服务，支持 Server-Sent Events 实时推送。

启动方式:
    python api.py

接口:
    POST /chat/stream  — SSE 流式对话（实时推送 StreamEvent）
    POST /chat         — 兼容接口（阻塞式，已废弃）
    GET  /health       — 健康检查
    GET  /tools        — 查看可用工具列表
"""

import json
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent import Agent
from tools.base import ToolRegistry


# ─────────────────────────────────────────────
# 全局单例（在 lifespan 中初始化）
# ─────────────────────────────────────────────

agent_instance: Agent | None = None
tool_registry: ToolRegistry | None = None


# ─────────────────────────────────────────────
# FastAPI 应用生命周期
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用启动时初始化 Agent，关闭时清理资源。
    这是 FastAPI 推荐的初始化方式（替代 @app.on_event）。
    复用 main.py 的初始化逻辑，确保工具注册与 CLI 完全一致。
    """
    global agent_instance, tool_registry

    from main import load_config, create_agent

    print("[*] 正在初始化 Agent（复用 main.py 完整初始化逻辑）...")
    config = load_config()
    agent_instance = create_agent(config)
    tool_registry = agent_instance.tools
    print(f"[OK] Agent 就绪，已加载 {len(tool_registry)} 个工具")

    yield  # ← 应用运行中

    print("[*] Agent 服务关闭")


app = FastAPI(
    title="DocMaster Agent API",
    description="学术论文排版 AI 智能助手 — HTTP 接口",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────
# 请求/响应数据模型
# ─────────────────────────────────────────────

class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    literature_folder: str = ""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"message": "帮我检查一下缩写有没有定义"},
                {
                    "message": "帮我审计这篇论文的引用是否准确",
                    "literature_folder": "C:/papers/my_thesis_refs",
                },
            ]
        }
    }


class ChatResponse(BaseModel):
    """对话回复"""
    reply: str
    success: bool


class ToolInfo(BaseModel):
    """工具信息"""
    name: str
    description: str
    parameters: list[str]


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.get("/health")
def health_check():
    """
    健康检查 — Docker/K8s 用这个接口判断服务是否存活。
    """
    return {
        "status": "healthy",
        "agent_ready": agent_instance is not None,
    }


@app.get("/tools", response_model=list[ToolInfo])
def list_tools():
    """
    查看所有可用工具。
    """
    if tool_registry is None:
        return []
    tools = []
    for t in tool_registry.get_all_tools():
        params = list(t.parameters.get("properties", {}).keys())
        tools.append(ToolInfo(
            name=t.name,
            description=t.description[:100],
            parameters=params,
        ))
    return tools


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    SSE 流式对话接口 — 实时推送 Agent 的 StreamEvent。

    每个 SSE 消息格式:
        event: {event_type}
        data: {json_payload}

    前端消费示例 (JavaScript):
        const es = new EventSource('/chat/stream', { method: 'POST', body: ... });
        es.addEventListener('text', (e) => appendToUI(JSON.parse(e.data).content));
        es.addEventListener('tool_progress', (e) => updateProgressBar(JSON.parse(e.data)));
        es.addEventListener('finish', () => es.close());
    """
    if agent_instance is None:
        async def error_gen():
            yield f"event: error\ndata: {json.dumps({'content': 'Agent 未初始化'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    async def event_generator():
        agent_instance.reset()
        # 如果前端传入了参考文献文件夹，注入到 agent 会话配置
        if req.literature_folder and req.literature_folder.strip():
            agent_instance._session_literature_folder = req.literature_folder.strip()
            agent_instance._inject_literature_folder()
        try:
            async for event in agent_instance.run_async(req.message):
                payload = json.dumps({
                    "type": event.type,
                    "content": event.content,
                    "metadata": event.metadata,
                }, ensure_ascii=False)
                yield f"event: {event.type}\ndata: {payload}\n\n"
        except Exception as e:
            error_payload = json.dumps({
                "type": "error",
                "content": f"Agent 执行崩溃: {e}",
                "metadata": {},
            }, ensure_ascii=False)
            yield f"event: error\ndata: {error_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁止 Nginx 缓冲
        },
    )


@app.post("/chat", response_model=ChatResponse, deprecated=True)
async def chat(req: ChatRequest):
    """
    兼容接口（已废弃）— 阻塞式调用，建议迁移到 /chat/stream。

    内部已改用 run_async() 驱动，但仍然是等全部完成后一次性返回。
    对于耗时超过 60s 的任务，网关可能会 504 超时。
    """
    if agent_instance is None:
        return ChatResponse(reply="Agent 未初始化", success=False)

    try:
        agent_instance.reset()
        final_text = ""
        async for event in agent_instance.run_async(req.message):
            if event.type == "text":
                final_text += event.content
        return ChatResponse(reply=final_text or "任务完成", success=True)
    except Exception as e:
        return ChatResponse(reply=f"Agent 执行失败: {e}", success=False)


# ─────────────────────────────────────────────
# 直接运行入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  DocMaster Agent API Server")
    print("  http://localhost:8000")
    print("  http://localhost:8000/docs  <- API 文档")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
