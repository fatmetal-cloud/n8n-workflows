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


# --- Санация workflow JSON: убрать связи на несуществующие ноды ---
# Часть сценариев апстрима ссылается в connections на error-handler-ноды,
# которых нет в nodes. n8n >= 2.29 строго валидирует это и отклоняет импорт.
# Убираем мёртвые связи (сам сценарий не искажается - удаляем ссылки на то,
# чего и так нет). Делаем на стороне API, чтобы все потребители (cloud-init,
# кнопка «Скачать JSON», REST) получали уже валидный JSON.
def sanitize_workflow(wf: dict) -> dict:
    """Привести workflow к валидному для n8n виду:
    1) connections должны ключеваться по NAME ноды. В части сценариев апстрима
       ключи/цели идут по ID - ремаппим id->name.
    2) Связи на несуществующие ноды (error-handler-*, которых нет в nodes) - убираем.
    При любой ошибке отдаём исходный wf, чтобы не ломать download.
    """
    try:
        nodes = wf.get('nodes', []) or []
        names = {n.get('name') for n in nodes if n.get('name')}
        id2name = {n.get('id'): n.get('name') for n in nodes if n.get('id') and n.get('name')}

        def resolve(ref):
            # вернуть валидное NAME ноды или None
            if ref in names:
                return ref
            if ref in id2name:
                return id2name[ref]
            return None

        conns = wf.get('connections', {}) or {}
        clean = {}
        for src_ref, outs in conns.items():
            src_name = resolve(src_ref)
            if src_name is None:
                continue  # source не резолвится ни как name, ни как id - выкидываем
            new_outs = {}
            for out_type, branches in (outs or {}).items():
                new_branches = []
                for branch in (branches or []):
                    new_branch = []
                    for c in (branch or []):
                        if not isinstance(c, dict):
                            continue
                        tgt_name = resolve(c.get('node'))
                        if tgt_name is None:
                            continue  # target не существует (error-handler) - убираем
                        nc = dict(c)
                        nc['node'] = tgt_name
                        new_branch.append(nc)
                    new_branches.append(new_branch)
                new_outs[out_type] = new_branches
            # если source уже есть (после ремаппинга два ключа могут слиться) - мёржим
            if src_name in clean:
                for ot, brs in new_outs.items():
                    clean[src_name].setdefault(ot, [])
                    clean[src_name][ot].extend(brs)
            else:
                clean[src_name] = new_outs
        wf['connections'] = clean
        # tags в сценариях апстрима без валидных id - импорт n8n падает на них
        # (null tagId). Они для каталога, рабочему n8n не нужны - убираем.
        wf.pop('tags', None)
        wf.pop('pinData', None)
        return wf
    except Exception:
        return wf


# --- Шаги сценария (ноды) для секции «Как это работает» ---
# Схема потока апстрима (/diagram) не содержит связей (все битые error-handler),
# граф не строится. Поэтому отдаём чистый упорядоченный СПИСОК нод с типом -
# фронт рисует его как «шаги сценария». Служебные ноды (Sticky Note) убираем.
import os as _os, json as _json
from pathlib import Path as _Path

_TRIGGER_TYPES = {"scheduleTrigger", "webhook", "manualTrigger", "cron", "trigger",
                  "emailReadImap", "interval"}
_SKIP_TYPES = {"stickyNote"}

def _node_kind(t: str) -> str:
    tl = (t or "").lower()
    if any(x.lower() in tl for x in _TRIGGER_TYPES) or tl.endswith("trigger"):
        return "trigger"
    if "stopanderror" in tl or "errortrigger" in tl:
        return "error"
    if any(x in tl for x in ("openai", "agent", "lmchat", "gemini", "anthropic", "chain", "embeddings")):
        return "ai"
    return "action"

