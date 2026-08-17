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


# Справочник макс. валидных typeVersion нод n8n (из n8n-as-code node registry, 825 нод).
# Используется санацией для понижения несуществующих typeVersion до валидной.
_NODE_MAX_VERSION = {
    '@n8n/n8n-nodes-langchain.agent': 3.1,
    '@n8n/n8n-nodes-langchain.agentTool': 3,
    '@n8n/n8n-nodes-langchain.alibabaCloud': 1.1,
    '@n8n/n8n-nodes-langchain.alibabaCloudTool': 1.1,
    '@n8n/n8n-nodes-langchain.anthropic': 1,
    '@n8n/n8n-nodes-langchain.anthropicTool': 1,
    '@n8n/n8n-nodes-langchain.chainLlm': 1.9,
    '@n8n/n8n-nodes-langchain.chainRetrievalQa': 1.7,
    '@n8n/n8n-nodes-langchain.chainSummarization': 2.1,
    '@n8n/n8n-nodes-langchain.chat': 1.3,
    '@n8n/n8n-nodes-langchain.chatHubVectorStorePGVector': 1.3,
    '@n8n/n8n-nodes-langchain.chatHubVectorStorePinecone': 1.3,
    '@n8n/n8n-nodes-langchain.chatHubVectorStoreQdrant': 1.3,
    '@n8n/n8n-nodes-langchain.chatTool': 1.3,
    '@n8n/n8n-nodes-langchain.chatTrigger': 1.4,
    '@n8n/n8n-nodes-langchain.documentBinaryInputLoader': 1,
    '@n8n/n8n-nodes-langchain.documentDefaultDataLoader': 1.1,
    '@n8n/n8n-nodes-langchain.documentGithubLoader': 1.1,
    '@n8n/n8n-nodes-langchain.documentJsonInputLoader': 1,
    '@n8n/n8n-nodes-langchain.embeddingsAwsBedrock': 1,
    '@n8n/n8n-nodes-langchain.embeddingsAzureOpenAi': 1,
    '@n8n/n8n-nodes-langchain.embeddingsCohere': 1,
    '@n8n/n8n-nodes-langchain.embeddingsGoogleGemini': 1,
    '@n8n/n8n-nodes-langchain.embeddingsGoogleVertex': 1,
    '@n8n/n8n-nodes-langchain.embeddingsHuggingFaceInference': 1,
    '@n8n/n8n-nodes-langchain.embeddingsLemonade': 1,
    '@n8n/n8n-nodes-langchain.embeddingsMistralCloud': 1,
    '@n8n/n8n-nodes-langchain.embeddingsNvidia': 1,
    '@n8n/n8n-nodes-langchain.embeddingsOllama': 1,
    '@n8n/n8n-nodes-langchain.embeddingsOpenAi': 1.2,
    '@n8n/n8n-nodes-langchain.embeddingsOracleDb': 1,
    '@n8n/n8n-nodes-langchain.googleGemini': 1.2,
    '@n8n/n8n-nodes-langchain.googleGeminiTool': 1.2,
    '@n8n/n8n-nodes-langchain.guardrails': 2,
    '@n8n/n8n-nodes-langchain.informationExtractor': 1.2,
    '@n8n/n8n-nodes-langchain.lmChatAlibabaCloud': 1,
    '@n8n/n8n-nodes-langchain.lmChatAnthropic': 1.5,
    '@n8n/n8n-nodes-langchain.lmChatAwsBedrock': 1.1,
    '@n8n/n8n-nodes-langchain.lmChatAzureOpenAi': 1,
    '@n8n/n8n-nodes-langchain.lmChatCohere': 1,
    '@n8n/n8n-nodes-langchain.lmChatDeepSeek': 1,
    '@n8n/n8n-nodes-langchain.lmChatGoogleGemini': 1.1,
    '@n8n/n8n-nodes-langchain.lmChatGoogleVertex': 1,
    '@n8n/n8n-nodes-langchain.lmChatGroq': 1,
    '@n8n/n8n-nodes-langchain.lmChatLemonade': 1,
    '@n8n/n8n-nodes-langchain.lmChatMinimax': 1,
    '@n8n/n8n-nodes-langchain.lmChatMistralCloud': 1,
    '@n8n/n8n-nodes-langchain.lmChatMoonshot': 1.1,
    '@n8n/n8n-nodes-langchain.lmChatNvidia': 1,
    '@n8n/n8n-nodes-langchain.lmChatOllama': 1,
    '@n8n/n8n-nodes-langchain.lmChatOpenAi': 1.3,
    '@n8n/n8n-nodes-langchain.lmChatOpenRouter': 1,
    '@n8n/n8n-nodes-langchain.lmChatVercelAiGateway': 1,
    '@n8n/n8n-nodes-langchain.lmChatXAiGrok': 1,
    '@n8n/n8n-nodes-langchain.lmCohere': 1,
    '@n8n/n8n-nodes-langchain.lmLemonade': 1,
    '@n8n/n8n-nodes-langchain.lmOllama': 1,
    '@n8n/n8n-nodes-langchain.lmOpenAi': 1,
    '@n8n/n8n-nodes-langchain.lmOpenHuggingFaceInference': 1,
    '@n8n/n8n-nodes-langchain.manualChatTrigger': 1.1,
    '@n8n/n8n-nodes-langchain.mcpClient': 1.1,
    '@n8n/n8n-nodes-langchain.mcpClientTool': 1.4,
    '@n8n/n8n-nodes-langchain.mcpRegistryClientTool': 1.1,
    '@n8n/n8n-nodes-langchain.mcpTrigger': 2,
    '@n8n/n8n-nodes-langchain.memoryBufferWindow': 1.4,
    '@n8n/n8n-nodes-langchain.memoryChatRetriever': 1,
    '@n8n/n8n-nodes-langchain.memoryManager': 1.1,
    '@n8n/n8n-nodes-langchain.memoryMongoDbChat': 1.1,
    '@n8n/n8n-nodes-langchain.memoryMotorhead': 1.4,
    '@n8n/n8n-nodes-langchain.memoryPostgresChat': 1.4,
    '@n8n/n8n-nodes-langchain.memoryRedisChat': 1.6,
    '@n8n/n8n-nodes-langchain.memoryXata': 1.5,
    '@n8n/n8n-nodes-langchain.memoryZep': 1.4,
    '@n8n/n8n-nodes-langchain.microsoftAgent365Trigger': 1.1,
    '@n8n/n8n-nodes-langchain.minimax': 1,
    '@n8n/n8n-nodes-langchain.minimaxTool': 1,
    '@n8n/n8n-nodes-langchain.modelSelector': 1,
    '@n8n/n8n-nodes-langchain.moonshot': 1,
    '@n8n/n8n-nodes-langchain.moonshotTool': 1,
    '@n8n/n8n-nodes-langchain.ollama': 1,
    '@n8n/n8n-nodes-langchain.ollamaTool': 1,
    '@n8n/n8n-nodes-langchain.openAiAssistant': 1.1,
    '@n8n/n8n-nodes-langchain.outputParserAutofixing': 1,
    '@n8n/n8n-nodes-langchain.outputParserItemList': 1,
    '@n8n/n8n-nodes-langchain.outputParserStructured': 1.3,
    '@n8n/n8n-nodes-langchain.rerankerCohere': 1,
    '@n8n/n8n-nodes-langchain.retrieverContextualCompression': 1,
    '@n8n/n8n-nodes-langchain.retrieverMultiQuery': 1,
    '@n8n/n8n-nodes-langchain.retrieverVectorStore': 1,
    '@n8n/n8n-nodes-langchain.retrieverWorkflow': 1.1,
    '@n8n/n8n-nodes-langchain.sentimentAnalysis': 1.1,
    '@n8n/n8n-nodes-langchain.textClassifier': 1.1,
    '@n8n/n8n-nodes-langchain.textSplitterCharacterTextSplitter': 1,
    '@n8n/n8n-nodes-langchain.textSplitterRecursiveCharacterTextSplitter': 1,
    '@n8n/n8n-nodes-langchain.textSplitterTokenSplitter': 1,
    '@n8n/n8n-nodes-langchain.toolCalculator': 1,
    '@n8n/n8n-nodes-langchain.toolCode': 1.3,
    '@n8n/n8n-nodes-langchain.toolExecutor': 1,
    '@n8n/n8n-nodes-langchain.toolHttpRequest': 1.1,
    '@n8n/n8n-nodes-langchain.toolSearXng': 1,
    '@n8n/n8n-nodes-langchain.toolSerpApi': 1,
    '@n8n/n8n-nodes-langchain.toolThink': 1.1,
    '@n8n/n8n-nodes-langchain.toolVectorStore': 1.1,
    '@n8n/n8n-nodes-langchain.toolWikipedia': 1,
    '@n8n/n8n-nodes-langchain.toolWolframAlpha': 1,
    '@n8n/n8n-nodes-langchain.toolWorkflow': 2.2,
    '@n8n/n8n-nodes-langchain.vectorStoreAzureAISearch': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreChromaDB': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreInMemory': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreInMemoryInsert': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreInMemoryLoad': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreMilvus': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreMongoDBAtlas': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreOracleDBVector': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStorePGVector': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStorePinecone': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStorePineconeInsert': 1,
    '@n8n/n8n-nodes-langchain.vectorStorePineconeLoad': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreQdrant': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreRedis': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreSupabase': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreSupabaseInsert': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreSupabaseLoad': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreWeaviate': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreZep': 1.3,
    '@n8n/n8n-nodes-langchain.vectorStoreZepInsert': 1,
    '@n8n/n8n-nodes-langchain.vectorStoreZepLoad': 1,
    'n8n-nodes-base.Brandfetch': 1,
    'n8n-nodes-base.BrandfetchTool': 1,
    'n8n-nodes-base.actionNetwork': 1,
    'n8n-nodes-base.actionNetworkTool': 1,
    'n8n-nodes-base.activeCampaign': 1,
    'n8n-nodes-base.activeCampaignTool': 1,
    'n8n-nodes-base.activeCampaignTrigger': 1,
    'n8n-nodes-base.acuitySchedulingTrigger': 1,
    'n8n-nodes-base.adalo': 1,
    'n8n-nodes-base.adaloTool': 1,
    'n8n-nodes-base.affinity': 1,
    'n8n-nodes-base.affinityTool': 1,
    'n8n-nodes-base.affinityTrigger': 1,
    'n8n-nodes-base.aggregate': 1,
    'n8n-nodes-base.agileCrm': 1,
    'n8n-nodes-base.agileCrmTool': 1,
    'n8n-nodes-base.aiTransform': 1,
    'n8n-nodes-base.airtable': 2.2,
    'n8n-nodes-base.airtableTool': 2.2,
    'n8n-nodes-base.airtableTrigger': 1,
    'n8n-nodes-base.airtop': 1.1,
    'n8n-nodes-base.airtopTool': 1.1,
    'n8n-nodes-base.amqp': 1,
    'n8n-nodes-base.amqpTool': 1,
    'n8n-nodes-base.amqpTrigger': 1,
    'n8n-nodes-base.apiTemplateIo': 1,
    'n8n-nodes-base.apiTemplateIoTool': 1,
    'n8n-nodes-base.asana': 1,
    'n8n-nodes-base.asanaTool': 1,
    'n8n-nodes-base.asanaTrigger': 1,
    'n8n-nodes-base.autopilot': 1,
    'n8n-nodes-base.autopilotTool': 1,
    'n8n-nodes-base.autopilotTrigger': 1,
    'n8n-nodes-base.awsCertificateManager': 1,
    'n8n-nodes-base.awsCognito': 1,
    'n8n-nodes-base.awsComprehend': 1,
    'n8n-nodes-base.awsDynamoDb': 1,
    'n8n-nodes-base.awsElb': 1,
    'n8n-nodes-base.awsIam': 1,
    'n8n-nodes-base.awsLambda': 1,
    'n8n-nodes-base.awsLambdaTool': 1,
    'n8n-nodes-base.awsRekognition': 1,
    'n8n-nodes-base.awsS3': 2,
    'n8n-nodes-base.awsS3Tool': 2,
    'n8n-nodes-base.awsSes': 1,
    'n8n-nodes-base.awsSesTool': 1,
    'n8n-nodes-base.awsSns': 1,
    'n8n-nodes-base.awsSnsTool': 1,
    'n8n-nodes-base.awsSnsTrigger': 1,
    'n8n-nodes-base.awsSqs': 1,
    'n8n-nodes-base.awsTextract': 1,
    'n8n-nodes-base.awsTextractTool': 1,
    'n8n-nodes-base.awsTranscribe': 1,
    'n8n-nodes-base.awsTranscribeTool': 1,
    'n8n-nodes-base.azureCosmosDb': 1,
    'n8n-nodes-base.azureStorage': 1,
    'n8n-nodes-base.bambooHr': 1,
    'n8n-nodes-base.bambooHrTool': 1,
    'n8n-nodes-base.bannerbear': 1,
    'n8n-nodes-base.baserow': 1.1,
    'n8n-nodes-base.baserowTool': 1.1,
    'n8n-nodes-base.beeminder': 1,
    'n8n-nodes-base.beeminderTool': 1,
    'n8n-nodes-base.bitbucketTrigger': 1.1,
    'n8n-nodes-base.bitly': 1,
    'n8n-nodes-base.bitlyTool': 1,
    'n8n-nodes-base.bitwarden': 1,
    'n8n-nodes-base.bitwardenTool': 1,
    'n8n-nodes-base.box': 1,
    'n8n-nodes-base.boxTrigger': 1,
    'n8n-nodes-base.bubble': 1,
    'n8n-nodes-base.bubbleTool': 1,
    'n8n-nodes-base.calTrigger': 2,
    'n8n-nodes-base.calendlyTrigger': 2,
    'n8n-nodes-base.chargebee': 1,
    'n8n-nodes-base.chargebeeTool': 1,
    'n8n-nodes-base.chargebeeTrigger': 1,
    'n8n-nodes-base.circleCi': 1,
    'n8n-nodes-base.circleCiTool': 1,
    'n8n-nodes-base.ciscoWebex': 1,
    'n8n-nodes-base.ciscoWebexTool': 1,
    'n8n-nodes-base.ciscoWebexTrigger': 1,
    'n8n-nodes-base.citrixAdc': 1,
    'n8n-nodes-base.clearbit': 1,
    'n8n-nodes-base.clearbitTool': 1,
    'n8n-nodes-base.clickUp': 1,
    'n8n-nodes-base.clickUpTool': 1,
    'n8n-nodes-base.clickUpTrigger': 1,
    'n8n-nodes-base.clockify': 1,
    'n8n-nodes-base.clockifyTool': 1,
    'n8n-nodes-base.clockifyTrigger': 1,
    'n8n-nodes-base.cloudflare': 1,
    'n8n-nodes-base.cloudflareTool': 1,
    'n8n-nodes-base.cockpit': 1,
    'n8n-nodes-base.cockpitTool': 1,
    'n8n-nodes-base.coda': 1.1,
    'n8n-nodes-base.codaTool': 1.1,
    'n8n-nodes-base.code': 2,
    'n8n-nodes-base.coinGecko': 1,
    'n8n-nodes-base.coinGeckoTool': 1,
    'n8n-nodes-base.compareDatasets': 2.3,
    'n8n-nodes-base.compression': 1.1,
    'n8n-nodes-base.compressionTool': 1.1,
    'n8n-nodes-base.contentful': 1,
    'n8n-nodes-base.contentfulTool': 1,
    'n8n-nodes-base.convertKit': 1,
    'n8n-nodes-base.convertKitTool': 1,
    'n8n-nodes-base.convertKitTrigger': 1,
    'n8n-nodes-base.convertToFile': 1.1,
    'n8n-nodes-base.copper': 1,
    'n8n-nodes-base.copperTool': 1,
    'n8n-nodes-base.copperTrigger': 1,
    'n8n-nodes-base.cortex': 1,
    'n8n-nodes-base.crateDb': 1,
    'n8n-nodes-base.crateDbTool': 1,
    'n8n-nodes-base.cron': 1,
    'n8n-nodes-base.crypto': 2,
    'n8n-nodes-base.cryptoTool': 2,
    'n8n-nodes-base.currents': 1,
    'n8n-nodes-base.currentsTool': 1,
    'n8n-nodes-base.currentsTrigger': 1,
    'n8n-nodes-base.customerIo': 1,
    'n8n-nodes-base.customerIoTool': 1,
    'n8n-nodes-base.customerIoTrigger': 1,
    'n8n-nodes-base.dataTable': 1.1,
    'n8n-nodes-base.dataTableTool': 1.1,
    'n8n-nodes-base.databricks': 1,
    'n8n-nodes-base.databricksTool': 1,
    'n8n-nodes-base.dateTime': 2,
    'n8n-nodes-base.dateTimeTool': 2,
    'n8n-nodes-base.debugHelper': 1,
    'n8n-nodes-base.deepL': 1,
    'n8n-nodes-base.deepLTool': 1,
    'n8n-nodes-base.demio': 1,
    'n8n-nodes-base.demioTool': 1,
    'n8n-nodes-base.dhl': 1,
    'n8n-nodes-base.dhlTool': 1,
    'n8n-nodes-base.discord': 2,
    'n8n-nodes-base.discordTool': 2,
    'n8n-nodes-base.discourse': 1,
    'n8n-nodes-base.discourseTool': 1,
    'n8n-nodes-base.disqus': 1,
    'n8n-nodes-base.drift': 1,
    'n8n-nodes-base.driftTool': 1,
    'n8n-nodes-base.dropbox': 1,
    'n8n-nodes-base.dropboxTool': 1,
    'n8n-nodes-base.dropcontact': 1,
    'n8n-nodes-base.dropcontactTool': 1,
    'n8n-nodes-base.dynamicCredentialCheck': 1,
    'n8n-nodes-base.e2eTest': 1,
    'n8n-nodes-base.e2eTestTool': 1,
    'n8n-nodes-base.editImage': 1,
    'n8n-nodes-base.egoi': 1,
    'n8n-nodes-base.egoiTool': 1,
    'n8n-nodes-base.elasticSecurity': 1,
    'n8n-nodes-base.elasticSecurityTool': 1,
    'n8n-nodes-base.elasticsearch': 1,
    'n8n-nodes-base.elasticsearchTool': 1,
    'n8n-nodes-base.emailReadImap': 2.1,
    'n8n-nodes-base.emailSend': 2.1,
    'n8n-nodes-base.emailSendTool': 2.1,
    'n8n-nodes-base.emelia': 1,
    'n8n-nodes-base.emeliaTool': 1,
    'n8n-nodes-base.emeliaTrigger': 1,
    'n8n-nodes-base.erpNext': 1,
    'n8n-nodes-base.erpNextTool': 1,
    'n8n-nodes-base.errorTrigger': 1,
    'n8n-nodes-base.eventbriteTrigger': 1,
    'n8n-nodes-base.executeCommand': 1,
    'n8n-nodes-base.executeCommandTool': 1,
    'n8n-nodes-base.executeWorkflow': 1.3,
    'n8n-nodes-base.executeWorkflowTrigger': 1.2,
    'n8n-nodes-base.executionData': 1.1,
    'n8n-nodes-base.extractFromFile': 1.1,
    'n8n-nodes-base.facebookGraphApi': 1,
    'n8n-nodes-base.facebookGraphApiTool': 1,
    'n8n-nodes-base.facebookLeadAdsTrigger': 1,
    'n8n-nodes-base.facebookTrigger': 1,
    'n8n-nodes-base.figmaTrigger': 1,
    'n8n-nodes-base.filemaker': 1,
    'n8n-nodes-base.filemakerTool': 1,
    'n8n-nodes-base.filter': 2.3,
    'n8n-nodes-base.flow': 1,
    'n8n-nodes-base.flowTrigger': 1,
    'n8n-nodes-base.form': 2.5,
    'n8n-nodes-base.formIoTrigger': 1,
    'n8n-nodes-base.formTrigger': 2.6,
    'n8n-nodes-base.formstackTrigger': 1,
    'n8n-nodes-base.freshdesk': 1,
    'n8n-nodes-base.freshdeskTool': 1,
    'n8n-nodes-base.freshservice': 1,
    'n8n-nodes-base.freshserviceTool': 1,
    'n8n-nodes-base.freshworksCrm': 1,
    'n8n-nodes-base.freshworksCrmTool': 1,
    'n8n-nodes-base.ftp': 1,
    'n8n-nodes-base.function': 1,
    'n8n-nodes-base.functionItem': 1,
    'n8n-nodes-base.gSuiteAdmin': 1,
    'n8n-nodes-base.gSuiteAdminTool': 1,
    'n8n-nodes-base.getResponse': 1,
    'n8n-nodes-base.getResponseTool': 1,
    'n8n-nodes-base.getResponseTrigger': 1,
    'n8n-nodes-base.ghost': 1,
    'n8n-nodes-base.ghostTool': 1,
    'n8n-nodes-base.git': 1.1,
    'n8n-nodes-base.gitTool': 1.1,
    'n8n-nodes-base.github': 1.1,
    'n8n-nodes-base.githubTool': 1.1,
    'n8n-nodes-base.githubTrigger': 1,
    'n8n-nodes-base.gitlab': 1,
    'n8n-nodes-base.gitlabTool': 1,
    'n8n-nodes-base.gitlabTrigger': 1,
    'n8n-nodes-base.gmail': 2.2,
    'n8n-nodes-base.gmailTool': 2.2,
    'n8n-nodes-base.gmailTrigger': 1.4,
    'n8n-nodes-base.goToWebinar': 1,
    'n8n-nodes-base.goToWebinarTool': 1,
    'n8n-nodes-base.gong': 1,
    'n8n-nodes-base.gongTool': 1,
    'n8n-nodes-base.googleAds': 1,
    'n8n-nodes-base.googleAdsTool': 1,
    'n8n-nodes-base.googleAnalytics': 2,
    'n8n-nodes-base.googleAnalyticsTool': 2,
    'n8n-nodes-base.googleBigQuery': 2.1,
    'n8n-nodes-base.googleBigQueryTool': 2.1,
    'n8n-nodes-base.googleBooks': 2,
    'n8n-nodes-base.googleBooksTool': 2,
    'n8n-nodes-base.googleBusinessProfile': 1,
    'n8n-nodes-base.googleBusinessProfileTool': 1,
    'n8n-nodes-base.googleBusinessProfileTrigger': 1,
    'n8n-nodes-base.googleCalendar': 1.3,
    'n8n-nodes-base.googleCalendarTool': 1.3,
    'n8n-nodes-base.googleCalendarTrigger': 1,
    'n8n-nodes-base.googleChat': 1,
    'n8n-nodes-base.googleChatTool': 1,
    'n8n-nodes-base.googleCloudNaturalLanguage': 1,
    'n8n-nodes-base.googleCloudNaturalLanguageTool': 1,
    'n8n-nodes-base.googleCloudStorage': 1.1,
    'n8n-nodes-base.googleCloudStorageTool': 1.1,
    'n8n-nodes-base.googleContacts': 1,
    'n8n-nodes-base.googleContactsTool': 1,
    'n8n-nodes-base.googleDocs': 2,
    'n8n-nodes-base.googleDocsTool': 2,
    'n8n-nodes-base.googleDrive': 3,
    'n8n-nodes-base.googleDriveTool': 3,
    'n8n-nodes-base.googleDriveTrigger': 1,
    'n8n-nodes-base.googleFirebaseCloudFirestore': 1.1,
    'n8n-nodes-base.googleFirebaseCloudFirestoreTool': 1.1,
    'n8n-nodes-base.googleFirebaseRealtimeDatabase': 1,
    'n8n-nodes-base.googleFirebaseRealtimeDatabaseTool': 1,
    'n8n-nodes-base.googlePerspective': 1,
    'n8n-nodes-base.googlePerspectiveTool': 1,
    'n8n-nodes-base.googleSheets': 4.7,
    'n8n-nodes-base.googleSheetsTool': 4.7,
    'n8n-nodes-base.googleSheetsTrigger': 1,
    'n8n-nodes-base.googleSlides': 2,
    'n8n-nodes-base.googleSlidesTool': 2,
    'n8n-nodes-base.googleTasks': 1,
    'n8n-nodes-base.googleTasksTool': 1,
    'n8n-nodes-base.googleTranslate': 2,
    'n8n-nodes-base.googleTranslateTool': 2,
    'n8n-nodes-base.gotify': 1,
    'n8n-nodes-base.gotifyTool': 1,
    'n8n-nodes-base.grafana': 1,
    'n8n-nodes-base.grafanaTool': 1,
    'n8n-nodes-base.graphql': 1.1,
    'n8n-nodes-base.graphqlTool': 1.1,
    'n8n-nodes-base.grist': 1,
    'n8n-nodes-base.gristTool': 1,
    'n8n-nodes-base.gumroadTrigger': 1,
    'n8n-nodes-base.hackerNews': 1,
    'n8n-nodes-base.hackerNewsTool': 1,
    'n8n-nodes-base.haloPSA': 1,
    'n8n-nodes-base.haloPSATool': 1,
    'n8n-nodes-base.harvest': 1,
    'n8n-nodes-base.harvestTool': 1,
    'n8n-nodes-base.helpScout': 1,
    'n8n-nodes-base.helpScoutTool': 1,
    'n8n-nodes-base.helpScoutTrigger': 1,
    'n8n-nodes-base.highLevel': 2,
    'n8n-nodes-base.highLevelTool': 2,
    'n8n-nodes-base.homeAssistant': 1,
    'n8n-nodes-base.homeAssistantTool': 1,
    'n8n-nodes-base.html': 1.2,
    'n8n-nodes-base.htmlExtract': 1,
    'n8n-nodes-base.httpRequest': 4.4,
    'n8n-nodes-base.httpRequestTool': 4.4,
    'n8n-nodes-base.hubspot': 2.2,
    'n8n-nodes-base.hubspotTool': 2.2,
    'n8n-nodes-base.hubspotTrigger': 1,
    'n8n-nodes-base.humanticAi': 1,
    'n8n-nodes-base.humanticAiTool': 1,
    'n8n-nodes-base.hunter': 1,
    'n8n-nodes-base.hunterTool': 1,
    'n8n-nodes-base.iCal': 1,
    'n8n-nodes-base.if': 2.3,
    'n8n-nodes-base.intercom': 1,
    'n8n-nodes-base.intercomTool': 1,
    'n8n-nodes-base.interval': 1,
    'n8n-nodes-base.invoiceNinja': 2,
    'n8n-nodes-base.invoiceNinjaTool': 2,
    'n8n-nodes-base.invoiceNinjaTrigger': 2,
    'n8n-nodes-base.itemLists': 3.1,
    'n8n-nodes-base.iterable': 1,
    'n8n-nodes-base.iterableTool': 1,
    'n8n-nodes-base.jenkins': 1,
    'n8n-nodes-base.jenkinsTool': 1,
    'n8n-nodes-base.jinaAi': 1,
    'n8n-nodes-base.jinaAiTool': 1,
    'n8n-nodes-base.jira': 1,
    'n8n-nodes-base.jiraTool': 1,
    'n8n-nodes-base.jiraTrigger': 1.1,
    'n8n-nodes-base.jotFormTrigger': 1,
    'n8n-nodes-base.jwt': 1,
    'n8n-nodes-base.jwtTool': 1,
    'n8n-nodes-base.kafka': 1,
    'n8n-nodes-base.kafkaTool': 1,
    'n8n-nodes-base.kafkaTrigger': 1.3,
    'n8n-nodes-base.keap': 1,
    'n8n-nodes-base.keapTool': 1,
    'n8n-nodes-base.keapTrigger': 1,
    'n8n-nodes-base.koBoToolbox': 1,
    'n8n-nodes-base.koBoToolboxTool': 1,
    'n8n-nodes-base.koBoToolboxTrigger': 1,
    'n8n-nodes-base.ldap': 1,
    'n8n-nodes-base.ldapTool': 1,
    'n8n-nodes-base.lemlist': 2,
    'n8n-nodes-base.lemlistTool': 2,
    'n8n-nodes-base.lemlistTrigger': 1,
    'n8n-nodes-base.limit': 1,
    'n8n-nodes-base.line': 1,
    'n8n-nodes-base.lineTool': 1,
    'n8n-nodes-base.linear': 1.1,
    'n8n-nodes-base.linearTool': 1.1,
    'n8n-nodes-base.linearTrigger': 1,
    'n8n-nodes-base.lingvaNex': 1,
    'n8n-nodes-base.lingvaNexTool': 1,
    'n8n-nodes-base.linkedIn': 1,
    'n8n-nodes-base.linkedInTool': 1,
    'n8n-nodes-base.localFileTrigger': 1,
    'n8n-nodes-base.loneScale': 1,
    'n8n-nodes-base.loneScaleTool': 1,
    'n8n-nodes-base.loneScaleTrigger': 1,
    'n8n-nodes-base.magento2': 1,
    'n8n-nodes-base.magento2Tool': 1,
    'n8n-nodes-base.mailcheck': 1,
    'n8n-nodes-base.mailcheckTool': 1,
    'n8n-nodes-base.mailchimp': 1,
    'n8n-nodes-base.mailchimpTool': 1,
    'n8n-nodes-base.mailchimpTrigger': 1,
    'n8n-nodes-base.mailerLite': 2,
    'n8n-nodes-base.mailerLiteTool': 2,
    'n8n-nodes-base.mailerLiteTrigger': 2,
    'n8n-nodes-base.mailgun': 1,
    'n8n-nodes-base.mailgunTool': 1,
    'n8n-nodes-base.mailjet': 1,
    'n8n-nodes-base.mailjetTool': 1,
    'n8n-nodes-base.mailjetTrigger': 1,
    'n8n-nodes-base.mandrill': 1,
    'n8n-nodes-base.mandrillTool': 1,
    'n8n-nodes-base.manualTrigger': 1,
    'n8n-nodes-base.markdown': 1,
    'n8n-nodes-base.marketstack': 1,
    'n8n-nodes-base.marketstackTool': 1,
    'n8n-nodes-base.matrix': 1,
    'n8n-nodes-base.matrixTool': 1,
    'n8n-nodes-base.mattermost': 1,
    'n8n-nodes-base.mattermostTool': 1,
    'n8n-nodes-base.mautic': 1,
    'n8n-nodes-base.mauticTool': 1,
    'n8n-nodes-base.mauticTrigger': 1,
    'n8n-nodes-base.medium': 1,
    'n8n-nodes-base.mediumTool': 1,
    'n8n-nodes-base.merge': 3.2,
    'n8n-nodes-base.messageAnAgent': 2,
    'n8n-nodes-base.messageAnAgentTool': 2,
    'n8n-nodes-base.messageBird': 1,
    'n8n-nodes-base.messageBirdTool': 1,
    'n8n-nodes-base.metabase': 1,
    'n8n-nodes-base.metabaseTool': 1,
    'n8n-nodes-base.microsoftDynamicsCrm': 1,
    'n8n-nodes-base.microsoftDynamicsCrmTool': 1,
    'n8n-nodes-base.microsoftEntra': 1,
    'n8n-nodes-base.microsoftEntraTool': 1,
    'n8n-nodes-base.microsoftExcel': 2.2,
    'n8n-nodes-base.microsoftExcelTool': 2.2,
    'n8n-nodes-base.microsoftGraphSecurity': 1,
    'n8n-nodes-base.microsoftGraphSecurityTool': 1,
    'n8n-nodes-base.microsoftOneDrive': 1.1,
    'n8n-nodes-base.microsoftOneDriveTool': 1.1,
    'n8n-nodes-base.microsoftOneDriveTrigger': 1,
    'n8n-nodes-base.microsoftOutlook': 2,
    'n8n-nodes-base.microsoftOutlookTool': 2,
    'n8n-nodes-base.microsoftOutlookTrigger': 1,
    'n8n-nodes-base.microsoftSharePoint': 1,
    'n8n-nodes-base.microsoftSharePointTool': 1,
    'n8n-nodes-base.microsoftSql': 1.1,
    'n8n-nodes-base.microsoftSqlTool': 1.1,
    'n8n-nodes-base.microsoftTeams': 2,
    'n8n-nodes-base.microsoftTeamsTool': 2,
    'n8n-nodes-base.microsoftTeamsTrigger': 1,
    'n8n-nodes-base.microsoftToDo': 1,
    'n8n-nodes-base.microsoftToDoTool': 1,
    'n8n-nodes-base.mindee': 3,
    'n8n-nodes-base.misp': 1,
    'n8n-nodes-base.mispTool': 1,
    'n8n-nodes-base.mistralAi': 1,
    'n8n-nodes-base.mistralAiTool': 1,
    'n8n-nodes-base.mocean': 1,
    'n8n-nodes-base.moceanTool': 1,
    'n8n-nodes-base.mondayCom': 1,
    'n8n-nodes-base.mondayComTool': 1,
    'n8n-nodes-base.mongoDb': 1.3,
    'n8n-nodes-base.mongoDbTool': 1.3,
    'n8n-nodes-base.monicaCrm': 1,
    'n8n-nodes-base.monicaCrmTool': 1,
    'n8n-nodes-base.moveBinaryData': 1.1,
    'n8n-nodes-base.mqtt': 1,
    'n8n-nodes-base.mqttTool': 1,
    'n8n-nodes-base.mqttTrigger': 1,
    'n8n-nodes-base.msg91': 1,
    'n8n-nodes-base.msg91Tool': 1,
    'n8n-nodes-base.mySql': 2.5,
    'n8n-nodes-base.mySqlTool': 2.5,
    'n8n-nodes-base.n8n': 1,
    'n8n-nodes-base.n8nTrainingCustomerDatastore': 1,
    'n8n-nodes-base.n8nTrainingCustomerMessenger': 1,
    'n8n-nodes-base.n8nTrigger': 1,
    'n8n-nodes-base.nasa': 1,
    'n8n-nodes-base.nasaTool': 1,
    'n8n-nodes-base.netlify': 1,
    'n8n-nodes-base.netlifyTool': 1,
    'n8n-nodes-base.netlifyTrigger': 1,
    'n8n-nodes-base.nextCloud': 1,
    'n8n-nodes-base.nextCloudTool': 1,
    'n8n-nodes-base.noOp': 1,
    'n8n-nodes-base.nocoDb': 4,
    'n8n-nodes-base.nocoDbTool': 4,
    'n8n-nodes-base.notion': 3,
    'n8n-nodes-base.notionTool': 3,
    'n8n-nodes-base.notionTrigger': 1.1,
    'n8n-nodes-base.npm': 1,
    'n8n-nodes-base.npmTool': 1,
    'n8n-nodes-base.odoo': 2,
    'n8n-nodes-base.odooTool': 2,
    'n8n-nodes-base.okta': 1,
    'n8n-nodes-base.oktaTool': 1,
    'n8n-nodes-base.oneSimpleApi': 1,
    'n8n-nodes-base.oneSimpleApiTool': 1,
    'n8n-nodes-base.onfleet': 1,
    'n8n-nodes-base.onfleetTool': 1,
    'n8n-nodes-base.onfleetTrigger': 1,
    'n8n-nodes-base.openAi': 1.1,
    'n8n-nodes-base.openThesaurus': 1,
    'n8n-nodes-base.openThesaurusTool': 1,
    'n8n-nodes-base.openWeatherMap': 1,
    'n8n-nodes-base.openWeatherMapTool': 1,
    'n8n-nodes-base.oracleDatabase': 1,
    'n8n-nodes-base.oracleDatabaseTool': 1,
    'n8n-nodes-base.orbit': 1,
    'n8n-nodes-base.oura': 1,
    'n8n-nodes-base.ouraTool': 1,
    'n8n-nodes-base.paddle': 1,
    'n8n-nodes-base.paddleTool': 1,
    'n8n-nodes-base.pagerDuty': 1,
    'n8n-nodes-base.pagerDutyTool': 1,
    'n8n-nodes-base.payPal': 1,
    'n8n-nodes-base.payPalTrigger': 1,
    'n8n-nodes-base.peekalink': 1,
    'n8n-nodes-base.peekalinkTool': 1,
    'n8n-nodes-base.perplexity': 2,
    'n8n-nodes-base.perplexityTool': 2,
    'n8n-nodes-base.phantombuster': 1,
    'n8n-nodes-base.phantombusterTool': 1,
    'n8n-nodes-base.philipsHue': 1,
    'n8n-nodes-base.philipsHueTool': 1,
    'n8n-nodes-base.pipedrive': 2,
    'n8n-nodes-base.pipedriveTool': 2,
    'n8n-nodes-base.pipedriveTrigger': 1.1,
    'n8n-nodes-base.plivo': 1,
    'n8n-nodes-base.plivoTool': 1,
    'n8n-nodes-base.postBin': 1,
    'n8n-nodes-base.postBinTool': 1,
    'n8n-nodes-base.postHog': 1,
    'n8n-nodes-base.postHogTool': 1,
    'n8n-nodes-base.postgres': 2.6,
    'n8n-nodes-base.postgresTool': 2.6,
    'n8n-nodes-base.postgresTrigger': 1,
    'n8n-nodes-base.postmarkTrigger': 1,
    'n8n-nodes-base.profitWell': 1,
    'n8n-nodes-base.profitWellTool': 1,
    'n8n-nodes-base.pushbullet': 1,
    'n8n-nodes-base.pushbulletTool': 1,
    'n8n-nodes-base.pushcut': 1,
    'n8n-nodes-base.pushcutTool': 1,
    'n8n-nodes-base.pushcutTrigger': 1,
    'n8n-nodes-base.pushover': 1,
    'n8n-nodes-base.pushoverTool': 1,
    'n8n-nodes-base.questDb': 1,
    'n8n-nodes-base.questDbTool': 1,
    'n8n-nodes-base.quickChart': 1,
    'n8n-nodes-base.quickChartTool': 1,
    'n8n-nodes-base.quickbase': 1,
    'n8n-nodes-base.quickbaseTool': 1,
    'n8n-nodes-base.quickbooks': 1,
    'n8n-nodes-base.quickbooksTool': 1,
    'n8n-nodes-base.rabbitmq': 1.2,
    'n8n-nodes-base.rabbitmqTool': 1.2,
    'n8n-nodes-base.rabbitmqTrigger': 1,
    'n8n-nodes-base.raindrop': 1,
    'n8n-nodes-base.raindropTool': 1,
    'n8n-nodes-base.readBinaryFile': 1,
    'n8n-nodes-base.readBinaryFiles': 1,
    'n8n-nodes-base.readPDF': 1,
    'n8n-nodes-base.readWriteFile': 1.1,
    'n8n-nodes-base.reddit': 1,
    'n8n-nodes-base.redditTool': 1,
    'n8n-nodes-base.redis': 1,
    'n8n-nodes-base.redisTool': 1,
    'n8n-nodes-base.redisTrigger': 1,
    'n8n-nodes-base.removeDuplicates': 2,
    'n8n-nodes-base.renameKeys': 1,
    'n8n-nodes-base.respondToWebhook': 1.5,
    'n8n-nodes-base.rocketchat': 1,
    'n8n-nodes-base.rocketchatTool': 1,
    'n8n-nodes-base.rssFeedRead': 1.2,
    'n8n-nodes-base.rssFeedReadTool': 1.2,
    'n8n-nodes-base.rssFeedReadTrigger': 1,
    'n8n-nodes-base.rundeck': 1.1,
    'n8n-nodes-base.rundeckTool': 1.1,
    'n8n-nodes-base.s3': 1,
    'n8n-nodes-base.s3Tool': 1,
    'n8n-nodes-base.salesforce': 1.1,
    'n8n-nodes-base.salesforceTool': 1.1,
    'n8n-nodes-base.salesforceTrigger': 1.1,
    'n8n-nodes-base.salesmate': 1,
    'n8n-nodes-base.salesmateTool': 1,
    'n8n-nodes-base.scheduleTrigger': 1.3,
    'n8n-nodes-base.seaTable': 2,
    'n8n-nodes-base.seaTableTool': 2,
    'n8n-nodes-base.seaTableTrigger': 2,
    'n8n-nodes-base.securityScorecard': 1,
    'n8n-nodes-base.securityScorecardTool': 1,
    'n8n-nodes-base.segment': 1,
    'n8n-nodes-base.segmentTool': 1,
    'n8n-nodes-base.sendGrid': 1,
    'n8n-nodes-base.sendGridTool': 1,
    'n8n-nodes-base.sendInBlue': 1,
    'n8n-nodes-base.sendInBlueTool': 1,
    'n8n-nodes-base.sendInBlueTrigger': 1,
    'n8n-nodes-base.sendy': 1,
    'n8n-nodes-base.sendyTool': 1,
    'n8n-nodes-base.sentryIo': 1,
    'n8n-nodes-base.sentryIoTool': 1,
    'n8n-nodes-base.serviceNow': 1,
    'n8n-nodes-base.serviceNowTool': 1,
    'n8n-nodes-base.set': 3.4,
    'n8n-nodes-base.shopify': 1,
    'n8n-nodes-base.shopifyTool': 1,
    'n8n-nodes-base.shopifyTrigger': 1,
    'n8n-nodes-base.signl4': 1,
    'n8n-nodes-base.signl4Tool': 1,
    'n8n-nodes-base.simulate': 1,
    'n8n-nodes-base.simulateTrigger': 1,
    'n8n-nodes-base.slack': 2.5,
    'n8n-nodes-base.slackTool': 2.5,
    'n8n-nodes-base.slackTrigger': 1,
    'n8n-nodes-base.sms77': 1,
    'n8n-nodes-base.sms77Tool': 1,
    'n8n-nodes-base.snowflake': 1,
    'n8n-nodes-base.snowflakeTool': 1,
    'n8n-nodes-base.sort': 1,
    'n8n-nodes-base.splitInBatches': 3,
    'n8n-nodes-base.splitOut': 1,
    'n8n-nodes-base.splunk': 2,
    'n8n-nodes-base.splunkTool': 2,
    'n8n-nodes-base.spotify': 1,
    'n8n-nodes-base.spotifyTool': 1,
    'n8n-nodes-base.spreadsheetFile': 2,
    'n8n-nodes-base.sseTrigger': 1,
    'n8n-nodes-base.ssh': 1,
    'n8n-nodes-base.stackby': 1,
    'n8n-nodes-base.stackbyTool': 1,
    'n8n-nodes-base.stickyNote': 1,
    'n8n-nodes-base.stopAndError': 1,
    'n8n-nodes-base.storyblok': 1,
    'n8n-nodes-base.storyblokTool': 1,
    'n8n-nodes-base.strapi': 1,
    'n8n-nodes-base.strapiTool': 1,
    'n8n-nodes-base.strava': 1.1,
    'n8n-nodes-base.stravaTool': 1.1,
    'n8n-nodes-base.stravaTrigger': 1,
    'n8n-nodes-base.stripe': 1,
    'n8n-nodes-base.stripeTool': 1,
    'n8n-nodes-base.stripeTrigger': 1,
    'n8n-nodes-base.summarize': 1.1,
    'n8n-nodes-base.supabase': 1,
    'n8n-nodes-base.supabaseTool': 1,
    'n8n-nodes-base.surveyMonkeyTrigger': 1,
    'n8n-nodes-base.switch': 3.4,
    'n8n-nodes-base.syncroMsp': 1,
    'n8n-nodes-base.syncroMspTool': 1,
    'n8n-nodes-base.taiga': 1,
    'n8n-nodes-base.taigaTool': 1,
    'n8n-nodes-base.taigaTrigger': 1,
    'n8n-nodes-base.tapfiliate': 1,
    'n8n-nodes-base.tapfiliateTool': 1,
    'n8n-nodes-base.telegram': 1.2,
    'n8n-nodes-base.telegramTool': 1.2,
    'n8n-nodes-base.telegramTrigger': 1.4,
    'n8n-nodes-base.theHive': 1,
    'n8n-nodes-base.theHiveProject': 1,
    'n8n-nodes-base.theHiveProjectTool': 1,
    'n8n-nodes-base.theHiveProjectTrigger': 1,
    'n8n-nodes-base.theHiveTool': 1,
    'n8n-nodes-base.theHiveTrigger': 2,
    'n8n-nodes-base.timeSaved': 1,
    'n8n-nodes-base.timescaleDb': 1,
    'n8n-nodes-base.timescaleDbTool': 1,
    'n8n-nodes-base.todoist': 2.2,
    'n8n-nodes-base.todoistTool': 2.2,
    'n8n-nodes-base.togglTrigger': 1,
    'n8n-nodes-base.totp': 1,
    'n8n-nodes-base.totpTool': 1,
    'n8n-nodes-base.travisCi': 1,
    'n8n-nodes-base.travisCiTool': 1,
    'n8n-nodes-base.trello': 1,
    'n8n-nodes-base.trelloTool': 1,
    'n8n-nodes-base.trelloTrigger': 1,
    'n8n-nodes-base.twake': 1,
    'n8n-nodes-base.twakeTool': 1,
    'n8n-nodes-base.twilio': 1,
    'n8n-nodes-base.twilioTool': 1,
    'n8n-nodes-base.twilioTrigger': 1,
    'n8n-nodes-base.twist': 1,
    'n8n-nodes-base.twistTool': 1,
    'n8n-nodes-base.twitter': 2,
    'n8n-nodes-base.twitterTool': 2,
    'n8n-nodes-base.typeformTrigger': 1.1,
    'n8n-nodes-base.unleashedSoftware': 1,
    'n8n-nodes-base.unleashedSoftwareTool': 1,
    'n8n-nodes-base.uplead': 1,
    'n8n-nodes-base.upleadTool': 1,
    'n8n-nodes-base.uproc': 1,
    'n8n-nodes-base.uprocTool': 1,
    'n8n-nodes-base.uptimeRobot': 1,
    'n8n-nodes-base.uptimeRobotTool': 1,
    'n8n-nodes-base.urlScanIo': 1,
    'n8n-nodes-base.urlScanIoTool': 1,
    'n8n-nodes-base.venafiTlsProtectCloud': 1,
    'n8n-nodes-base.venafiTlsProtectCloudTool': 1,
    'n8n-nodes-base.venafiTlsProtectCloudTrigger': 1,
    'n8n-nodes-base.venafiTlsProtectDatacenter': 1,
    'n8n-nodes-base.venafiTlsProtectDatacenterTool': 1,
    'n8n-nodes-base.venafiTlsProtectDatacenterTrigger': 1,
    'n8n-nodes-base.vero': 1,
    'n8n-nodes-base.veroTool': 1,
    'n8n-nodes-base.vonage': 1,
    'n8n-nodes-base.vonageTool': 1,
    'n8n-nodes-base.wait': 1.1,
    'n8n-nodes-base.webflow': 2,
    'n8n-nodes-base.webflowTool': 2,
    'n8n-nodes-base.webflowTrigger': 2,
    'n8n-nodes-base.webhook': 2.1,
    'n8n-nodes-base.wekan': 1,
    'n8n-nodes-base.wekanTool': 1,
    'n8n-nodes-base.whatsApp': 1.1,
    'n8n-nodes-base.whatsAppTool': 1.1,
    'n8n-nodes-base.whatsAppTrigger': 1,
    'n8n-nodes-base.wise': 1,
    'n8n-nodes-base.wiseTrigger': 1,
    'n8n-nodes-base.wooCommerce': 1,
    'n8n-nodes-base.wooCommerceTool': 1,
    'n8n-nodes-base.wooCommerceTrigger': 1,
    'n8n-nodes-base.wordpress': 1,
    'n8n-nodes-base.wordpressTool': 1,
    'n8n-nodes-base.workableTrigger': 1,
    'n8n-nodes-base.workflowTrigger': 1,
    'n8n-nodes-base.writeBinaryFile': 1,
    'n8n-nodes-base.wufooTrigger': 1,
    'n8n-nodes-base.xero': 1,
    'n8n-nodes-base.xeroTool': 1,
    'n8n-nodes-base.xml': 1,
    'n8n-nodes-base.youTube': 1,
    'n8n-nodes-base.youTubeTool': 1,
    'n8n-nodes-base.yourls': 1,
    'n8n-nodes-base.yourlsTool': 1,
    'n8n-nodes-base.zammad': 1,
    'n8n-nodes-base.zammadTool': 1,
    'n8n-nodes-base.zendesk': 1,
    'n8n-nodes-base.zendeskTool': 1,
    'n8n-nodes-base.zendeskTrigger': 1,
    'n8n-nodes-base.zohoCrm': 1,
    'n8n-nodes-base.zohoCrmTool': 1,
    'n8n-nodes-base.zoom': 1,
    'n8n-nodes-base.zoomTool': 1,
    'n8n-nodes-base.zulip': 1,
    'n8n-nodes-base.zulipTool': 1,
}


