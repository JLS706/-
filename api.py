# -*- coding: utf-8 -*-
"""
PaperOps — FastAPI Web 接口层（证据驱动科研写作工作台）

将命令行 Agent 包装为 HTTP 服务，支持 Server-Sent Events 实时推送。

启动方式:
    python api.py

接口:
    POST /chat/stream  — SSE 流式 Agent 事件
    GET  /api/projects  — 论文项目列表
    POST /api/projects/{id}/tasks — 启动科研写作任务
    GET  /health       — 健康检查
    GET  /tools        — 查看可用工具列表
"""

import asyncio
import json
import os
import pathlib
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.agent import Agent
from core.logger import logger
from tools.base import ToolRegistry


# ─────────────────────────────────────────────
# 全局单例（在 lifespan 中初始化）
# ─────────────────────────────────────────────

agent_instance: Agent | None = None
tool_registry: ToolRegistry | None = None
agent_run_lock: asyncio.Lock | None = None

# 论文交付工作台的轻量持久化状态。第一版使用 JSON，后续可平滑替换为
# SQLite/PostgreSQL；任务状态和产物仍然通过稳定的 task_id 访问。
_FRONTEND_DIR = os.path.join(_PROJECT_ROOT, "frontend")
_RUNTIME_DIR = os.path.join(_PROJECT_ROOT, "runtime")
_WORKBENCH_STATE_PATH = os.path.join(_RUNTIME_DIR, "workbench_state.json")
_workbench_state: dict = {"projects": {}, "tasks": {}}


