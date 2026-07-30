"""
Fatmetal: реестр отложенных деплоев для one-click CTA «Развернуть n8n со сценарием».

Поток: фронт создаёт токен (token -> filename сценария), прокидывает его в имя
заказа VM (name=n8n-<token>). cloud-init шаблона n8n читает свой hostname,
извлекает токен, дёргает GET /api/deploy/{token} и импортирует сценарий.

Таблица pending_deploys живёт в той же SQLite (отдельная, как workflow_ru).
Токены с TTL: неоплаченные/неиспользованные чистятся, чтобы не копиться.
"""
import os
import re
import sqlite3
import secrets
import urllib.parse
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

DB_PATH = os.environ.get("WORKFLOW_DB_PATH", "database/workflows.db")
# TTL токена: 24 часа. За это время пользователь оплачивает и VM стартует.
TOKEN_TTL_HOURS = 24


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_deploy_table() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_deploys (
                token        TEXT PRIMARY KEY,
                filename     TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                consumed_at  TEXT
            )
            """
        )


def _cleanup(c) -> None:
    """Удалить протухшие токены (старше TTL)."""
    c.execute(
        "DELETE FROM pending_deploys WHERE created_at < datetime('now', ?)",
        (f'-{TOKEN_TTL_HOURS} hours',),
    )


router = APIRouter()


class DeployCreateIn(BaseModel):
    filename: str


class DeployCreateOut(BaseModel):
    token: str
    vm_name: str  # готовое имя заказа для checkout (name=n8n-<token>)


class DeployResolveOut(BaseModel):
    filename: str
    download_url: str


# Публичный базовый URL каталога (для сборки download_url в ответе cloud-init).
PUBLIC_BASE = os.environ.get("N8N_CATALOG_PUBLIC_BASE", "https://n8n-api.fatmetal.net")

# Токен - только [a-z0-9], чтобы безопасно жить в hostname (DNS-совместимо).
def _gen_token() -> str:
    return secrets.token_hex(5)  # 10 hex-символов


@router.post("/api/deploy", response_model=DeployCreateOut)
async def create_deploy(body: DeployCreateIn):
    """Фронт: создать токен для сценария. Возвращает token и готовое vm_name."""
    # валидируем, что сценарий существует
    fn = body.filename
    # Разрешаем любые имена (в каталоге есть файлы с пробелами/скобками),
    # но защищаемся от path-traversal и требуем расширение .json.
    if ('/' in fn) or ('\\' in fn) or ('..' in fn) or ('\x00' in fn) or not fn.endswith('.json'):
        raise HTTPException(status_code=400, detail="bad filename")
    try:
        with _conn() as c:
            _cleanup(c)
            row = c.execute("SELECT 1 FROM workflows WHERE filename = ?", (fn,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="scenario not found")
            token = _gen_token()
            c.execute(
                "INSERT INTO pending_deploys (token, filename) VALUES (?, ?)",
                (token, fn),
            )
        return DeployCreateOut(token=token, vm_name=f"n8n-{token}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"deploy create failed: {e}")


@router.get("/api/deploy/{token}", response_model=DeployResolveOut)
async def resolve_deploy(token: str, request: Request):
    """cloud-init: по токену получить сценарий (download_url). Помечает consumed."""
    if not re.match(r'^[a-f0-9]{6,32}$', token):
        raise HTTPException(status_code=400, detail="bad token")
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT filename FROM pending_deploys WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="token not found or expired")
            fn = row["filename"]
            c.execute(
                "UPDATE pending_deploys SET consumed_at = datetime('now') WHERE token = ?",
                (token,),
            )
        return DeployResolveOut(
            filename=fn,
            download_url=f"{PUBLIC_BASE}/api/workflows/{urllib.parse.quote(fn)}/download",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"deploy resolve failed: {e}")
