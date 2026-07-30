"""
Fatmetal: русский фасад над каталогом n8n-сценариев.

Тонкая надстройка над апстрим-кодом (api_server.py / workflow_db.py его не знают).
Держит ОТДЕЛЬНУЮ таблицу workflow_ru в той же SQLite. Отдельную - потому что
reindex делает INSERT OR REPLACE INTO workflows и затёр бы ru-поля, будь они в
основной таблице. Фасад подмешивается в ответ API по filename (LEFT JOIN семантика).

Поля ru_title / ru_description генерирует агент (agents-vm) и пишет через
POST /api/workflows/{filename}/ru (см. router ниже). До генерации API отдаёт
ru_* = null, фронт показывает англ. name/description.
"""
import os
import sqlite3
from typing import Optional, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

DB_PATH = os.environ.get("WORKFLOW_DB_PATH", "database/workflows.db")

# Токен на запись фасада. Задаётся env FACADE_WRITE_TOKEN на VM; агент шлёт его
# в заголовке. Чтение фасада публичное (идёт в общий ответ), запись - защищённая.
WRITE_TOKEN = os.environ.get("FACADE_WRITE_TOKEN", "")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_facade_table() -> None:
    """Создать таблицу workflow_ru, если её нет. Вызывается при старте."""
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_ru (
                filename       TEXT PRIMARY KEY,
                ru_title       TEXT,
                ru_description TEXT,
                source_hash    TEXT,
                generated_at   TEXT DEFAULT (datetime('now')),
                model          TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_ru_filename ON workflow_ru(filename)")


# --- Чтение: подмешивание в ответ основного API ---

def get_ru(filename: str) -> Optional[Dict[str, Optional[str]]]:
    """Вернуть {ru_title, ru_description} для одного файла или None."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT ru_title, ru_description FROM workflow_ru WHERE filename = ?",
                (filename,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def enrich_one(d: dict) -> dict:
    """Добавить ru_title/ru_description в dict одного воркфлоу по его filename."""
    fn = d.get("filename")
    ru = get_ru(fn) if fn else None
    d["ru_title"] = ru["ru_title"] if ru else None
    d["ru_description"] = ru["ru_description"] if ru else None
    return d


def enrich_many(items) -> list:
    """Батч-обогащение списка воркфлоу одним запросом (для списков каталога)."""
    filenames = [i.get("filename") for i in items if i.get("filename")]
    if not filenames:
        for i in items:
            i["ru_title"] = None
            i["ru_description"] = None
        return items
    try:
        placeholders = ",".join("?" * len(filenames))
        with _conn() as c:
            rows = c.execute(
                f"SELECT filename, ru_title, ru_description FROM workflow_ru "
                f"WHERE filename IN ({placeholders})",
                filenames,
            ).fetchall()
        ru_map = {r["filename"]: r for r in rows}
    except Exception:
        ru_map = {}
    for i in items:
        r = ru_map.get(i.get("filename"))
        i["ru_title"] = r["ru_title"] if r else None
        i["ru_description"] = r["ru_description"] if r else None
    return items


# --- Запись: эндпоинт для агента-генератора ---

router = APIRouter()


class RuFacadeIn(BaseModel):
    ru_title: str
    ru_description: str
    source_hash: Optional[str] = None
    model: Optional[str] = None


class FacadeStats(BaseModel):
    total_ru: int


def _check_token(request: Request) -> None:
    if not WRITE_TOKEN:
        raise HTTPException(status_code=503, detail="Facade write token not configured")
    sent = request.headers.get("x-facade-token", "")
    if sent != WRITE_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid facade token")


@router.post("/api/workflows/{filename}/ru")
async def upsert_ru(filename: str, body: RuFacadeIn, request: Request):
    """Записать/обновить русский фасад для сценария. Требует X-Facade-Token."""
    _check_token(request)
    try:
        with _conn() as c:
            c.execute(
                """
                INSERT INTO workflow_ru (filename, ru_title, ru_description, source_hash, model, generated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(filename) DO UPDATE SET
                    ru_title=excluded.ru_title,
                    ru_description=excluded.ru_description,
                    source_hash=excluded.source_hash,
                    model=excluded.model,
                    generated_at=datetime('now')
                """,
                (filename, body.ru_title, body.ru_description, body.source_hash, body.model),
            )
        return {"ok": True, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Facade write failed: {e}")


@router.get("/api/facade/stats", response_model=FacadeStats)
async def facade_stats():
    """Сколько сценариев уже имеют русский фасад (для мониторинга генерации)."""
    try:
        with _conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM workflow_ru").fetchone()["n"]
        return FacadeStats(total_ru=n)
    except Exception:
        return FacadeStats(total_ru=0)


@router.get("/api/facade/pending")
async def facade_pending(limit: int = 50):
    """Список filename БЕЗ фасада (для агента: что ещё сгенерировать)."""
    try:
        with _conn() as c:
            rows = c.execute(
                """
                SELECT w.filename, w.name, w.description, w.trigger_type,
                       w.complexity, w.node_count, w.integrations
                FROM workflows w
                LEFT JOIN workflow_ru r ON r.filename = w.filename
                WHERE r.filename IS NULL
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"pending": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pending query failed: {e}")
