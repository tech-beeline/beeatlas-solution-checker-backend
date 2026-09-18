# Copyright (c) 2024 PJSC VimpelCom
"""
BeeAtlas API клиент для поиска систем FDM и E2E по TC и CJ.

Использует HMAC-SHA256 аутентификацию для всех запросов.
Формат подписи соответствует beeatlas_gateway_sdk HMACAuth.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger("hld-agent")


def _md5(data: str) -> str:
    """Вычислить MD5 хеш строки."""
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _build_hmac_headers(
    method: str,
    path: str,
    body: Optional[str] = None,
) -> dict[str, str]:
    """
    Формирование HMAC-SHA256 подписи для запроса к BeeAtlas API.

    Формат (соответствует beeatlas_gateway_sdk HMACAuth):
    - Nonce: <secrets.token_urlsafe(32)> — 32 байта, base64url
    - X-Authorization: <api_key>:<base64_signature>

    Подпись считается по строке:
        method + "\\n" + path + "\\n" + md5(body) + "\\n" + content-type + "\\n" + nonce + "\\n"
    """
    nonce = secrets.token_urlsafe(32)
    content_type = "application/json"

    body_str = body if body else ""
    md5_body = _md5(body_str)

    message = f"{method}\n{path}\n{md5_body}\n{content_type}\n{nonce}\n"

    signature = hmac.new(
        settings.BEEATLAS_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    signature_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "X-Authorization": f"{settings.BEEATLAS_API_KEY}:{signature_b64}",
        "Nonce": nonce,
        "Content-Type": content_type,
    }


async def _beeatlas_request(
    method: str,
    path: str,
    body: Optional[dict] = None,
) -> Optional[dict]:
    """
    Выполнение запроса к BeeAtlas API с HMAC-аутентификацией.
    Возвращает None при недоступности API (graceful degradation).
    """
    if not settings.BEEATLAS_API_URL:
        logger.warning("BeeAtlas API URL not configured, skipping request")
        return None

    url = f"{settings.BEEATLAS_API_URL.rstrip('/')}{path}"
    body_str = json.dumps(body) if body else None
    headers = _build_hmac_headers(method, path, body_str)

    logger.info(
        "BeeAtlas request: %s %s | nonce=%s",
        method,
        path,
        headers.get("Nonce", ""),
    )

    try:
        async with httpx.AsyncClient(
            timeout=settings.BEEATLAS_TIMEOUT_SECONDS,
            verify=False,
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            logger.info(
                "BeeAtlas response: %s %s | status=%d",
                method,
                path,
                response.status_code,
            )
            return data

    except httpx.TimeoutException:
        logger.warning("BeeAtlas API timeout: %s %s", method, path)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            "BeeAtlas API error: %s %s | status=%d | body=%s",
            method,
            path,
            e.response.status_code,
            e.response.text[:500],
        )
        return None
    except Exception as e:
        logger.warning("BeeAtlas API request failed: %s %s | %s", method, path, str(e))
        return None


async def _beeatlas_request_with_path(
    method: str,
    path_for_hmac: str,
    full_path: str,
    body: Optional[dict] = None,
) -> Optional[dict | list]:
    """
    Выполнение запроса к BeeAtlas API с HMAC-аутентификацией,
    где path для HMAC и URL path могут отличаться (например, из-за query-параметров).

    Query-параметры НЕ участвуют в HMAC-подписи.
    """
    if not settings.BEEATLAS_API_URL:
        logger.warning("BeeAtlas API URL not configured, skipping request")
        return None

    url = f"{settings.BEEATLAS_API_URL.rstrip('/')}{full_path}"
    body_str = json.dumps(body) if body else None
    headers = _build_hmac_headers(method, path_for_hmac, body_str)

    logger.info(
        "BeeAtlas request: %s %s | nonce=%s",
        method,
        full_path,
        headers.get("Nonce", ""),
    )

    try:
        async with httpx.AsyncClient(
            timeout=settings.BEEATLAS_TIMEOUT_SECONDS,
            verify=False,
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            logger.info(
                "BeeAtlas response: %s %s | status=%d",
                method,
                full_path,
                response.status_code,
            )
            return data

    except httpx.TimeoutException:
        logger.warning("BeeAtlas API timeout: %s %s", method, full_path)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            "BeeAtlas API error: %s %s | status=%d | body=%s",
            method,
            full_path,
            e.response.status_code,
            e.response.text[:500],
        )
        return None
    except Exception as e:
        logger.warning(
            "BeeAtlas API request failed: %s %s | %s",
            method,
            full_path,
            str(e),
        )
        return None


async def search_capability_v2(
    query: str = "",
    top_k: int = 10,
    code: Optional[str] = None,
    exclude_systems: Optional[str] = None,
    parents: Optional[str] = None,
    llm_rerank: bool = False,
    entity_type: str = "tc"
) -> list[dict]:
    """
    Поиск technical capability через fdm-search сервис.

    fdm-search опубликован за BeeAtlas Gateway (HMAC-аутентификация):
        GET {BEEATLAS_API_URL}/search/api/v1/search?query=<query>&limit=<top_k>&...

    Параметры:
        query — текстовый запрос для семантического поиска (по смыслу).
        top_k — максимум результатов (limit, 1–100, по умолч. 10).
        code — точное совпадение по полю code TC (опционально).
        exclude_systems — исключить системы: alias или name, через запятую (опционально).
        parent — фильтр по доменам/BC: code через запятую, OR (опционально).
        llm_rerank — использовать LLM для лучшего поиска по смыслу (только для query, по умолч. False).

    Возвращает список TC, нормализованный к формату:
        [{code, name, description, score, internal_id, synonyms, actions,
          system_id, system_name, system_alias, domain_codes}]

    При недоступности API возвращает пустой список (graceful degradation).
    """
    path_for_hmac = "/search/api/v1/search"

    if not (query and query.strip()) and not code:
        logger.warning("Empty search query and no code provided, skipping fdm-search")
        return []

    import urllib.parse

    params: list[str] = []
    if query and query.strip():
        params.append(f"query={urllib.parse.quote(query.strip(), safe='')}")
    if code:
        params.append(f"code={urllib.parse.quote(code.strip(), safe='')}")
    if exclude_systems:
        params.append(f"exclude_systems={urllib.parse.quote(exclude_systems.strip(), safe='')}")
    if parents:
        params.append(f"parent={urllib.parse.quote(parents.strip(), safe='')}")

    params.append(f"entity_type={urllib.parse.quote(entity_type.strip(), safe='')}")
    
    limit = max(1, min(100, top_k))
    params.append(f"limit={limit}")
    if llm_rerank:
        params.append("llm_rerank=true")

    query_string = "&".join(params)
    full_path = f"{path_for_hmac}?{query_string}"

    logger.info(
        "FDM search request: GET %s",
        full_path,
    )

    data = await _beeatlas_request_with_path("GET", path_for_hmac, full_path)

    if data is None:
        return []

    if not isinstance(data, dict):
        logger.warning(
            "Unexpected FDM search response format (expected dict): %s",
            str(data)[:200],
        )
        return []

    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        logger.warning(
            "FDM search response missing 'results' array: %s",
            str(data)[:200],
        )
        return []

    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        code_val = payload.get("code", "")
        if not code_val:
            continue
        results.append({
            "code": code_val,
            "name": payload.get("name", ""),
            "description": payload.get("description", ""),
            "score": float(item.get("score") or 0),
            # Дополнительные поля из payload для downstream потребителей
            "internal_id": payload.get("internal_id"),
            "synonyms": payload.get("synonyms", []),
            "actions": payload.get("actions", []),
            "system_id": payload.get("system_id"),
            "system_name": payload.get("system_name"),
            "system_alias": payload.get("system_alias"),
            "domain_codes": payload.get("domain_codes", []),
        })

    # Сортируем по score (убывание)
    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:limit]

    logger.info(
        "FDM search: found %d results for query='%s' code=%s (limit=%d)",
        len(results),
        (query or "")[:100],
        code,
        limit,
    )

    return results


async def get_systems(query: str = "") -> list[dict]:
    """
    Получение списка систем (продуктов) ландшафта для назначения создаваемым TC.

        GET /api-gateway/product/v1/product/info

    Возвращает список систем, нормализованный к формату:
        [{code, name, description}]
    code — alias продукта (системы).

    query — фильтр по названию или коду (case-insensitive). Пустая строка — без фильтра.
    При недоступности API возвращает пустой список (graceful degradation).
    """
    data = await _beeatlas_request("GET", "/api-gateway/product/v1/product/info")

    if data is None:
        return []

    if not isinstance(data, list):
        logger.warning(
            "Unexpected BeeAtlas response format for product/info: %s",
            str(data)[:200],
        )
        return []

    systems: list[dict] = []
    q = query.strip().lower()

    for item in data:
        if not isinstance(item, dict):
            continue
        code = item.get("alias", "") or item.get("code", "")
        name = item.get("name", "")
        if not code and not name:
            continue
        if q and q not in name.lower() and q not in code.lower():
            continue
        systems.append({
            "code": code,
            "name": name,
            "description": item.get("description", "") or "",
        })

    systems.sort(key=lambda s: (s["name"].lower(), s["code"].lower()))
    logger.info(
        "BeeAtlas product/info: found %d systems (query='%s')",
        len(systems),
        query,
    )

    return systems
