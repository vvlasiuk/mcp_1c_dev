# telemetry.py — запис статистики викликів MCP API у JSONL.
# Підключається з server.py: from telemetry import record as _tel_record
# Файл: telemetry.jsonl поруч з цим модулем.
# Ротація: кожні TRIM_EVERY записів перевіряє розмір;
#          якщо > MAX_BYTES — залишає останні MAX_LINES рядків.

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_FILE = Path(__file__).parent / "telemetry.jsonl"
_LOCK = Lock()
_MAX_LINES = 10_000
_TRIM_EVERY = 500
_MAX_BYTES = 3 * 1024 * 1024  # 3 МБ
_counter = 0


def record(endpoint: str, method: str, req_b: int, res_b: int, ms: float,
           ok: bool, err: str = "") -> None:
    """Записати один рядок телеметрії.
    Не кидає виняток — збій логування не ламає сервер."""
    global _counter
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "ep": endpoint,
        "m": method,
        "req_b": req_b,
        "res_b": res_b,
        "ms": round(ms, 1),
        "ok": ok,
        "err": err,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with _LOCK:
            with open(_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            _counter += 1
            if _counter % _TRIM_EVERY == 0:
                _maybe_trim()
    except Exception:
        pass  # телеметрія не повинна ламати сервер


def _maybe_trim() -> None:
    """Обрізати файл до MAX_LINES якщо перевищено MAX_BYTES.
    Викликається під _LOCK."""
    try:
        if not _FILE.exists() or _FILE.stat().st_size <= _MAX_BYTES:
            return
        lines = _FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > _MAX_LINES:
            _FILE.write_text("".join(lines[-_MAX_LINES:]), encoding="utf-8")
    except Exception:
        pass