# --- Health сценария (workflow_health): число ошибок валидации ПОСЛЕ санации ---
# Меряно валидатором n8n-as-code по санированному /download. error_count = число
# структурных ошибок (кроме версий - их чинит санация). 0 = чистый (витрина),
# >=11 = безнадёжный (фронт ставит noindex). Отдельная таблица - переживает reindex.
def init_health_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_health (
                filename    TEXT PRIMARY KEY,
                error_count INTEGER NOT NULL,
                measured_at TEXT DEFAULT (datetime('now'))
            )
        """)

def enrich_health_many(items):
    filenames=[i.get("filename") for i in items if i.get("filename")]
    hmap={}
    if filenames:
        try:
            ph=",".join("?"*len(filenames))
            with _conn() as c:
                rows=c.execute(f"SELECT filename, error_count FROM workflow_health WHERE filename IN ({ph})", filenames).fetchall()
            hmap={r["filename"]: r["error_count"] for r in rows}
        except Exception:
            pass
    for i in items:
        i["error_count"]=hmap.get(i.get("filename"))
    return items

def upsert_health(filename: str, error_count: int) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO workflow_health (filename, error_count, measured_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(filename) DO UPDATE SET error_count=excluded.error_count, measured_at=datetime('now')""",
            (filename, error_count))


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
        # 3) Понизить несуществующие typeVersion до макс. валидной (по справочнику
        #    n8n-as-code). Частый дефект коллекции: typeVersion выше, чем есть у ноды
        #    в текущем n8n - импорт/выполнение падает «typeVersion X does not exist».
        for _n in nodes:
            _t = _n.get('type'); _v = _n.get('typeVersion')
            if _t in _NODE_MAX_VERSION and isinstance(_v, (int, float)):
                _mx = _NODE_MAX_VERSION[_t]
                if _v > _mx:
                    _n['typeVersion'] = _mx
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