def get_workflow_steps(raw_json: dict) -> list:
    nodes = raw_json.get("nodes", []) or []
    steps = []
    for n in nodes:
        t = n.get("type", "") or ""
        short = t.split(".")[-1] if t else ""
        if short in _SKIP_TYPES:
            continue
        steps.append({
            "name": n.get("name", ""),
            "type": short,
            "kind": _node_kind(short),
        })
    # триггеры вперёд, ошибки в конец, остальное по исходному порядку
    order = {"trigger": 0, "action": 1, "ai": 1, "error": 2}
    steps.sort(key=lambda s: order.get(s["kind"], 1))
    return steps


@router.get("/api/workflows/{filename}/steps")
async def workflow_steps(filename: str):
    """Список нод сценария (шаги) для фронта. Читает JSON, чистит служебное."""
    import re as _re
    if not _re.match(r"^[\w\s.\-()+]+\.json$", filename):
        raise HTTPException(status_code=400, detail="bad filename")
    try:
        wf_dir = _Path("workflows").resolve()
        found = None
        for sub in wf_dir.iterdir():
            if sub.is_dir():
                p = sub / filename
                if p.exists():
                    found = p
                    break
        if not found:
            raise HTTPException(status_code=404, detail="not found")
        with open(found, encoding="utf-8") as f:
            wf = _json.load(f)
        return {"steps": get_workflow_steps(wf)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"steps failed: {e}")


# --- Граф сценария для визуализации «как в n8n» (canvas) ---
# Отдаём ноды с позициями (position [x,y]) + тип + kind. Связи фронт выводит
# из позиций (соседние по x в одном y-ряду): реальное поле connections в
# коллекции битое (сплошь несуществующие error-handler), граф из него не
# строится, а позиции есть у 100% нод. Служебные Sticky Note убираем.
def get_workflow_graph(raw_json: dict) -> dict:
    nodes = raw_json.get("nodes", []) or []
    out = []
    for n in nodes:
        t = n.get("type", "") or ""
        short = t.split(".")[-1] if t else ""
        if short in _SKIP_TYPES:
            continue
        pos = n.get("position", [0, 0]) or [0, 0]
        try:
            x, y = float(pos[0]), float(pos[1])
        except Exception:
            x, y = 0.0, 0.0
        out.append({
            "name": n.get("name", ""),
            "type": short,
            "kind": _node_kind(short),
            "x": x,
            "y": y,
        })
    return {"nodes": out, "count": len(out)}


@router.get("/api/workflows/{filename}/graph")
async def workflow_graph(filename: str):
    """Граф сценария (ноды + позиции + типы) для canvas-визуализации."""
    import re as _re
    if not _re.match(r"^[\w\s.\-()+]+\.json$", filename):
        raise HTTPException(status_code=400, detail="bad filename")
    try:
        wf_dir = _Path("workflows").resolve()
        found = None
        for sub in wf_dir.iterdir():
            if sub.is_dir():
                p = sub / filename
                if p.exists():
                    found = p
                    break
        if not found:
            raise HTTPException(status_code=404, detail="not found")
        with open(found, encoding="utf-8") as f:
            wf = _json.load(f)
        return get_workflow_graph(wf)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"graph failed: {e}")


# ── Категории Fatmetal (workflow_cat) + топ приложений ────────────────────────
# Категория проставляется агентом по рус-описанию (как фасад). Отдельная таблица -
# reindex её не затрёт. Подмешивается в ответ по filename.
CATEGORIES = [
    "AI и контент", "Данные и таблицы", "Коммуникации", "Календарь и встречи",
    "Документы и файлы", "Разработка", "Маркетинг и продажи", "Прочая автоматизация",
]

