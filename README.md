# HLD Agent — Backend

Backend ИИ-ассистента аналитика: анализ текста требований, выявление Business/Technical
Capability, поиск готовых возможностей на ИТ-ландшафте, генерация HLD-отчёта и публикация
в Confluence.

**FastAPI (Python 3.11), stateless, без базы данных.** Состояние пользовательской сессии
хранит фронтенд (localStorage); на этапах с длительными LLM-операциями backend держит
встроенные in-memory хранилища фоновых задач (см. [Паттерн длительных задач](#паттерн-длительных-задач)).

---

## Содержание

- [Workflow](#workflow)
- [Структура репозитория](#структура-репозитория)
- [Запуск](#запуск)
- [Конфигурация](#конфигурация)
- [API](#api)
  - [Общие сведения](#общие-сведения)
  - [Паттерн длительных задач](#паттерн-длительных-задач)
  - [Health](#health)
  - [Intake](#intake--apiintake)
  - [Business Capability](#business-capability--apibc)
  - [Technical Capability](#technical-capability--apitc)
  - [Landscape](#landscape--apilandscape-экспериментальный-модуль)
  - [Publish](#publish--apipublish)
- [Ключевые механизмы](#ключевые-механизмы)
- [Внешние интеграции](#внешние-интеграции)
- [LLM-промпты](#llm-промпты)
- [Тесты](#тесты)
- [Известные особенности](#известные-особенности)

---

## Workflow

```
Intake ──→ BC ──→ TC ──→ Impact ──→ Publish
```

| Этап | Что делает | Где реализован |
|---|---|---|
| **Intake** | Импорт требований (текст или Confluence), разбиение на FR/NFR/OQ через LLM | `app/api/intake.py`, `app/core/intake.py` |
| **BC** (Business Capability) | Выявление бизнес-возможностей, релевантных задаче | `app/api/bc.py`, `app/core/bc.py` |
| **TC** (Technical Capability) | Выявление TC из FR + поиск готовых TC на ландшафте (fdm-search), выбор reuse / create_new | `app/api/tc.py`, `app/core/tc.py` |
| **Impact** | Оценка влияния на системы. Уровень S/M/L/XL считается **на фронтенде**; backend принимает уже подтверждённые TC | — (только приём данных в `/api/publish/*`) |
| **Publish** | Генерация HLD-отчёта (Markdown) и публикация в Confluence | `app/api/publish.py`, `app/core/publish.py` |

Каждый этап — отдельный модуль API (`app/api/*.py`) и модуль бизнес-логики
(`app/core/*.py`); ядро не хранит состояние между вызовами.

---

## Структура репозитория

```
solution-checker-backtend/
├── app/
│   ├── main.py                 # точка входа FastAPI: CORS, middleware логирования, регистрация роутеров
│   ├── config.py               # Settings (pydantic-settings) — единый источник env-конфигурации
│   ├── api/                    # REST-роуты; Pydantic-модели запросов/ответов объявлены здесь же
│   │   ├── health.py           #   GET /api/health
│   │   ├── intake.py           #   импорт + structure/start|progress|result
│   │   ├── bc.py               #   выявление BC: identify/start|progress|result
│   │   ├── tc.py               #   systems, describe-task, identify/*, поиск TC (sync + async)
│   │   ├── landscape.py        #   экспериментальный модуль: живой каталог (analyze/*, bc/search)
│   │   ├── publish.py          #   экспорт Markdown + публикация в Confluence
│   │   └── task_store.py       #   cleanup_expired — очистка истёкших фоновых задач по TTL
│   ├── core/                   # бизнес-логика (stateless)
│   │   ├── intake.py           #   импорт, чанкинг, структуризация, merge, парсинг LLM-JSON
│   │   ├── bc.py               #   выявление BC (промпт со вшитым каталогом возможностей)
│   │   ├── tc.py               #   описание задачи, выявление TC, поиск на ландшафте, дедупликация
│   │   ├── landscape.py        #   анализ кандидатов TC по живому каталогу, type-ahead поиск BC
│   │   └── publish.py          #   генерация Markdown, публикация, Markdown → Confluence Storage
│   ├── integrations/           # клиенты внешних систем (singleton на уровне модуля)
│   │   ├── llm_client.py       #   LLMClient (OpenAI-compatible) + disable_thinking_params()
│   │   ├── confluence.py       #   чтение/запись страниц, include-макросы, Storage → текст
│   │   └── beeatlas.py         #   HMAC-подпись, fdm-search (search_capability_v2), системы
│   ├── prompts/                # LLM-промпты (*.txt), читаются при вызове
│   │   └── ...                 #   см. раздел «LLM-промпты»
│   └── models/                 # зарезервировано; сейчас пусто — модели живут в app/api/*.py
├── tests/                      # pytest (68 тестов), фикстуры client объявлены в каждом файле
├── Dockerfile                  # прод-образ (python base + CA-сертификаты), порт 8080
├── Dockerfile.dev              # dev-образ (без блока сертификатов)
├── requirements.txt            # fastapi, uvicorn, pydantic, httpx, pytest, respx, beautifulsoup4
├── .bumpversion.cfg            # версия пакета (bump2version)
├── .gitlab-ci/helm/            # helm-values dev-окружения (env, vault-секреты, ingress)
└── .env                        # локальная конфигурация (в .gitignore)
```

Запускать `uvicorn` нужно **из корня репозитория**: промпты читаются по относительному
пути `app/prompts/<name>.txt`.

---

## Запуск

### Локально

```bash
cd solution-checker-backtend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# создайте .env — образца .env.example в репозитории нет,
# перечень переменных и дефолтов — в разделе «Конфигурация»
uvicorn app.main:app --reload --port 8000
```

- API: <http://localhost:8000/api>
- Swagger UI (автогенерируется FastAPI): <http://localhost:8000/docs>

### Через docker-compose

Из родительского каталога `solution-checker/` (поднимает backend и frontend вместе):

```bash
docker compose up --build
# backend  → http://localhost:8000
# frontend → http://localhost:8080
```

### Тесты

```bash
pytest                    # весь набор
pytest tests/test_tc.py   # один модуль
```

---

## Конфигурация

Читается в `app/config.py` (`pydantic-settings`, `env_file=".env"`, `extra="ignore"`).
Неизвестные ключи в `.env` игнорируются.

### Общие

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `AGENT_NAME` | str | `HLD Agent` | Заголовок FastAPI-приложения |
| `CORS_ORIGINS` | str | `http://localhost:5173,http://localhost:3000` | Разрешённые origin через запятую |

### LLM

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `LLM_API_URL` | str | `https://api.deepseek.com` | База OpenAI-совместимого API; вызов `{URL}/chat/completions` |
| `LLM_MODEL` | str | `deepseek-chat` | Имя модели |
| `LLM_API_KEY` | str | `""` | Bearer-токен; пустой → health отдаёт `llm: unknown` |
| `LLM_TIMEOUT_SECONDS` | int | `30` | Таймаут HTTP-запроса к LLM |
| `LLM_MAX_TOKENS` | int | `16000` | Верхняя граница выходных токенов (клампится под контекст модели) |
| `LLM_TEMPERATURE` | float | `0.2` | Температура генерации |
| `LLM_RETRY_COUNT` | int | `3` | База числа попыток; фактически `max(значение, 6)` |
| `LLM_ENABLE_THINKING` | bool | `False` | Глобальный выключатель reasoning у thinking-моделей. Выключен осознанно: токены рассуждений расходуются из того же `max_tokens`, и на больших входах ответ приходил пустым при `status=ok` (пустые требования/TC на фронте) |
| `LLM_THINKING_PROMPTS` | str | `merge_dedup,merge_tc` | Исключения: промпты, у которых reasoning остаётся включённым. Дедупликация — задача на сопоставление дубликатов, где рассуждения повышают качество слияния |
| `LLM_THINKING_MAX_TOKENS` | int | `32768` | Бюджет вывода для промптов с reasoning (рассуждения делят лимит с ответом, поэтому он задаётся с запасом). `0` — не переопределять, использовать `LLM_MAX_TOKENS` |

### Выявление TC

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `TC_IDENTIFY_CHUNK_SIZE` | int | `30` | Порог числа FR, выше которого включается чанкованный режим + merge |

### Confluence

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `CONFLUENCE_TIMEOUT_SECONDS` | int | `30` | Таймаут запросов. Base URL, space key и PAT передаются с фронтенда в теле запроса |

### BeeAtlas / fdm-search

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `BEEATLAS_API_URL` | str | `""` | База шлюза; пусто → запросы пропускаются |
| `BEEATLAS_API_KEY` | str | `""` | Часть `X-Authorization` до двоеточия |
| `BEEATLAS_API_SECRET` | str | `""` | Ключ HMAC-SHA256 |
| `BEEATLAS_TIMEOUT_SECONDS` | int | `30` | Таймаут запросов |
| `FDM_SEARCH_TOP_K` | int | `10` | Сколько результатов возвращает поиск TC |
| `FDM_SEARCH_EXCLUDE_SYSTEMS` | str | `""` | Системы-исключения через запятую; пусто → параметр не передаётся |
| `FDM_BC_TOP_K` | int | `10` | Сколько BC искать на кандидата в `/api/landscape/analyze` |
| `FDM_TC_PER_BC_TOP_K` | int | `5` | Сколько TC искать внутри каждого BC |

### Фоновые задачи

| Переменная | Тип | Дефолт | Назначение |
|---|---|---|---|
| `TASK_TTL_SECONDS` | int | `86400` (24 ч) | Сколько задача живёт в in-memory хранилище |

---

## API

### Общие сведения

| Параметр | Значение |
|---|---|
| Базовый путь | `/api` |
| Формат | `application/json` (исключение — `POST /api/publish/export`, отдаёт файл) |
| OpenAPI | `/docs` (Swagger UI), `/openapi.json` |
| Аутентификация | нет; ключи внешних систем передаются в теле запроса (Confluence PAT) или в env (LLM, BeeAtlas) |
| Версия приложения | `0.1.0` (в ответе `/api/health`) |

Ошибки — стандартный FastAPI `HTTPException`:

```json
{ "detail": "человекочитаемое сообщение на русском" }
```

| Код | Когда |
|---|---|
| `400` | Невалидный вход или ошибка бизнес-логики; при чтении результата — ошибка фоновой задачи |
| `404` | Неизвестный `task_id`; не найдена страница/родитель в Confluence |
| `425` | Задача ещё не завершена (результат запрошен раньше времени) |
| `500` | Непредвиденная ошибка (в т.ч. сбой LLM на синхронных эндпоинтах) |
| `502` | Ошибка внешнего API при публикации в Confluence |

### Паттерн длительных задач

Операции с LLM выполняются в фоне по единой схеме из трёх эндпоинтов:

```
1. POST  .../start                → { "task_id": "<uuid>" }
2. GET   .../{task_id}/progress   → { ..., "done": false|true, "error": null|"..." }
3. GET   .../{task_id}/result     → результат | 425 пока не готово | 400 при ошибке задачи
```

- `start` создаёт запись в in-memory словаре, запускает `asyncio.create_task` и сразу возвращает
  `task_id` (HTTP 200, не 202).
- `progress` отдаёт текущую фазу и статистику; `done=true` появляется, когда результат готов
  **или** произошла ошибка (тогда заполнен `error`).
- `result` живёт до истечения TTL (`TASK_TTL_SECONDS`, по умолчанию 24 ч); истёкшие задачи
  удаляются при старте новых (`cleanup_expired`), а не по таймеру.

| Группа | start | progress / result | Хранилище | Фазы |
|---|---|---|---|---|
| Intake | `POST /api/intake/structure/start` | `GET /api/intake/structure/{task_id}/…` | `_structure_tasks` | `chunking → processing → merging → done` |
| BC | `POST /api/bc/identify/start` | `GET /api/bc/identify/{task_id}/…` | `_bc_tasks` | `identifying → done` |
| TC identify | `POST /api/tc/identify/start` | `GET /api/tc/identify/{task_id}/…` | `_identify_tasks` | `sending/processing → merging → done` |
| TC поиск | `POST /api/tc/search/start` | `GET /api/tc/search/{task_id}/…` | `_search_tasks` | `searching → done` |
| Landscape | `POST /api/landscape/analyze/start` | `GET /api/landscape/analyze/{task_id}/…` | `_analyze_tasks` | `starting → analyzing → tc_searching → done` |

> ⚠️ Хранилища **процесс-локальные**: при нескольких репликах пода запрос `progress`/`result`
> может попасть на инстанс, где задачи нет (вернётся `404`). Хранилища сбрасываются при
> перезапуске сервиса.

### Health

**`GET /api/health`** — проверка backend и внешних зависимостей.

Ответ (200):

```json
{
  "status": "ok",
  "timestamp": "2026-09-11T05:19:42.000Z",
  "version": "0.1.0",
  "integrations": {
    "backend": "ok",
    "llm": "ok",
    "beeatlas": "ok",
    "fdm_search": "ok"
  }
}
```

`status` = `degraded`, если хотя бы одна интеграция в `error`. Значения полей:
`ok` / `error` / `unknown` (не сконфигурировано) / `not_configured`.
Пробы: LLM — `POST {LLM_API_URL}/chat/completions` (`max_tokens: 10`, timeout 5 с);
BeeAtlas — `GET /api-gateway/product/v1/product/fdmshowcaseapp/patterns`;
fdm-search — `GET /search/api/v1/search?query=test&limit=1`.

### Intake — `/api/intake`

**`POST /api/intake/import`** — импорт требований из текста или Confluence.

| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `text` | str \| null | * | Исходный текст |
| `confluence_url` | str \| null | * | URL страницы Confluence |
| `include_child_pages` | bool | нет (false) | Включать дочерние страницы |
| `confluence_pat` | str \| null | нет | PAT для Confluence |

\* Нужно указать ровно один источник, иначе `400`.

Ответ (200): `{ raw_text, source: "text"|"confluence", title, source_url }`.
Коды: `400` (нет источника / не удалось извлечь текст), `500`.

**`POST /api/intake/structure/start`** — структурирование в FR/NFR/OQ.

Тело: `{ "raw_text": "..." }` (пустая строка → `400`). Ответ (200): `{ "task_id": "..." }`.

**`GET /api/intake/structure/{task_id}/progress`** (200 / 404)

```json
{
  "phase": "processing",
  "current_chunk": 3, "total_chunks": 9, "chunk_size": 2947,
  "chars_sent": 5773, "last_response_chars": 4061,
  "attempts": 3, "merge_input_chars": 0, "merge_attempts": 0,
  "done": false, "error": null
}
```

**`GET /api/intake/structure/{task_id}/result`** (200 / 400 / 404 / 425)

```json
{
  "requirements": [
    { "id": "FR-1", "type": "FR", "title": "…", "description": "…" }
  ]
}
```

`type` — `FR` | `NFR` | `OQ`; `id` присваивается последовательно по типу (`FR-1`, `NFR-1`, `OQ-1`, …).

### Business Capability — `/api/bc`

Выявление BC через LLM по промпту со **вшитым полным каталогом** возможностей
(`prompt_top10_with_catalog.txt`).

**`POST /api/bc/identify/start`** — тело `{ "task_text": "..." }` (пустой → `400`).
Ответ: `{ "task_id": "..." }`.

**`GET /api/bc/identify/{task_id}/progress`** (200 / 404)

```json
{ "phase": "...", "data_chars": 9168, "response_chars": 0,
  "attempts": 1, "elapsed_ms": 0, "done": false, "error": null }
```

**`GET /api/bc/identify/{task_id}/result`** (200 / 400 / 404 / 425)

```json
{ "candidates": [ { "code": "BC-019359", "description": "…", "relevance": 85, "reason": "…" } ] }
```

### Technical Capability — `/api/tc`

**`GET /api/tc/systems`** — список систем ландшафта (BeeAtlas), для назначения создаваемым TC.

Query: `query` (необязательный, регистронезависимый фильтр по `name`/`code`).
Ответ: `{ "systems": [ { "code": "...", "name": "...", "description": "..." } ] }`
(сортировка по name, code; при недоступности BeeAtlas — пустой список, без ошибки).

**`POST /api/tc/describe-task`** — краткое описание задачи из текста требований.

Тело: `{ "raw_content": "..." }` (пустой → `400`). Ответ: `{ "task_description": "..." }`.
Промпт — `summarize_task`. Коды: `200` / `400` / `500`.

**`POST /api/tc/identify/start`** — выявление TC из FR.

| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `structured_requirements` | array | да | Требования (пустой список → `400`) |
| `task_description` | str | нет | Краткое описание задачи (контекст) |
| `business_capabilities` | array | нет | `[{"code": "...", "description": "..."}]` — контекст |

Ответ: `{ "task_id": "..." }`. При числе FR больше `TC_IDENTIFY_CHUNK_SIZE` включается
чанкованный режим (последовательные запросы + финальный merge).

**`GET /api/tc/identify/{task_id}/progress`** (200 / 404)

```json
{ "phase": "merging", "total_fr": 224, "total_candidates": 47,
  "data_chars": 24007, "response_chars": 0, "attempts": 9,
  "elapsed_ms": 0, "current_chunk": 9, "total_chunks": 9,
  "merge_input_chars": 26390, "merge_attempts": 0,
  "done": false, "error": null }
```

**`GET /api/tc/identify/{task_id}/result`** (200 / 400 / 404 / 425)

```json
{
  "candidates": [
    { "name": "…", "description": "…", "rationale": "…", "score": 85.0, "fr_ids": ["FR-1", "FR-2"] }
  ]
}
```

**`POST /api/tc/search`** — синхронный поиск TC-кандидата на ландшафте (fdm-search).

| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `tc_candidate_name` | str | да | Имя TC — основа поискового запроса |
| `tc_description` | str | нет | Добавляется в запрос |
| `tc_rationale` | str | нет | Добавляется в запрос |
| `business_capabilities` | array | нет | Коды BC → фильтр `parent`/`domain` в fdm-search |

Ответ (200):

```json
{
  "query": "Управление заказами …",
  "results": [
    { "code": "TC-1024", "name": "…", "description": "…", "score": 0.87,
      "system_code": "SYS-1", "system_name": "…" }
  ]
}
```

Коды: `200` / `500`. При сбое fdm-search возвращается пустой список (не ошибка).

**`POST /api/tc/search/start`** — массовый поиск по списку кандидатов (фон).

Тело: `{ "tc_candidates": [ { "name": "...", "description": "" } ], "business_capabilities": [] }`
(пустой список → `400`). Ответ: `{ "task_id": "..." }`.

**`GET /api/tc/search/{task_id}/progress`** (200 / 404)

```json
{ "phase": "searching", "current_tc": 2, "total_tc": 5,
  "current_tc_name": "…", "elapsed_ms": 1200, "done": false, "error": null }
```

**`GET /api/tc/search/{task_id}/result`** (200 / 400 / 404 / 425) — ключ словаря = имя кандидата:

```json
{ "results": { "Управление заказами": [ { "code": "TC-1", "name": "…", "score": 0.9 } ] } }
```

### Landscape — `/api/landscape` (экспериментальный модуль)

Замена LLM-выявления BC на **живой поиск по каталогу** (`feature/exp-bc-search`). Старые
`/api/bc/*` и `/api/tc/*` оставлены без изменений и работают параллельно.

**`POST /api/landscape/analyze/start`** — анализ кандидатов TC по каталогу.

| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `candidates` | array | да | `[{ "name": "...", "description": "" }]` |
| `bc_top_k` | int \| null | нет | Сколько BC на кандидата (по умолчанию `FDM_BC_TOP_K`) |
| `tc_top_k` | int \| null | нет | Сколько TC внутри BC (по умолчанию `FDM_TC_PER_BC_TOP_K`) |

Коды: `200 { task_id }`; `400` — пустой список кандидатов или все имена пустые.

**`GET /api/landscape/analyze/{task_id}/progress`** (200 / 404)

```json
{ "phase": "analyzing", "current_tc": 1, "total_tc": 5, "current_tc_name": "…",
  "current_bc": 2, "total_bc": 10, "current_bc_code": "BC-1", "current_bc_name": "…",
  "elapsed_ms": 0, "done": false, "error": null }
```

Тики по BC монотонны (инкремент и отправка под блокировкой).

**`GET /api/landscape/analyze/{task_id}/result`** (200 / 400 / 404 / 425)

```json
{
  "results": [
    {
      "candidate_name": "…", "query": "…",
      "bcs": [
        { "code": "BC-1", "name": "…", "description": "…", "score": 0.8,
          "tcs": [ { "code": "TC-1", "name": "…", "score": 0.7,
                     "system_code": "SYS", "system_name": "…" } ] }
      ]
    }
  ],
  "elapsed_ms": 45000
}
```

**`POST /api/landscape/bc/search`** — синхронный type-ahead поиск BC (для BC-picker).

Тело: `{ "query": "...", "top_k": 10 }` (пустой query → `400`).
Ответ: `{ "results": [ { "code": "BC-1", "name": "…", "description": "…", "score": 0.87 } ] }`.
При недоступности каталога — `{ "results": [] }`.

### Publish — `/api/publish`

**`POST /api/publish/export`** — экспорт HLD-отчёта в Markdown.

Тело: `ExportRequest` — `title` (**да**), `source` (**да**), `source_url`,
`structured_requirements[]`, `impact_tcs[]`, `task_description`, `impact_level`,
`impact_level_label`.

Ответ (200): **файл** `text/markdown`, `Content-Disposition: attachment`,
имя `hld-report-<ascii_title>.md` (до 40 символов). Коды: `200` / `404`.

Элемент `impact_tcs[]` (`ImpactTCItem`): `code`, `name`, `description`,
`action` (`reuse` | `create_new`), `source`, `fr_ids`, `system` (`{code, name, endpoints[]}`),
`parent_bc` (`{code, name}`).

**`POST /api/publish/confluence`** — публикация отчёта в Confluence (Storage Format).

Тело: поля `ExportRequest` + `page_title`, `parent_page_url`, `pat`.

Ответ (200): `{ "confluence_url": "https://…/pages/987654" }`.
Логика: Markdown → Confluence Storage (XHTML) → поиск дочерней страницы с тем же заголовком →
создание или обновление с инкрементом версии.

Коды: `404` (нет PAT, не извлечь ID родительской страницы, нет base URL/space key),
`502` (ошибка Confluence API).

---

## Ключевые механизмы

**Хранилища фоновых задач.** Пять модульных `dict` — по одному на группу эндпоинтов.
Запись: `{ created_at, progress, result, error }`. Очистка — `cleanup_expired(store)`
перед стартом новой задачи: удаляются записи старше `TASK_TTL_SECONDS`. Результаты не
удаляются по завершении — живут до TTL.

**Ретраи и таймауты LLM** (`app/integrations/llm_client.py`):

- до `max(LLM_RETRY_COUNT, 6)` попыток; `429` — пауза `min(max(Retry-After, 10), 30)` с;
  прочие ошибки — `2**attempt` с;
- **пустой `content` при `status=ok`** — отдельный класс сбоя (до 2 доп. попыток), после
  исчерпания возвращается пустая строка, чтобы вызывающий код мог сработать по fallback;
- `max_tokens` клампится под контекст модели (262 194 токена), вход оценивается как
  `длина // 3`;
- reasoning выключен по умолчанию (`LLM_ENABLE_THINKING=False`), **кроме промптов из
  `LLM_THINKING_PROMPTS`** (по умолчанию дедупликация — `merge_dedup`, `merge_tc`); для них
  бюджет вывода берётся из `LLM_THINKING_MAX_TOKENS`, так как рассуждения делят лимит с ответом.
  Единая точка — `disable_thinking_params(prompt_name)`, используется и в health-пробе;
- тела запросов/ответов логируются с обрезкой до 2000 символов; логируются `finish_reason`
  и `reasoning_chars`.

**Чанкинг.**

- Текст требований: по пустым строкам, чанк ≤ 3000 символов, перекрытие 200 символов →
  LLM на каждый чанк (`structure_chunk`) → один merge-запрос (`merge_dedup`).
- FR для выявления TC: порог `TC_IDENTIFY_CHUNK_SIZE` (30). Выше порога — последовательные
  чанки (`identify_tc`) + merge (`merge_tc`). Merge идёт по «компактным» кандидатам
  (description ≤ 240, rationale ≤ 180 символов), после merge полные тексты восстанавливаются
  по совпадению `name`.

**Устойчивость парсинга LLM-JSON.** Три независимых парсера (`intake._parse_llm_result`,
`tc._parse_tc_result`, `bc._parse_bc_result`) ищут первую `[` и последнюю `]`, восстанавливают
обрезанный массив по последнему завершённому `}`, перебирают кандидатов от длинного к короткому
и нормализуют поля. Дополнительно: при пустом merge требования собираются из чанков
(`intake._collect_chunk_requirements`) либо возвращаются накопленные кандидаты (`tc`).

**Graceful degradation.** Обращения к BeeAtlas/fdm-search не бросают исключений наружу:
при таймауте или HTTP-ошибке возвращается `None` / пустой список. Пустой текст со страницы
Confluence → `ValueError` → `400` на `/api/intake/import`.

**Ограничения поисковых запросов.** Поисковый запрос к fdm-search обрезается до 200 символов
(длинные запросы дают пустую выдачу, длинный URL — `413` на шлюзе). `top_k` клампится:
`[1, 100]` для fdm-search, `[1, 20]` для landscape. Конкурентность запросов внутри кандидата —
семафор на 5.

---

## Внешние интеграции

| Система | Клиент | Что используется |
|---|---|---|
| **LLM** (OpenAI-совместимый шлюз) | `app/integrations/llm_client.py` | Единственная точка `POST {LLM_API_URL}/chat/completions`; вызывается из `core/intake.py`, `core/tc.py`, `core/bc.py`, `api/health.py` |
| **Confluence** (Data Center) | `app/integrations/confluence.py` | `GET /rest/api/content/{id}` (`expand=body.storage,title,space,version`), `GET …/child/page`, `GET /rest/api/content?title=…&spaceKey=…`, `POST /rest/api/content`, `PUT /rest/api/content/{id}` (инкремент версии). Плюс: разбор page_id из URL, рекурсивные `include`-макросы, Storage → текст, дедупликация объединённых страниц, генерация уникального заголовка |
| **BeeAtlas Gateway** | `app/integrations/beeatlas.py` | HMAC-SHA256-подпись (`X-Authorization: {key}:{hmac}`, заголовок `Nonce`); `search_capability_v2` → `GET /search/api/v1/search` (fdm-search); `get_systems` → `GET /api-gateway/product/v1/product/info` |

---

## LLM-промпты

Файлы в `app/prompts/*.txt`; читаются при вызове по относительному пути. В шапке каждого —
`# Version`, `# Prompt`, `# Description`, `# Strict mode`.

| Файл | Назначение | Где используется |
|---|---|---|
| `structure_chunk.txt` | Структурирование чанка текста в FR/NFR/OQ | Intake, шаг чанков |
| `merge_dedup.txt` | Объединение и дедупликация результатов чанков | Intake, финальный шаг |
| `identify_tc.txt` | Выявление Technical Capability из списка FR (TC-centric) | TC identify |
| `merge_tc.txt` | Дедупликация TC-кандидатов из разных чанков | TC identify (чанкованный режим) |
| `summarize_task.txt` | Краткое описание задачи из текста требований | `POST /api/tc/describe-task` |
| `prompt_top10_with_catalog.txt` | Выявление BC; **полный каталог возможностей вшит в файл** (~338 КБ) | `POST /api/bc/identify/start` |

---

## Тесты

`pytest`, 68 тестов; фикстура `client` (`httpx.AsyncClient` + `ASGITransport`) объявлена
в каждом файле — общий `conftest.py` отсутствует.

| Файл | Что покрывает |
|---|---|
| `test_health.py` | Health-эндпоинт, выключение reasoning в пробе |
| `test_intake.py` | Импорт, структурирование, fallback-сборка из чанков |
| `test_tc.py` | Выявление TC, чанкинг, парсинг (обрезанный JSON, score, вложенные `fr_ids`), merge |
| `test_bc.py` | Выявление BC, парсинг кандидатов |
| `test_landscape.py` | Живой каталог: нормализация, дедупликация TC, монотонность прогресса, полный flow |
| `test_publish.py` | Экспорт Markdown, публикация/обновление страницы Confluence |
| `test_task_store.py` | TTL-очистка фоновых задач |
| `test_llm_client.py` | Отключение reasoning и исключение для дедупликации, бюджет reasoning-промптов, ретрай пустого ответа |

---

## Известные особенности

Полезно знать перед изменениями:

- **In-memory задачи процесс-локальны.** Несколько реплик пода → `progress`/`result` могут
  вернуть `404`. Перезапуск сервиса стирает задачи.
- **`Dockerfile`:** `EXPOSE 8000`, но фактический порт `8080` (`uvicorn --port 8080`).
  Шаг `COPY certs/*` требует каталога `certs/`, которого в репозитории нет — для дев-сборки
  используется `Dockerfile.dev` (блок сертификатов закомментирован).
- **Промпты читаются по относительному пути** — `uvicorn` запускать из корня репозитория.
- **`POST /api/publish/export`** создаёт временный файл (`delete=False`) и не удаляет его —
  файлы накапливаются в `tempfile.gettempdir()`.
- **Устаревшие/неиспользуемые поля:** `ImpactTCSystem.endpoints` объявлен, но в отчёт не
  попадает; `ConfluenceClient.space_key` — заглушка (space key приходит с фронтенда).
- **`app/models/`** зарезервирован, но пуст: все Pydantic-модели объявлены в `app/api/*.py`.
- **`/api/landscape/*`** — экспериментальный модуль (живой каталог); `/api/bc/*` и `/api/tc/*`
  работают параллельно и не изменялись.
- Файлы `logs-from-service-*.log` в корне — дампы логов подов, оставленные при отладке.

---

Развёрнутая документация (в т.ч. ADR, требования, бизнес-логика) лежит рядом, в каталоге
`../documentation/` — вне этого репозитория.