# ── Canonical для дублей (workflow_canonical) ────────────────────────────────
# У части сценариев есть дубли (одинаковый контент под разными filename). НЕ удаляем
# (иначе 404 + потеря SEO-веса), а помечаем: дубль -> основная. Фронт ставит
# <link rel="canonical"> на основную. Отдельная таблица - reindex её не затрёт.
def init_canon_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_canonical (
                filename           TEXT PRIMARY KEY,
                canonical_filename TEXT NOT NULL,
                created_at         TEXT DEFAULT (datetime('now'))
            )
        """)

def enrich_canon_many(items):
    """Добавить canonical_filename по filename. Для не-дублей = None (фронт канонит на себя)."""
    filenames = [i.get("filename") for i in items if i.get("filename")]
    canon_map = {}
    if filenames:
        try:
            ph = ",".join("?" * len(filenames))
            with _conn() as c:
                rows = c.execute(
                    f"SELECT filename, canonical_filename FROM workflow_canonical WHERE filename IN ({ph})",
                    filenames,
                ).fetchall()
            canon_map = {r["filename"]: r["canonical_filename"] for r in rows}
        except Exception:
            pass
    for i in items:
        i["canonical_filename"] = canon_map.get(i.get("filename"))
    return items

def upsert_canonical(filename: str, canonical_filename: str) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO workflow_canonical (filename, canonical_filename, created_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(filename) DO UPDATE SET canonical_filename=excluded.canonical_filename
        """, (filename, canonical_filename))


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