def _load_workbench_state() -> None:
    global _workbench_state
    try:
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        if os.path.exists(_WORKBENCH_STATE_PATH):
            with open(_WORKBENCH_STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                _workbench_state = {
                    "projects": data.get("projects", {}),
                    "tasks": data.get("tasks", {}),
                }
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[Workbench] 状态加载失败，将使用空状态: %s", exc)


def _save_workbench_state() -> None:
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    tmp_path = f"{_WORKBENCH_STATE_PATH}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(_workbench_state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _WORKBENCH_STATE_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _project_snapshot(project: dict) -> dict:
    return dict(project)


def _task_snapshot(task: dict) -> dict:
    result = dict(task)
    result["modules"] = [dict(module) for module in task.get("modules", [])]
    result["events"] = list(task.get("events", []))[-80:]
    return result


def _update_task(task_id: str, **fields) -> dict | None:
    task = _workbench_state["tasks"].get(task_id)
    if not task:
        return None
    task.update(fields)
    task["updated_at"] = _now()
    _save_workbench_state()
    return task


def _append_task_event(task_id: str, message: str, event_type: str = "info") -> None:
    task = _workbench_state["tasks"].get(task_id)
    if not task:
        return
    task.setdefault("events", []).append({
        "time": _now(),
        "type": event_type,
        "message": message,
    })
    task["events"] = task["events"][-80:]
    task["updated_at"] = _now()
    _save_workbench_state()


def _set_module_status(task: dict, module_id: str, status: str, summary: str = "") -> None:
    for module in task.get("modules", []):
        if module.get("id") == module_id:
            module["status"] = status
            if summary:
                module["summary"] = summary
            module["updated_at"] = _now()
            break


def _module_for_tool(tool_name: str) -> str:
    tool_name = tool_name.lower()
    if "citation" in tool_name or "claim" in tool_name:
        return "citation_check"
    if "literature" in tool_name or "search" in tool_name or "index" in tool_name:
        return "evidence_map"
    if "summar" in tool_name or "analy" in tool_name or "read" in tool_name:
        return "outline"
    if "format" in tool_name or "reference" in tool_name or "crossref" in tool_name:
        return "export"
    return "section_draft"


_load_workbench_state()


def _agent_is_busy() -> bool:
    return agent_run_lock is not None and agent_run_lock.locked()


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
    global agent_instance, tool_registry, agent_run_lock

    from main import load_config, create_agent

    print("[*] 正在初始化 Agent（复用 main.py 完整初始化逻辑）...")
    config = load_config()
    agent_instance = create_agent(config)
    agent_instance.api_mode = True  # Web 工作台模式：由服务统一管理任务生命周期
    tool_registry = agent_instance.tools
    agent_run_lock = asyncio.Lock()
    print(f"[OK] Agent 就绪，已加载 {len(tool_registry)} 个工具（api_mode=True: 不会关闭 Word）")

    yield  # ← 应用运行中

    print("[*] Agent 服务关闭")


app = FastAPI(
    title="PaperOps API",
    description="证据驱动科研写作工作台 — HTTP 接口",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS：允许 Word Add-in WebView 跨域调用（限制为本地来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://localhost", "https://127.0.0.1", "http://localhost", "http://127.0.0.1", "null"],
    allow_origin_regex=r"https?://localhost(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
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


class ActionRequest(BaseModel):
    """用户交互动作请求"""
    action: str


class RollbackRequest(BaseModel):
    """回滚请求"""
    commit_id: str


class ToolInfo(BaseModel):
    """工具信息"""
    name: str
    description: str
    parameters: list[str]


class ProjectCreateRequest(BaseModel):
    """创建一个科研写作项目。"""
    title: str = "未命名科研项目"
    research_question: str = ""
    method_notes: str = ""
    experiment_notes: str = ""
    target_venue: str = ""
    document_path: str = ""
    literature_folder: str = ""
    format_rule: str = "通用学术论文规范"


class ProjectUpdateRequest(BaseModel):
    """更新科研写作项目资料。"""
    title: str | None = None
    research_question: str | None = None
    method_notes: str | None = None
    experiment_notes: str | None = None
    target_venue: str | None = None
    document_path: str | None = None
    literature_folder: str | None = None
    format_rule: str | None = None


class TaskCreateRequest(BaseModel):
    """启动一次科研写作任务。"""
    instruction: str = "根据研究素材生成论文大纲和证据地图，并起草引言或相关工作。"


class WorkbenchActionRequest(BaseModel):
    action: str


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


# ─────────────────────────────────────────────
# 科研写作工作台：项目 / 任务 / 模块状态
# ─────────────────────────────────────────────

_WORKBENCH_MODULES = [
    ("research_brief", "研究素材整理"),
    ("outline", "论文大纲"),
    ("evidence_map", "证据地图"),
    ("section_draft", "分章节草稿"),
    ("citation_check", "引用核验"),
    ("export", "编辑与导出"),
]


async def _run_workbench_task(task_id: str, project_id: str, instruction: str) -> None:
    """Run one project task and persist progress after every observable event."""
    global agent_instance, agent_run_lock
    task = _workbench_state["tasks"].get(task_id)
    project = _workbench_state["projects"].get(project_id)
    if not task or not project:
        return

    if agent_instance is None or agent_run_lock is None:
        _update_task(task_id, status="error", progress=100, message="Agent 尚未初始化，请先启动服务")
        _append_task_event(task_id, "Agent 尚未初始化", "error")
        return

    try:
        await asyncio.wait_for(agent_run_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        _update_task(task_id, status="error", progress=100, message="已有任务正在运行，请稍后重试")
        _append_task_event(task_id, "任务未启动：已有任务正在运行", "error")
        return

    try:
        document_path = project.get("document_path", "")
        literature_folder = project.get("literature_folder", "")
        agent_instance._session_file = document_path
        agent_instance._session_literature_folder = literature_folder
        agent_instance.reset()
        agent_instance._session_file = document_path
        agent_instance._session_literature_folder = literature_folder

        _update_task(task_id, status="running", progress=4, active_module="research_brief", message="正在整理研究素材")
        _set_module_status(task, "research_brief", "running", "正在读取研究问题、方法和实验素材")
        _append_task_event(task_id, "任务已启动，进入研究素材整理阶段", "start")

        prompt = (
            "你是证据驱动科研写作工作台的执行 Agent。请基于用户提供的研究素材，"
            "先组织论文结构和证据地图，再按用户要求生成指定章节的可编辑草稿。"
            "研究结果、数据和结论不得凭空编造；缺少素材时要明确标记待补信息。"
            "优先使用已有文档分析、RAG、摘要和引用核验工具。输出中区分大纲、证据、草稿和待确认项。\n\n"
            f"研究问题: {project.get('research_question', '') or '未提供'}\n"
            f"方法素材: {project.get('method_notes', '') or '未提供'}\n"
            f"实验素材: {project.get('experiment_notes', '') or '未提供'}\n"
            f"目标期刊/学校: {project.get('target_venue', '') or '未指定'}\n"
            f"论文文件: {document_path or '未提供，请先说明需要文件'}\n"
            f"参考文献目录: {literature_folder or '未提供'}\n"
            f"格式规范: {project.get('format_rule', '通用学术论文规范')}\n"
            f"用户任务: {instruction}"
        )

        output_parts: list[str] = []
        event_count = 0
        async for event in agent_instance.run_async(prompt):
            event_count += 1
            event_type = getattr(event, "type", "info")
            content = str(getattr(event, "content", "") or "")
            metadata = getattr(event, "metadata", {}) or {}
            if event_type == "text":
                output_parts.append(content)
            elif event_type == "tool_start":
                tool_name = str(metadata.get("tool", ""))
                module_id = _module_for_tool(tool_name)
                for module in task.get("modules", []):
                    if module.get("status") == "running" and module.get("id") != module_id:
                        module["status"] = "completed"
                _set_module_status(task, module_id, "running", content or f"正在执行 {tool_name}")
                _update_task(
                    task_id,
                    active_module=module_id,
                    progress=min(92, 8 + event_count),
                    message=content or f"正在执行 {tool_name}",
                )
                _append_task_event(task_id, content or f"开始执行 {tool_name}", "tool_start")
            elif event_type == "tool_progress":
                _update_task(task_id, progress=min(95, max(8, 8 + event_count)), message=content)
            elif event_type == "error":
                _append_task_event(task_id, content or "Agent 返回错误", "error")
            elif event_type == "finish":
                _append_task_event(task_id, content or "Agent 执行完成", "finish")

        for module in task.get("modules", []):
            if module.get("status") == "running":
                module["status"] = "completed"
        _update_task(
            task_id,
            status="completed",
            progress=100,
            active_module="export",
            message="审阅任务完成，可查看报告并继续发起修改",
            output="".join(output_parts)[-20000:],
        )
        project["last_task_id"] = task_id
        project["status"] = "ready"
        project["updated_at"] = _now()
        _save_workbench_state()
    except Exception as exc:
        logger.exception("[Workbench] task failed: %s", task_id)
        _update_task(task_id, status="error", progress=100, message=str(exc))
        _append_task_event(task_id, f"任务失败: {exc}", "error")
        project["status"] = "error"
        project["updated_at"] = _now()
        _save_workbench_state()
    finally:
        if agent_run_lock.locked():
            agent_run_lock.release()


@app.get("/api/projects")
def list_workbench_projects():
    projects = sorted(
        _workbench_state["projects"].values(),
        key=lambda item: item.get("updated_at", ""),
        reverse=True,
    )
    return {"projects": [_project_snapshot(project) for project in projects]}


@app.post("/api/projects")
def create_workbench_project(req: ProjectCreateRequest):
    project_id = f"p_{uuid.uuid4().hex[:12]}"
    now = _now()
    project = {
        "id": project_id,
        "title": req.title.strip() or "未命名科研项目",
        "research_question": req.research_question.strip(),
        "method_notes": req.method_notes.strip(),
        "experiment_notes": req.experiment_notes.strip(),
        "target_venue": req.target_venue.strip(),
        "document_path": req.document_path.strip(),
        "literature_folder": req.literature_folder.strip(),
        "format_rule": req.format_rule.strip() or "通用学术论文规范",
        "status": "draft",
        "last_task_id": "",
        "created_at": now,
        "updated_at": now,
    }
    _workbench_state["projects"][project_id] = project
    _save_workbench_state()
    return {"success": True, "project": _project_snapshot(project)}


@app.get("/api/projects/{project_id}")
def get_workbench_project(project_id: str):
    project = _workbench_state["projects"].get(project_id)
    if not project:
        return {"success": False, "error": "项目不存在"}
    tasks = [
        _task_snapshot(task)
        for task in _workbench_state["tasks"].values()
        if task.get("project_id") == project_id
    ]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"success": True, "project": _project_snapshot(project), "tasks": tasks[:20]}


@app.patch("/api/projects/{project_id}")
def update_workbench_project(project_id: str, req: ProjectUpdateRequest):
    project = _workbench_state["projects"].get(project_id)
    if not project:
        return {"success": False, "error": "项目不存在"}
    payload = req.model_dump(exclude_unset=True) if hasattr(req, "model_dump") else req.dict(exclude_unset=True)
    for key, value in payload.items():
        if value is not None:
            project[key] = value.strip() if isinstance(value, str) else value
    project["updated_at"] = _now()
    _save_workbench_state()
    return {"success": True, "project": _project_snapshot(project)}


@app.post("/api/projects/{project_id}/tasks")
async def create_workbench_task(project_id: str, req: TaskCreateRequest):
    project = _workbench_state["projects"].get(project_id)
    if not project:
        return {"success": False, "error": "项目不存在"}
    running = any(
        task.get("project_id") == project_id and task.get("status") == "running"
        for task in _workbench_state["tasks"].values()
    )
    if running:
        return {"success": False, "error": "该项目已有任务正在运行"}

    task_id = f"t_{uuid.uuid4().hex[:12]}"
    now = _now()
    task = {
        "id": task_id,
        "project_id": project_id,
        "instruction": req.instruction.strip() or "根据研究素材生成论文大纲和证据地图，并起草引言或相关工作",
        "status": "queued",
        "progress": 0,
        "active_module": "research_brief",
        "message": "任务排队中",
        "output": "",
        "events": [],
        "modules": [
            {"id": module_id, "label": label, "status": "waiting", "summary": "等待执行"}
            for module_id, label in _WORKBENCH_MODULES
        ],
        "created_at": now,
        "updated_at": now,
    }
    _workbench_state["tasks"][task_id] = task
    project["status"] = "processing"
    project["last_task_id"] = task_id
    project["updated_at"] = now
    _save_workbench_state()
    asyncio.create_task(_run_workbench_task(task_id, project_id, task["instruction"]))
    return {"success": True, "task": _task_snapshot(task)}


@app.get("/api/tasks/{task_id}")
def get_workbench_task(task_id: str):
    task = _workbench_state["tasks"].get(task_id)
    if not task:
        return {"success": False, "error": "任务不存在"}
    return {"success": True, "task": _task_snapshot(task)}


@app.post("/api/tasks/{task_id}/action")
async def workbench_task_action(task_id: str, req: WorkbenchActionRequest):
    task = _workbench_state["tasks"].get(task_id)
    if not task:
        return {"success": False, "error": "任务不存在"}
    action = req.action.strip().lower()
    if action not in {"approve", "reject", "rollback", "keep"}:
        return {"success": False, "error": "不支持的任务动作"}
    if agent_instance is not None:
        agent_instance.resume_fsm(action)
    _append_task_event(task_id, f"用户动作: {action}", "action")
    return {"success": True, "action": action}


@app.get("/", include_in_schema=False)
def workbench_home():
    """论文交付工作台首页。"""
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/workbench-static", StaticFiles(directory=_FRONTEND_DIR), name="workbench-static")


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
        if agent_run_lock is None:
            payload = json.dumps({
                "type": "error",
                "content": "Agent 运行锁未初始化",
                "metadata": {},
            }, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            return
        try:
            await asyncio.wait_for(agent_run_lock.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            payload = json.dumps({
                "type": "error",
                "content": "已有任务正在运行，请等待当前任务结束或先中断后再重试。",
                "metadata": {"busy": True},
            }, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            yield f"event: finish\ndata: {json.dumps({'type': 'finish', 'content': '请求已拒绝：Agent 忙碌中。', 'metadata': {}}, ensure_ascii=False)}\n\n"
            return

        try:
            agent_instance.reset()
            # 如果前端传入了参考文献文件夹，注入到 agent 会话配置
            if req.literature_folder and req.literature_folder.strip():
                agent_instance._session_literature_folder = req.literature_folder.strip()
                agent_instance._inject_literature_folder()
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
        finally:
            agent_run_lock.release()

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
    if agent_run_lock is None:
        return ChatResponse(reply="Agent 运行锁未初始化", success=False)
    try:
        await asyncio.wait_for(agent_run_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        return ChatResponse(reply="已有任务正在运行，请稍后重试。", success=False)

    try:
        agent_instance.reset()
        final_text = ""
        async for event in agent_instance.run_async(req.message):
            if event.type == "text":
                final_text += event.content
        return ChatResponse(reply=final_text or "任务完成", success=True)
    except Exception as e:
        return ChatResponse(reply=f"Agent 执行失败: {e}", success=False)
    finally:
        agent_run_lock.release()


@app.post("/chat/action")
async def chat_action(req: ActionRequest):
    """
    接收用户确认或回退等交互信号，唤醒 FSM
    """
    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    agent_instance.resume_fsm(req.action)
    return {"success": True}


# ─────────────────────────────────────────────
# Word Add-in 侧边栏：会话 & 文件浏览
# ─────────────────────────────────────────────

# 文件浏览允许的扩展名
_BROWSE_EXTENSIONS = {
    ".docx", ".doc", ".pdf", ".txt", ".md",
    ".tex", ".bib", ".xlsx", ".pptx", ".rtf",
}


class SetPathRequest(BaseModel):
    """路径设置请求"""
    path: str


@app.get("/session/info")
def session_info():
    """获取当前会话状态（活跃文档、参考文献文件夹）"""
    if agent_instance is None:
        return {"document": "", "literature_folder": "", "status": "not_ready"}
    return {
        "document": getattr(agent_instance, "_session_file", "") or "",
        "literature_folder": getattr(agent_instance, "_session_literature_folder", "") or "",
        "status": agent_instance.state.value,
    }


@app.post("/session/document")
def set_document(req: SetPathRequest):
    """设置当前活跃文档路径（切换文档时自动重置会话）"""
    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    if _agent_is_busy():
        return {"success": False, "error": "Agent 正在执行任务，请等待结束后再切换文档"}
    old_doc = getattr(agent_instance, "_session_file", "") or ""
    doc_changed = req.path != old_doc
    # 文档切换 → 重置 Agent 状态（独立项目/会话）
    if doc_changed:
        agent_instance.reset()
        agent_instance._session_literature_folder = ""
    agent_instance._session_file = req.path
    return {"success": True, "document": req.path, "session_reset": doc_changed}


@app.post("/session/literature")
def set_literature(req: SetPathRequest):
    """设置参考文献文件夹路径"""
    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    if _agent_is_busy():
        return {"success": False, "error": "Agent 正在执行任务，请等待结束后再修改参考文献文件夹"}
    agent_instance._session_literature_folder = req.path
    return {"success": True, "literature_folder": req.path}


@app.get("/session/versions")
def get_versions():
    """获取当前活跃文档的历史保存点列表"""
    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    
    doc_path = getattr(agent_instance, "_session_file", "")
    if not doc_path or not os.path.exists(doc_path):
        return {"success": True, "versions": [], "error": "未载入活跃文档或文档不存在"}
        
    try:
        from core.version_manager import VersionManager
        version_manager = VersionManager(doc_path)
        history = version_manager.get_history()
        return {"success": True, "versions": history}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/session/rollback")
async def rollback_version(req: RollbackRequest):
    """回滚当前活跃文档至指定检查点"""
    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    if _agent_is_busy():
        return {"success": False, "error": "Agent 正在执行任务，请等待结束后再回滚"}
        
    doc_path = getattr(agent_instance, "_session_file", "")
    if not doc_path or not os.path.exists(doc_path):
        return {"success": False, "error": "当前没有活跃文档，无法回滚"}
        
    try:
        from core.version_manager import VersionManager
        version_manager = VersionManager(doc_path)
        
        # 回滚物理文档
        plan_state = version_manager.rollback_to(req.commit_id)
        
        # 重置 Agent 的 FSM 状态与对话历史
        agent_instance.reset()
        
        return {"success": True, "message": f"成功回滚至版本: {req.commit_id}", "plan_state": plan_state}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/session/select_literature_folder")
async def select_literature_folder():
    """
    弹出系统原生文件夹选择框，选择参考文献文件夹。
    """
    import asyncio

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            
            root = tk.Tk()
            root.withdraw()
            # 置顶弹窗，防止被 Word 或其他窗口遮挡
            root.attributes("-topmost", True)
            
            path = filedialog.askdirectory(title="选择参考文献文件夹")
            root.destroy()
            return path
        except Exception as e:
            logger.error(f"Tkinter dialog error: {e}")
            return None

    if agent_instance is None:
        return {"success": False, "error": "Agent 未初始化"}
    if _agent_is_busy():
        return {"success": False, "error": "Agent 正在执行任务，请等待结束后再选择参考文献文件夹"}

    try:
        path = await asyncio.to_thread(_pick)
        if path:
            path = os.path.abspath(path)
            agent_instance._session_literature_folder = path
            return {"success": True, "path": path}
        return {"success": False, "error": "用户取消选择"}
    except Exception as e:
        logger.error(f"弹出原生文件夹选择框失败: {e}")
        return {"success": False, "error": str(e)}


@app.get("/files/browse")
def browse_files(dir: str = ""):
    """
    浏览指定目录下的文件（仅返回学术相关格式）。
    用于侧边栏文件树展示参考文献文件夹内容。
    """
    p = pathlib.Path(dir)
    if not dir or not p.is_dir():
        return {"items": [], "error": "目录不存在或路径为空"}

    # 路径遍历防护：只允许访问用户主目录或项目目录下的路径
    resolved = p.resolve()
    allowed_bases = [
        pathlib.Path(os.path.expanduser("~")).resolve(),
        pathlib.Path(_PROJECT_ROOT).resolve(),
    ]
    if not any(str(resolved).startswith(str(base)) for base in allowed_bases):
        return {"items": [], "error": "路径不在允许的访问范围内"}

    items = []
    try:
        for child in sorted(p.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "type": "directory",
                })
            elif child.suffix.lower() in _BROWSE_EXTENSIONS:
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "type": "file",
                    "size": child.stat().st_size,
                    "ext": child.suffix.lower(),
                })
    except PermissionError:
        return {"items": [], "error": "权限不足"}

    return {"items": items}


# ─────────────────────────────────────────────
# Word Add-in 前端静态文件服务
# ─────────────────────────────────────────────

_ADDIN_DIR = os.path.join(_PROJECT_ROOT, "addin")


@app.get("/taskpane.html")
async def serve_taskpane():
    """Word Add-in 侧边栏入口页"""
    return FileResponse(
        os.path.join(_ADDIN_DIR, "taskpane.html"),
        media_type="text/html",
    )


# 挂载静态资源（CSS/JS）
if os.path.isdir(_ADDIN_DIR):
    app.mount("/static", StaticFiles(directory=_ADDIN_DIR), name="static")


# ─────────────────────────────────────────────
# 自动向 Windows 注册表写入本 Word Add-in 的 manifest.xml 路径
# ─────────────────────────────────────────────

def register_manifest_automatically():
    """
    自动向 Windows 注册表写入本 Word Add-in 的 manifest.xml 路径。
    实现“双击启动后端，自动注册 Word 插件”的零配置开发体验。
    """
    import platform
    if platform.system() != "Windows":
        return

    try:
        import winreg
        manifest_path = os.path.abspath(os.path.join(_PROJECT_ROOT, "addin", "manifest.xml"))
        if not os.path.exists(manifest_path):
            print(f"[!] 未找到 manifest.xml，跳过自动注册: {manifest_path}")
            return

        # 写入 HKEY_CURRENT_USER\Software\Microsoft\Office\16.0\WEF\Developer
        reg_path = r"Software\Microsoft\Office\16.0\WEF\Developer"
        
        # 创建/打开键
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE)
        try:
            # 写入键值，以 DocMaster-Addin 命名，指向本地 manifest.xml 路径
            winreg.SetValueEx(key, "DocMaster-Addin", 0, winreg.REG_SZ, manifest_path)
        finally:
            winreg.CloseKey(key)
        print(f"[OK] Word Add-in 已成功自动注册至 Office 信任开发项目，路径: {manifest_path}")
    except Exception as e:
        print(f"[!] 自动注册 manifest 失败 (非致命): {e}")


# ─────────────────────────────────────────────
# 直接运行入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # PaperOps 使用浏览器工作台，不再注册 Word Add-in，也不弹出旧版 Tk 控制面板。
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
