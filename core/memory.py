# -*- coding: utf-8 -*-
"""
DocMaster Agent - 三级分层记忆系统 (Hierarchical Memory)

v3: 三级分层架构

  L1: Core Memory (核心规则库)
      载体: learned_rules.json（由 learned_rules 工具管理）
      特点: 永不压缩、永不过期、最高执行权重

  L2: Long-term RAG (长期向量记忆)
      载体: memory_vectors.json (type=reflection)
      来源: 子任务反思归纳 / L3 晋升
      淘汰: FIFO 上限 50 条，绝不二次压缩

  L3: Short-term Conversation (短期对话记忆)
      载体: memory_vectors.json (type=conversation)
      来源: 每轮对话的 Q+A 摘要
      淘汰: 30天 TTL + 召回率末尾淘汰
      晋升: recall_count ≥ 5 → 自动提升到 L2

  Working Memory (工作记忆):
      载体: agent.py 的 self.history（内存，非持久化）
      压缩: Token 水位线驱动（见 agent.py）

层感知语义冲突规则:
  L3 → L3: 同层替换 ✅
  L2 → L2: 同层替换 ✅
  L3 → L2: 不动 L2 ❌（低层不覆盖高层）
  L2 → L3: 清理相似 L3 🗑️（高层替代低层冗余）
"""

import json
import os
import math
from datetime import datetime
from typing import Optional

from core.logger import logger


# ─────────────────────────────────────────────
# 配置常量
# ─────────────────────────────────────────────

L3_CAPACITY = 100          # L3 最大条目数
L3_EVICT_TARGET = 80       # L3 淘汰后保留条目数
L3_TTL_DAYS = 30           # L3 过期天数
L2_CAPACITY = 50           # L2 最大条目数
CONFLICT_THRESHOLD = 0.92  # 语义冲突阈值（0.85→0.92: 短中文文本天然相似度高，调高减少误杀）
PROMOTION_THRESHOLD = 5    # L3 → L2 晋升所需召回次数

# ── 记忆清洗：移除不应被记忆的临时信息（文件路径、会话 ID 等）──
import re as _re
_PATH_PATTERN = _re.compile(
    r'[A-Za-z]:\\(?:[^\\/:*?"<>|]+\\)*[^\s\\/:*?"<>|]+\.\w{1,5}'  # Windows 绝对路径
    r'|/(?:home|Users|tmp|var|mnt)/\S+'                                # Unix 绝对路径
)


def _sanitize_for_memory(text: str) -> str:
    """
    清洗记忆文本：将绝对文件路径替换为文件名。
    
    避免路径这类会话级临时信息被存入长期记忆污染后续会话。
    保留文件名（有语义价值），去掉目录路径（无复用价值且会误导）。
    """
    def _replace(m):
        path = m.group(0)
        basename = path.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
        return basename
    return _PATH_PATTERN.sub(_replace, text)


