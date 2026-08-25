#!/usr/bin/env python3
"""
seed_canonical.py - разметка дублей сценариев в таблице workflow_canonical.

Часть сценариев каталога - точные дубли (идентичный workflow под разными
filename). Помечаем дубль -> основную (canonical): фронт ставит
<link rel="canonical"> на основную и исключает дубль из sitemap. НЕ удаляем
(иначе 404 + потеря SEO-веса).

Пары подтверждены СТРОГО: сравнение нормализованного workflow JSON (без
волатильных id/meta/webhookId). Ложные «дубли» (одинаковый РЕНДЕР страницы, но
разный workflow) сюда НЕ входят - их лечит фронт (текстовый разбор шагов).

Основная (canonical) в паре = сценарий с МЕНЬШИМ ведущим номером (раньше
добавлен, стабильнее для уже проиндексированных URL).

Идемпотентно (ON CONFLICT DO UPDATE). Запуск на VM каталога рядом с фасадом:
    python3 seed_canonical.py
Данные версионируются в git (форк n8n-workflows) - reindex/пересборка не трёт.
"""
from fatmetal_facade import init_canon_table, upsert_canonical

# (дубль, основная-canonical) - 40 подтверждённых пар
CANONICAL_PAIRS = [
    ('0020_Mattermost_Emelia_Automate_Triggered.json', '0017_Mattermost_Emelia_Automate_Triggered.json'),
    ('0214_Manual_Markdown_Create_Webhook.json', '0213_Manual_Markdown_Create_Webhook.json'),
    ('0661_Calendly_Noop_Create_Triggered.json', '0660_Calendly_Noop_Create_Triggered.json'),
    ('1013_Manual_Bannerbear_Automate_Triggered.json', '1012_Manual_Bannerbear_Automate_Triggered.json'),
    ('1309_Mattermost_Googlecloudnaturallanguage_Send_Triggered.json', '0132_Mattermost_Googlecloudnaturallanguage_Send_Triggered.json'),
    ('1360_Manual_Stickynote_Create_Triggered.json', '1303_Manual_Stickynote_Create_Triggered.json'),
    ('1364_Extractfromfile_Manual_Create_Webhook.json', '0601_Extractfromfile_Manual_Create_Webhook.json'),
    ('1365_Extractfromfile_Manual_Create_Webhook.json', '0601_Extractfromfile_Manual_Create_Webhook.json'),
    ('1394_Manual_Humanticai_Create_Webhook.json', '0110_Manual_Humanticai_Create_Webhook.json'),
    ('1416_Webhook_Respondtowebhook_Create_Webhook.json', '1415_Webhook_Respondtowebhook_Create_Webhook.json'),
    ('1421_Postgres_Googlecloudnaturallanguage_Automation_Scheduled.json', '1108_Postgres_Googlecloudnaturallanguage_Automation_Scheduled.json'),
    ('1425_Splitout_Elasticsearch_Create_Webhook.json', '0532_Splitout_Elasticsearch_Create_Webhook.json'),
    ('1433_Webhook_Respondtowebhook_Automate_Webhook.json', '1432_Webhook_Respondtowebhook_Automate_Webhook.json'),
    ('1441_Form_Automation_Triggered.json', '1348_Form_Automation_Triggered.json'),
    ('1513_Wait_Splitout_Process_Webhook.json', '1512_Wait_Splitout_Process_Webhook.json'),
    ('1554_Form_GoogleSheets_Automation_Triggered.json', '1537_Form_GoogleSheets_Automation_Triggered.json'),
    ('1608_Respondtowebhook_Stickynote_Automation_Webhook.json', '1311_Respondtowebhook_Stickynote_Automation_Webhook.json'),
    ('1618_Openai_GoogleSheets_Create_Triggered.json', '1177_Openai_GoogleSheets_Create_Triggered.json'),
    ('1626_Stickynote_GoogleDrive_Automate_Triggered.json', '1141_Stickynote_GoogleDrive_Automate_Triggered.json'),
    ('1652_Googleanalytics_Code_Automation_Webhook.json', '1480_Googleanalytics_Code_Automation_Webhook.json'),
    ('1654_HTTP_Telegram_Send_Webhook.json', '0162_HTTP_Telegram_Send_Webhook.json'),
    ('1658_Splitout_Schedule_Monitor_Scheduled.json', '1657_Splitout_Schedule_Monitor_Scheduled.json'),
    ('1663_Slack_Stickynote_Automate_Webhook.json', '1592_Slack_Stickynote_Automate_Webhook.json'),
    ('1675_HTTP_Emailreadimap_Send_Webhook.json', '1674_HTTP_Emailreadimap_Send_Webhook.json'),
    ('1683_Compression_Manual_Automation_Webhook.json', '1294_Compression_Manual_Automation_Webhook.json'),
    ('1686_Telegram_Stickynote_Automate_Triggered.json', '1485_Telegram_Stickynote_Automate_Triggered.json'),
    ('1734_Stickynote_Automation_Triggered.json', '1719_Stickynote_Automation_Triggered.json'),
    ('1803_Respondtowebhook_Stickynote_Import_Webhook.json', '1476_Respondtowebhook_Stickynote_Import_Webhook.json'),
    ('1804_Stickynote_Automation_Triggered.json', '1379_Stickynote_Automation_Triggered.json'),
    ('1808_HTTP_Telegram_Automate_Webhook.json', '1687_HTTP_Telegram_Automate_Webhook.json'),
    ('1826_Manual_Wordpress_Automation_Triggered.json', '1322_Manual_Wordpress_Automation_Triggered.json'),
    ('1891_Schedule_Slack_Automation_Scheduled.json', '1406_Schedule_Slack_Automation_Scheduled.json'),
    ('1922_Linkedin_Schedule_Automate_Webhook.json', '1330_Linkedin_Schedule_Automate_Webhook.json'),
    ('1938_Telegram_Schedule_Automation_Scheduled.json', '0001_Telegram_Schedule_Automation_Scheduled.json'),
    ('1949_Wordpress_Manual_Automate_Webhook.json', '1327_Wordpress_Manual_Automate_Webhook.json'),
    ('1967_Respondtowebhook_Stickynote_Automation_Webhook.json', '1266_Respondtowebhook_Stickynote_Automation_Webhook.json'),
    ('1980_Splitout_Code_Automation_Webhook.json', '1323_Splitout_Code_Automation_Webhook.json'),
    ('1996_HTTP_Manual_Automation_Webhook.json', '1334_HTTP_Manual_Automation_Webhook.json'),
    ('1997_Respondtowebhook_Stickynote_Automation_Webhook.json', '1387_Respondtowebhook_Stickynote_Automation_Webhook.json'),
    ('2048_Stickynote_Automation_Triggered.json', '1691_Stickynote_Automation_Triggered.json'),
]

def main():
    init_canon_table()
    for dup, canon in CANONICAL_PAIRS:
        upsert_canonical(dup, canon)
    print(f"seed_canonical: размечено пар: {len(CANONICAL_PAIRS)}")

if __name__ == "__main__":
    main()
