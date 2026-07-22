"""JSONL audit sink — append-only audit-record v1 lines on local disk.

The interface is append-only (no update, no delete, no read-back); the file is
opened in append mode on every write so the sink itself cannot rewrite history.
"""

from pathlib import Path

from agent_runtime.audit import AuditRecord


class JsonlAuditSink:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> None:
        line = record.model_dump_json()
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
