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

import asyncio
import json
import os
import pathlib
import sys
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
    agent_instance.api_mode = True  # 插件模式：禁止自动关闭 Word（Word 是宿主）
    tool_registry = agent_instance.tools
    agent_run_lock = asyncio.Lock()
    print(f"[OK] Agent 就绪，已加载 {len(tool_registry)} 个工具（api_mode=True: 不会关闭 Word）")

    yield  # ← 应用运行中

    print("[*] Agent 服务关闭")


app = FastAPI(
    title="DocMaster Agent API",
    description="学术论文排版 AI 智能助手 — HTTP 接口",
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
    import threading
    
    # 1. 自动向注册表写入 Word Add-in 加载项
    register_manifest_automatically()
    
    # 2. 启动后台 uvicorn 服务线程
    def start_server():
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
        
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    # 3. 弹出小型现代控制中心 GUI，提供一键关闭服务并退出的方式
    try:
        import tkinter as tk
        
        root = tk.Tk()
        root.title("DocMaster 助手")
        root.geometry("320x160")
        root.resizable(False, False)
        root.configure(bg="#1e1e2e")
        root.attributes("-topmost", True)
        
        # 居中显示窗口
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width / 2) - (320 / 2)
        y = (screen_height / 2) - (160 / 2)
        root.geometry(f"320x160+{int(x)}+{int(y)}")
        
        # UI 组件
        lbl_title = tk.Label(
            root, text="📝 DocMaster 排版精灵",
            font=("Outfit", 14, "bold"), fg="#89b4fa", bg="#1e1e2e"
        )
        lbl_title.pack(pady=15)
        
        lbl_status = tk.Label(
            root, text="● 服务已启动，后台运行中 (Port: 8000)",
            font=("Segoe UI", 10), fg="#a6e3a1", bg="#1e1e2e"
        )
        lbl_status.pack(pady=5)
        
        def on_exit():
            root.destroy()
            sys.exit(0)
            
        btn_close = tk.Button(
            root, text="停止并退出", command=on_exit,
            font=("Segoe UI", 10, "bold"), fg="#11111b", bg="#f38ba8",
            activebackground="#f38ba8", activeforeground="#11111b",
            bd=0, padx=12, pady=5, cursor="hand2"
        )
        btn_close.pack(pady=15)
        
        root.protocol("WM_DELETE_WINDOW", on_exit)
        root.mainloop()
    except Exception as e:
        print(f"[!] 无法拉起控制面板 GUI ({e})，将在命令行中阻塞运行。")
        server_thread.join()