def init_cat_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_cat (
                filename     TEXT PRIMARY KEY,
                category     TEXT,
                generated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_category ON workflow_cat(category)")

def get_cat(filename):
    try:
        with _conn() as c:
            row = c.execute("SELECT category FROM workflow_cat WHERE filename = ?", (filename,)).fetchone()
        return row["category"] if row else None
    except Exception:
        return None

def enrich_cat_many(items):
    filenames = [i.get("filename") for i in items if i.get("filename")]
    cat_map = {}
    if filenames:
        try:
            ph = ",".join("?" * len(filenames))
            with _conn() as c:
                rows = c.execute(f"SELECT filename, category FROM workflow_cat WHERE filename IN ({ph})", filenames).fetchall()
            cat_map = {r["filename"]: r["category"] for r in rows}
        except Exception:
            pass
    for i in items:
        i["category"] = cat_map.get(i.get("filename"))
    return items


class CatIn(BaseModel):
    category: str


@router.post("/api/workflows/{filename}/category")
async def upsert_cat(filename: str, body: CatIn, request: Request):
    """Записать категорию сценария. Требует X-Facade-Token."""
    _check_token(request)
    if body.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"unknown category; allowed: {CATEGORIES}")
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO workflow_cat (filename, category, generated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(filename) DO UPDATE SET category=excluded.category, generated_at=datetime('now')
            """, (filename, body.category))
        return {"ok": True, "filename": filename, "category": body.category}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cat write failed: {e}")


@router.get("/api/categories-list")
async def categories_list():
    """Список категорий Fatmetal + сколько сценариев в каждой."""
    try:
        with _conn() as c:
            rows = c.execute("SELECT category, COUNT(*) AS n FROM workflow_cat GROUP BY category").fetchall()
        counts = {r["category"]: r["n"] for r in rows}
        return {"categories": [{"name": cat, "count": counts.get(cat, 0)} for cat in CATEGORIES]}
    except Exception:
        return {"categories": [{"name": cat, "count": 0} for cat in CATEGORIES]}


@router.get("/api/cat/pending")
async def cat_pending(limit: int = 50):
    """Сценарии без категории (для генератора). С рус-описанием для классификации."""
    try:
        with _conn() as c:
            rows = c.execute("""
                SELECT w.filename, w.name, w.description, w.integrations, r.ru_title, r.ru_description
                FROM workflows w
                LEFT JOIN workflow_cat k ON k.filename = w.filename
                LEFT JOIN workflow_ru r ON r.filename = w.filename
                WHERE k.filename IS NULL
                LIMIT ?
            """, (limit,)).fetchall()
        return {"pending": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cat pending failed: {e}")


# Топ приложений с РФ-приоритетом. Служебные ноды не считаем.
_APP_JUNK = {
    "Webhook","Httprequest","Respondtowebhook","Splitinbatches","Executeworkflow",
    "Extractfromfile","Converttofile","Markdown","Html","Removeduplicates","Form Trigger",
    "N8N","Set","Function","Code","Filter","Switch","If","Merge","Aggregate","Itemlists",
    "Noop","Wait","Datetime","Schedule Trigger","Manual Trigger","Editimage","Sort",
    "Splitout","Emailsend","Readwritefile","Spreadsheetfile","Limit","Rename","Compression",
    "Xml","Crypto","Ftp","Ssh","Movebinarydata","Renamekeys","Summarize","Comparedatasets",
}
# РФ-приоритет: эти сервисы поднимаем в топе (бонус к частоте).
_RF_BOOST = {"Telegram": 400, "WhatsApp": 400, "VK": 400}

@router.get("/api/top-apps")
async def top_apps(limit: int = 10):
    """Топ приложений каталога (с РФ-приоритетом) для фильтра «Популярное»."""
    try:
        from collections import Counter
        cnt = Counter()
        with _conn() as c:
            rows = c.execute("SELECT integrations FROM workflows").fetchall()
        for r in rows:
            try:
                ints = _json.loads(r["integrations"] or "[]")
            except Exception:
                ints = []
            for i in set(ints):
                if i not in _APP_JUNK:
                    cnt[i] += 1
        # РФ-приоритет: добавляем бонус к счётчику для ранжирования
        ranked = sorted(cnt.items(), key=lambda kv: kv[1] + _RF_BOOST.get(kv[0], 0), reverse=True)
        return {"apps": [{"name": n, "count": c} for n, c in ranked[:limit]]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"top-apps failed: {e}")
