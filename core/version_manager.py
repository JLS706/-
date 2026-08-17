# -*- coding: utf-8 -*-
"""Small, file-based version store for workbench document checkpoints.

The workbench deliberately keeps versioning outside the Agent history.  A
checkpoint is a physical copy of the document plus the FSM snapshot that
explains why it was created.  This makes rollback deterministic and keeps a
failed model run from corrupting the active document.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class VersionManager:
    """Create, list and restore document checkpoints beside the source file."""

    def __init__(self, document_path: str):
        self.document_path = Path(document_path).expanduser().resolve()
        self.store_dir = self.document_path.parent / ".docmaster_versions" / self.document_path.name
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.store_dir / "index.json"

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_index(self, records: list[dict[str, Any]]) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="index_", suffix=".tmp", dir=self.store_dir)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(records, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.index_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def create_checkpoint(
        self,
        node_id: str,
        description: str,
        plan_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.document_path.exists():
            return None

        commit_id = f"v_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        snapshot_path = self.store_dir / f"{commit_id}{self.document_path.suffix}"
        shutil.copy2(self.document_path, snapshot_path)
        record = {
            "commit_id": commit_id,
            "node_id": node_id,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_path": str(snapshot_path),
            "plan_state": plan_state or {},
        }
        records = self._load_index()
        records.append(record)
        self._save_index(records)
        return record

    def get_history(self) -> list[dict[str, Any]]:
        records = self._load_index()
        return [
            {
                "commit_id": item.get("commit_id", ""),
                "node_id": item.get("node_id", ""),
                "description": item.get("description", ""),
                "created_at": item.get("created_at", ""),
            }
            for item in reversed(records)
            if item.get("commit_id")
        ]

    def rollback_to(self, commit_id: str) -> dict[str, Any]:
        records = self._load_index()
        record = next((item for item in records if item.get("commit_id") == commit_id), None)
        if not record:
            raise ValueError(f"版本不存在: {commit_id}")

        snapshot_path = Path(record.get("snapshot_path", ""))
        if not snapshot_path.exists():
            raise FileNotFoundError(f"版本快照不存在: {snapshot_path}")

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"rollback_{commit_id}_", suffix=self.document_path.suffix,
            dir=self.document_path.parent,
        )
        os.close(tmp_fd)
        try:
            shutil.copy2(snapshot_path, tmp_name)
            os.replace(tmp_name, self.document_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return record.get("plan_state", {})