class Memory:
    """三级分层记忆管理器"""

    def __init__(self, memory_dir: str = "memory", embed_client=None, llm=None):
        self.memory_dir = os.path.abspath(memory_dir)
        self.history_file = os.path.join(self.memory_dir, "history.json")
        self._data = self._load()

        # ── LLM 实例（用于 L2 冲突融合判断，复用外部实例避免重复创建）──
        self._llm = llm

        # ── 向量记忆 ──
        self.embed_client = embed_client
        self._vector_store = None
        self._vector_cache_path = os.path.join(self.memory_dir, "memory_vectors.json")
        self._last_recalled_l2_texts: list[str] = []  # 本轮召回的 L2 文本（用于反馈，避免索引偏移）
        self._load_vector_store()

    def _project_key(self, current_file: str = "") -> str:
        """Return the stable memory scope key for a document-backed project."""
        if not current_file:
            return ""
        try:
            return os.path.normcase(os.path.abspath(os.path.expanduser(current_file)))
        except (TypeError, ValueError):
            return ""

    def _same_project(self, metadata: dict, project_id: str) -> bool:
        """When a current document exists, keep vector memory scoped to it."""
        if not project_id:
            return True
        return metadata.get("project_id") == project_id

    def _sanitize_active_file(self, text: str, current_file: str = "") -> str:
        """
        清洗记忆/查询文本：
        1. 将绝对路径清洗为文件名本身。
        2. 将当前/最后处理的文件名（及去后缀名称）替换为通用占位符 <DOC>，消除文件名干扰。
        """
        if not text:
            return ""
        
        # 1. 清洗绝对路径
        text = _sanitize_for_memory(text)
        
        # 2. 识别并替换当前/最后处理的文件名
        active_file = current_file or self.get_last_file()
        if active_file:
            basename = os.path.basename(active_file)
            name_no_ext, _ = os.path.splitext(basename)
            
            # 从长到短替换，防止部分替换
            targets = sorted(list({basename, name_no_ext}), key=len, reverse=True)
            for target in targets:
                if len(target) > 2:  # 避免替换过短的误伤
                    escaped = _re.escape(target)
                    text = _re.sub(escaped, "<DOC>", text, flags=_re.IGNORECASE)
        return text

    def _load_vector_store(self):
        """从缓存加载向量记忆（如果存在）"""
        if self.embed_client is None:
            return
        try:
            from core.embeddings import VectorStore
            cached = VectorStore.load_cache(self._vector_cache_path)
            if cached:
                self._vector_store = cached
                # 兼容旧数据：补全缺失的 metadata 字段
                self._migrate_metadata()
            else:
                self._vector_store = VectorStore()
        except Exception:
            from core.embeddings import VectorStore
            self._vector_store = VectorStore()

    def _migrate_metadata(self):
        """兼容旧版数据：为缺少 type/recall_count/utility_score 的条目补全默认值"""
        if self._vector_store is None:
            return
        changed = False
        for i, meta in enumerate(self._vector_store.metadata):
            if "type" not in meta:
                meta["type"] = "conversation"
                changed = True
            if "recall_count" not in meta:
                meta["recall_count"] = 0
                changed = True
            if "last_recalled" not in meta:
                meta["last_recalled"] = None
                changed = True
            # L2 记忆的效用分（强化学习反馈）
            if meta.get("type") == "reflection" and "utility_score" not in meta:
                meta["utility_score"] = 1.0
                changed = True
        if changed:
            self._vector_store.save_cache(self._vector_cache_path)
            logger.debug("  [Memory] 已迁移旧版 metadata 到分层格式")

    def _load(self) -> dict:
        """从 JSON 文件加载记忆"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"sessions": [], "preferences": {}}

    def _save(self):
        """持久化保存到 JSON 文件"""
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ─────────────────────────────────────────────
    # 操作历史（不变）
    # ─────────────────────────────────────────────

    def add_session(self, file_path: str, actions: list[str], summary: str):
        """记录一次操作会话"""
        session = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "file": os.path.abspath(file_path),
            "actions": actions,
            "summary": summary,
        }
        self._data["sessions"].append(session)
        if len(self._data["sessions"]) > 50:
            self._data["sessions"] = self._data["sessions"][-50:]
        self._save()

    def set_preference(self, key: str, value):
        """保存用户偏好"""
        self._data["preferences"][key] = value
        self._save()

    def get_preference(self, key: str, default=None):
        """读取用户偏好"""
        return self._data["preferences"].get(key, default)

    def get_recent_sessions(self, n: int = 5) -> list[dict]:
        """获取最近 N 条操作记录"""
        return self._data["sessions"][-n:]

    def get_last_file(self) -> Optional[str]:
        """获取上次处理的文件路径"""
        sessions = self._data["sessions"]
        if sessions:
            return sessions[-1].get("file")
        return None

    def get_context_summary(self, recalled_context: str = "",
                            current_file: str = "") -> str:
        """
        生成记忆摘要，注入到 System Prompt 中。

        Args:
            recalled_context: 向量记忆召回的相关上下文
            current_file: 当前活跃文档路径（由调用方传入）。
                若提供，则只展示与该文档相关的历史记录，
                且不注入 last_file（避免旧路径污染新会话）。
        """
        # 按当前文档过滤历史记录
        recent = self.get_recent_sessions(5)
        if current_file:
            current_abs = os.path.abspath(current_file)
            recent = [s for s in recent if os.path.abspath(s.get("file", "")) == current_abs]
            recent = recent[-3:]  # 最多 3 条
        else:
            recent = recent[-3:]

        if not recent and not recalled_context:
            return ""

        lines = []

        if recalled_context:
            lines.append("📌 与当前问题最相关的历史对话：\n")
            lines.append(recalled_context)
            lines.append("")

        if recent:
            lines.append("最近操作记录：")
            for s in recent:
                actions_str = ", ".join(s["actions"]) if s["actions"] else "无"
                lines.append(
                    f"- [{s['time']}] 文件: {os.path.basename(s['file'])} | "
                    f"操作: {actions_str} | {s['summary']}"
                )

        # 用户偏好（不含文件路径——文件路径由调用方控制，不属于偏好）
        pref_lines = []
        for k, v in self._data["preferences"].items():
            pref_lines.append(f"- {k}: {v}")

        result = "\n".join(lines)
        if pref_lines:
            result += "\n\n用户偏好：\n" + "\n".join(pref_lines)
        return result

    # ═════════════════════════════════════════════
    # L3: Short-term Conversation (短期对话记忆)
    # ═════════════════════════════════════════════

    def add_conversation(self, user_input: str, agent_reply: str,
                         current_file: str = ""):
        """
        将一轮对话存入 L3 短期记忆（含层感知语义冲突消解）。

        冲突规则:
          - 与 L3 冲突 (>0.85): 替换旧 L3（同层覆盖）
          - 与 L2 冲突 (>0.85): 不动 L2，L3 正常存入（低层不覆盖高层）
        """
        if not self.embed_client or self._vector_store is None:
            return

        # 去掉模板化前缀，减少不同对话间 embedding 的基础相似度
        # 脱敏文件名与绝对路径 → 消除文件名串扰与路径污染
        project_id = self._project_key(current_file)
        summary = self._sanitize_active_file(
            f"{user_input[:200]} → {agent_reply[:300]}",
            current_file=current_file,
        )
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        try:
            embedding = self.embed_client.embed(summary)

            # ── 层感知语义冲突消解 ──
            if len(self._vector_store) > 0:
                from core.embeddings import cosine_similarity
                import numpy as np
                query_vec = np.array(embedding)
                for i in range(len(self._vector_store.chunks)):
                    old_meta = self._vector_store.metadata[i]
                    if not self._same_project(old_meta, project_id):
                        continue
                    score = cosine_similarity(
                        query_vec, self._vector_store.embeddings[i]
                    )
                    if score > CONFLICT_THRESHOLD:
                        old_type = old_meta.get("type", "conversation")
                        if old_type == "conversation":
                            # L3 → L3: 同层替换
                            logger.debug(
                                "  [Memory] L3 语义冲突(%.1f%%)，替换旧 L3",
                                score * 100,
                            )
                            self._vector_store.chunks[i] = summary
                            self._vector_store.embeddings[i] = query_vec
                            self._vector_store.metadata[i] = {
                                "type": "conversation",
                                "time": now_str,
                                "recall_count": 0,
                                "last_recalled": None,
                                "project_id": project_id,
                            }
                            self._vector_store.save_cache(
                                self._vector_cache_path
                            )
                            return
                        elif old_type == "reflection":
                            # L3 → L2: 不动 L2，L3 正常存入
                            logger.debug(
                                "  [Memory] L3 与 L2 语义相似(%.1f%%)，"
                                "L2 不动，L3 正常存入",
                                score * 100,
                            )
                            # 不 return，继续往下正常追加

            # 无冲突 / 跨层不覆盖 → 正常追加
            metadata = [{
                "type": "conversation",
                "time": now_str,
                "recall_count": 0,
                "last_recalled": None,
                "project_id": project_id,
            }]
            self._vector_store.add([summary], [embedding], metadata)
            self._vector_store.save_cache(self._vector_cache_path)

            # ── 淘汰检查 ──
            self._evict_expired()

        except Exception as e:
            logger.warning("  [Memory] L3 存储失败(不影响主功能): %s", e)

    def add_to_vector(self, user_input: str, agent_reply: str,
                      current_file: str = ""):
        """向后兼容：代理到 add_conversation()"""
        self.add_conversation(user_input, agent_reply, current_file=current_file)

    # ═════════════════════════════════════════════
    # L2: Long-term RAG (长期向量记忆)
    # ═════════════════════════════════════════════

    def _fuse_l2_conflict(
        self, old_text: str, new_text: str
    ) -> tuple[str, str]:
        """
        L2 冲突融合节点：判断冲突类型并执行相应策略。

        当新 L2 经验与旧 L2 经验 cosine > 0.85 时触发。
        调用 LLM 判断是"冲突覆写"还是"细节补充"：
          - REPLACE: 新旧矛盾，用新的替换旧的
          - MERGE:   新旧互补，融合为一条更完整的经验

        Args:
            old_text: 已存在的 L2 经验
            new_text: 新传入的 L2 经验

        Returns:
            (action, result_text)
            action: "replace" | "merge"
            result_text: 最终要写入的经验文本
        """
        try:
            from core.schema import Message, Role

            # 优先复用外部传入的 LLM 实例，避免每次冲突都重新读配置 + 创建客户端
            llm = self._llm
            if llm is None:
                import toml
                from core.llm import LLM
                config_path = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    "config", "config.toml",
                )
                config = toml.load(config_path)
                llm = LLM(**config.get("llm", {}))
                # 缓存以供后续调用复用
                self._llm = llm

            messages = [
                Message(role=Role.SYSTEM, content=(
                    "你是记忆融合判断器。给定一条旧经验和一条新经验，判断关系：\n"
                    "- 如果新经验与旧经验矛盾（结论相反），回复第一行: REPLACE\n"
                    "- 如果新经验是旧经验的补充（增加细节/条件），回复第一行: MERGE\n"
                    "\n"
                    "如果是 MERGE，第二行写出融合后的完整经验（不超过80字，"
                    "保留两者的关键信息）。\n"
                    "如果是 REPLACE，第二行写出新经验即可。"
                )),
                Message(role=Role.USER, content=(
                    f"旧经验: {old_text}\n新经验: {new_text}"
                )),
            ]
            response = llm.chat(messages)
            reply = (response.content or "").strip()

            lines = reply.split("\n", 1)
            action_line = lines[0].strip().upper()
            result_line = lines[1].strip() if len(lines) > 1 else new_text

            if "MERGE" in action_line and result_line:
                logger.info(
                    "  [Memory] L2 融合: '%s' + '%s' → '%s'",
                    old_text[:30], new_text[:30], result_line[:50],
                )
                return "merge", result_line
            else:
                logger.info(
                    "  [Memory] L2 覆写: '%s' → '%s'",
                    old_text[:30], new_text[:30],
                )
                return "replace", new_text

        except Exception as e:
            # LLM 不可用时，回退为简单替换（保守策略）
            logger.warning(
                "  [Memory] L2 融合判断失败(%s)，回退为替换", e
            )
            return "replace", new_text

    def add_reflection(self, experience_text: str, current_file: str = ""):
        """
        将反思经验存入 L2 长期记忆（含层感知冲突消解）。
        入库前自动清洗绝对路径，防止会话级临时路径成为永久记忆。

        冲突规则:
          - 与 L2 冲突 (>0.85): 替换旧 L2（同层覆盖）
          - 与 L3 冲突 (>0.85): 清理相似 L3（高层替代低层冗余）
        """
        if not self.embed_client or self._vector_store is None:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        project_id = self._project_key(current_file)

        try:
            experience_text = self._sanitize_active_file(
                experience_text,
                current_file=current_file,
            )
            query_embedding = self.embed_client.embed(experience_text)

            # ── 层感知冲突消解 ──
            l3_to_delete = []  # 要清理的 L3 索引
            l2_replaced = False  # 是否已替换了 L2 条目
            if len(self._vector_store) > 0:
                from core.embeddings import cosine_similarity
                import numpy as np
                query_vec = np.array(query_embedding)
                for i in range(len(self._vector_store.chunks)):
                    old_meta = self._vector_store.metadata[i]
                    if not self._same_project(old_meta, project_id):
                        continue
                    score = cosine_similarity(
                        query_vec, self._vector_store.embeddings[i]
                    )
                    if score > CONFLICT_THRESHOLD:
                        old_type = old_meta.get("type", "conversation")
                        if old_type == "reflection":
                            # L2 → L2: 融合节点判断
                            old_text = self._vector_store.chunks[i]
                            action, fused = self._fuse_l2_conflict(
                                old_text, experience_text
                            )
                            # 无论 replace 还是 merge，都更新这条
                            self._vector_store.chunks[i] = fused
                            # merge 需要重新生成 embedding
                            if action == "merge":
                                fused_emb = self.embed_client.embed(fused)
                                import numpy as np
                                self._vector_store.embeddings[i] = np.array(
                                    fused_emb
                                )
                            else:
                                self._vector_store.embeddings[i] = query_vec
                            self._vector_store.metadata[i] = {
                                "type": "reflection",
                                "time": now_str,
                                "source": "task_completion_hook",
                                "fused_from": action,
                                "utility_score": 1.0,
                                "project_id": project_id,
                            }
                            l2_replaced = True
                            break
                        elif old_type == "conversation":
                            # L2 → L3: 标记清理
                            logger.debug(
                                "  [Memory] L2 替代 L3(%.1f%%)，标记清理",
                                score * 100,
                            )
                            l3_to_delete.append(i)

                # 清理被 L2 替代的 L3 条目（从后往前删）
                if l3_to_delete:
                    self._delete_indices(l3_to_delete)

                # 如果已替换了 L2，保存并返回
                if l2_replaced:
                    self._vector_store.save_cache(self._vector_cache_path)
                    return

            # 无冲突 → 正常追加 L2
            import numpy as np
            query_vec = np.array(query_embedding)
            metadata = [{
                "type": "reflection",
                "time": now_str,
                "source": "task_completion_hook",
                "utility_score": 1.0,
                "project_id": project_id,
            }]
            self._vector_store.add(
                [experience_text], [query_embedding], metadata
            )
            self._vector_store.save_cache(self._vector_cache_path)

            # ── L2 容量控制 ──
            self._enforce_l2_cap()

        except Exception as e:
            logger.warning("  [Memory] L2 存储失败(不影响主功能): %s", e)

    # ═════════════════════════════════════════════
    # 召回（含召回追踪）
    # ═════════════════════════════════════════════

    def recall_relevant(self, query: str, top_k: int = 3,
                        min_score: float = 0.60,
                        current_file: str = "") -> str:
        """
        根据当前问题，召回最相关的历史片段（含时间衰减 + 召回追踪 + 效用分）。

        最终得分 = 0.55 × 语义相似度 + 0.25 × 时间新鲜度 + 0.20 × 效用分
        时间新鲜度: recency = exp(-days / 30)
        效用分: utility = min(max(utility_score, 0), 2) / 2  → [0, 1]

        副作用:
          - 被命中的记忆 recall_count += 1, last_recalled 更新
          - 记录本轮召回的 L2 索引，供后续 reward/penalize 使用
        """
        self._last_recalled_l2_texts = []  # 每轮重置

        if (not self.embed_client or self._vector_store is None
                or len(self._vector_store) == 0):
            return ""

        try:
            project_id = self._project_key(current_file)
            sanitized_query = self._sanitize_active_file(
                query,
                current_file=current_file,
            )
            query_embedding = self.embed_client.embed(sanitized_query)
            candidates = self._vector_store.search(
                query_embedding, top_k=max(top_k * 10, top_k * 3)
            )

            if not candidates:
                return ""

            # ── 时间衰减 + 效用分 重评分 ──
            now = datetime.now()
            scored = []
            for r in candidates:
                semantic = r["score"]
                meta = r.get("metadata", {})
                mem_type = meta.get("type", "conversation")

                if not self._same_project(meta, project_id):
                    continue

                # 过滤被隔离的 L2 记忆（utility_score < 0）
                if mem_type == "reflection":
                    us = meta.get("utility_score", 1.0)
                    if us < 0.0:
                        continue  # 已被隔离，跳过

                # 时间新鲜度
                recency = 0.5
                time_str = meta.get("time", "")
                if time_str:
                    try:
                        mem_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                        days_ago = (now - mem_time).total_seconds() / 86400
                        recency = math.exp(-days_ago / 30)
                    except ValueError:
                        pass

                # 效用分（仅 L2 有，L3 默认 1.0）
                utility_raw = meta.get("utility_score", 1.0)
                utility = min(max(utility_raw, 0.0), 2.0) / 2.0  # 归一化到 [0, 1]

                final_score = 0.55 * semantic + 0.25 * recency + 0.20 * utility
                scored.append({**r, "final_score": final_score})

            scored.sort(key=lambda x: x["final_score"], reverse=True)
            relevant = [
                r for r in scored[:top_k]
                if r["final_score"] >= min_score
            ]

            if not relevant:
                return ""

            # ── 召回追踪 + L2 索引记录 ──
            now_str = now.strftime("%Y-%m-%d %H:%M")
            tracking_changed = False
            for r in relevant:
                try:
                    idx = self._vector_store.chunks.index(r["chunk"])
                    meta = self._vector_store.metadata[idx]
                    meta["recall_count"] = meta.get("recall_count", 0) + 1
                    meta["last_recalled"] = now_str
                    tracking_changed = True

                    # 记录本轮召回的 L2 文本（用于 reward/penalize 反馈，避免索引偏移）
                    if meta.get("type") == "reflection":
                        self._last_recalled_l2_texts.append(r["chunk"])

                except (ValueError, IndexError):
                    pass

            if tracking_changed:
                self._vector_store.save_cache(self._vector_cache_path)

            # ── 晋升检查：高频召回的 L3 → L2 ──
            self._check_promotion()

            parts = []
            for i, r in enumerate(relevant, 1):
                score_pct = round(r["final_score"] * 100, 1)
                mem_type = r.get("metadata", {}).get("type", "?")
                label = "📝" if mem_type == "conversation" else "💡"
                parts.append(f"[{label} 相关度 {score_pct}%] {r['chunk']}")

            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("  [Memory] 向量召回失败(不影响主功能): %s", e)
            return ""

    # ═════════════════════════════════════════════
    # L2 效用反馈（强化学习式记忆淘汰）
    # ═════════════════════════════════════════════

    def reward_recalled_memories(self, delta: float = 0.1):
        """
        工具执行成功时奖励本轮召回的 L2 记忆。

        类似推荐系统的正反馈：当召回的经验“帮助”了 Agent 成功执行，
        就提升其效用分，使其在未来更容易被召回。
        """
        if not self._last_recalled_l2_texts or self._vector_store is None:
            return

        changed = False
        for text in self._last_recalled_l2_texts:
            # 通过文本匹配查找当前索引（避免因 _delete_indices 导致的索引偏移）
            try:
                idx = self._vector_store.chunks.index(text)
            except ValueError:
                continue  # 该记忆已被删除，跳过
            meta = self._vector_store.metadata[idx]
            if meta.get("type") == "reflection":
                old = meta.get("utility_score", 1.0)
                meta["utility_score"] = round(old + delta, 2)
                changed = True
                if self.verbose_feedback:
                    logger.debug(
                        "  [Memory] L2 奖励 +%.1f: %.2f → %.2f | %s",
                        delta, old, meta["utility_score"],
                        self._vector_store.chunks[idx][:40],
                    )

        if changed:
            self._vector_store.save_cache(self._vector_cache_path)

    def penalize_recalled_memories(self, delta: float = 0.5):
        """
        执行失败时惩罚本轮召回的 L2 记忆。

        类似推荐系统的负反馈：被召回的经验可能误导了 Agent，
        降低其效用分。一旦降到 0 以下将被隔离或删除。
        """
        if not self._last_recalled_l2_texts or self._vector_store is None:
            return

        changed = False
        for text in self._last_recalled_l2_texts:
            # 通过文本匹配查找当前索引（避免因 _delete_indices 导致的索引偏移）
            try:
                idx = self._vector_store.chunks.index(text)
            except ValueError:
                continue  # 该记忆已被删除，跳过
            meta = self._vector_store.metadata[idx]
            if meta.get("type") == "reflection":
                old = meta.get("utility_score", 1.0)
                meta["utility_score"] = round(old - delta, 2)
                changed = True
                logger.info(
                    "  [Memory] L2 惩罚 -%.1f: %.2f → %.2f | %s",
                    delta, old, meta["utility_score"],
                    self._vector_store.chunks[idx][:40],
                )

        if changed:
            self._vector_store.save_cache(self._vector_cache_path)
            self._quarantine_check()

    def _quarantine_check(self):
        """
        清理 utility_score < 0 的 L2 记忆（“毒记忆”淘汰）。

        一条记忆如果多次被召回后都伴随失败，说明它可能是"毒经验"——
        误导 Agent 采取错误操作。直接删除以永久解除威胁。
        """
        if self._vector_store is None:
            return

        quarantined = []
        for i, meta in enumerate(self._vector_store.metadata):
            if (meta.get("type") == "reflection"
                    and meta.get("utility_score", 1.0) < 0.0):
                quarantined.append(i)

        if quarantined:
            for i in quarantined:
                logger.warning(
                    "  [Memory] 🚩 淘汰毒记忆 (utility=%.2f): %s",
                    self._vector_store.metadata[i].get("utility_score", 0),
                    self._vector_store.chunks[i][:60],
                )
            self._delete_indices(quarantined)

    @property
    def verbose_feedback(self) -> bool:
        """控制效用反馈日志是否输出（奖励用 debug，惩罚用 info）"""
        return True

    # ═════════════════════════════════════════════
    # 淘汰与晋升
    # ═════════════════════════════════════════════

    def _evict_expired(self):
        """
        L3 淘汰：TTL 过期删除 + 召回率末尾淘汰。

        触发时机：每次 add_conversation() 后。
        策略：
          1. TTL: age > 30天 且 recall_count == 0 → 直接删除
          2. 容量: L3 条目 > L3_CAPACITY → 按 eviction_score 淘汰至 L3_EVICT_TARGET
             eviction_score = recall_count × exp(-days_since_last_recall / 30)
        """
        if self._vector_store is None or len(self._vector_store) == 0:
            return

        now = datetime.now()
        to_delete = []

        # ── Phase 1: TTL 清理 ──
        for i, meta in enumerate(self._vector_store.metadata):
            if meta.get("type") != "conversation":
                continue
            time_str = meta.get("time", "")
            if not time_str:
                continue
            try:
                mem_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                age_days = (now - mem_time).total_seconds() / 86400
                if age_days > L3_TTL_DAYS and meta.get("recall_count", 0) == 0:
                    to_delete.append(i)
            except ValueError:
                pass

        if to_delete:
            logger.debug(
                "  [Memory] TTL 淘汰 %d 条过期 L3 记忆", len(to_delete)
            )
            self._delete_indices(to_delete)

        # ── Phase 2: 容量淘汰（召回率末尾淘汰）──
        l3_indices = [
            i for i, m in enumerate(self._vector_store.metadata)
            if m.get("type") == "conversation"
        ]

        if len(l3_indices) <= L3_CAPACITY:
            return

        # 计算 eviction_score
        eviction_scores = []
        for i in l3_indices:
            meta = self._vector_store.metadata[i]
            recall_count = meta.get("recall_count", 0)
            last_recalled = meta.get("last_recalled")

            if last_recalled and recall_count > 0:
                try:
                    lr_time = datetime.strptime(last_recalled, "%Y-%m-%d %H:%M")
                    days_since = (now - lr_time).total_seconds() / 86400
                    recency = math.exp(-days_since / 30)
                except ValueError:
                    recency = 0.1
            else:
                recency = 0.01  # 从未被召回

            score = recall_count * recency
            eviction_scores.append((i, score))

        # 按 eviction_score 升序排，淘汰得分最低的
        eviction_scores.sort(key=lambda x: x[1])
        n_to_evict = len(l3_indices) - L3_EVICT_TARGET
        evict_indices = [idx for idx, _ in eviction_scores[:n_to_evict]]

        if evict_indices:
            logger.info(
                "  [Memory] 召回率淘汰 %d 条 L3 记忆（容量 %d → %d）",
                len(evict_indices), len(l3_indices), L3_EVICT_TARGET,
            )
            self._delete_indices(evict_indices)

    def _check_promotion(self):
        """
        L3 → L2 晋升检查。

        如果某条 L3 记忆的 recall_count >= PROMOTION_THRESHOLD，
        将其内容存入 L2（标记 source=promotion），然后从 L3 删除。
        """
        if self._vector_store is None:
            return

        promoted = []
        for i, meta in enumerate(self._vector_store.metadata):
            if (meta.get("type") == "conversation"
                    and meta.get("recall_count", 0) >= PROMOTION_THRESHOLD):
                promoted.append(i)

        if not promoted:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        for i in promoted:
            chunk = self._vector_store.chunks[i]
            old_meta = self._vector_store.metadata[i]
            # 👑 先读后写（两步）：虽然 Python 先求值右侧再赋值，
            # 把旧值查询写在新 dict literal 里语义正确但极易误导 code review，
            # 故显式拆成两步以免未来维护者误判为"读已被覆盖的值"。
            old_recall = old_meta.get("recall_count", 0)
            logger.info("  [Memory] L3 → L2 晋升（recall=%d）: %s",
                        old_recall, chunk[:60])
            # 将 L3 直接升级为 L2（原地修改 metadata）
            self._vector_store.metadata[i] = {
                "type": "reflection",
                "time": now_str,
                "source": "promotion",
                "original_recall_count": old_recall,
                "utility_score": 1.0,
                "project_id": old_meta.get("project_id", ""),
            }

        self._vector_store.save_cache(self._vector_cache_path)
        self._enforce_l2_cap()

    def _enforce_l2_cap(self):
        """L2 容量控制：FIFO 删除最旧的，绝不二次压缩。"""
        if self._vector_store is None:
            return

        l2_indices = [
            i for i, m in enumerate(self._vector_store.metadata)
            if m.get("type") == "reflection"
        ]

        if len(l2_indices) <= L2_CAPACITY:
            return

        # 按 time 排序，删除最旧的
        def get_time(idx):
            t = self._vector_store.metadata[idx].get("time", "")
            try:
                return datetime.strptime(t, "%Y-%m-%d %H:%M")
            except ValueError:
                return datetime.min

        l2_indices.sort(key=get_time)
        n_to_delete = len(l2_indices) - L2_CAPACITY
        to_delete = l2_indices[:n_to_delete]

        logger.info(
            "  [Memory] L2 FIFO 淘汰 %d 条最旧的长期记忆", n_to_delete
        )
        self._delete_indices(to_delete)

    # ═════════════════════════════════════════════
    # 内部工具方法
    # ═════════════════════════════════════════════

    def _delete_indices(self, indices: list[int]):
        """从 vector_store 中删除指定索引的条目（统一重建，避免索引偏移）。"""
        if not indices or self._vector_store is None:
            return
        import numpy as np

        to_delete = set(indices)
        total = len(self._vector_store.chunks)
        keep = sorted(set(range(total)) - to_delete)

        # 统一重建所有三个数据结构
        self._vector_store.chunks = [self._vector_store.chunks[i] for i in keep]
        self._vector_store.metadata = [self._vector_store.metadata[i] for i in keep]

        if keep and self._vector_store.embeddings.size > 0:
            self._vector_store.embeddings = self._vector_store.embeddings[keep]
        else:
            self._vector_store.embeddings = np.array([])

        self._vector_store.save_cache(self._vector_cache_path)

    def get_memory_stats(self) -> dict:
        """返回记忆系统统计信息（用于调试和监控）。"""
        if self._vector_store is None:
            return {"total": 0, "l2_reflection": 0, "l3_conversation": 0}

        l2 = sum(
            1 for m in self._vector_store.metadata
            if m.get("type") == "reflection"
        )
        l3 = sum(
            1 for m in self._vector_store.metadata
            if m.get("type") == "conversation"
        )
        return {
            "total": len(self._vector_store),
            "l2_reflection": l2,
            "l3_conversation": l3,
            "sessions": len(self._data.get("sessions", [])),
        }
