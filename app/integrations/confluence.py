# Copyright (c) 2024 PJSC VimpelCom
"""
Confluence API клиент для импорта страниц.
Использует Confluence Storage Format (XHTML) с кастомным парсером
для корректного извлечения текста из таблиц, expand-блоков, макросов и т.д.
"""

import logging
import re
from html.parser import HTMLParser
from typing import Any, Optional
import httpx

from app.config import settings

logger = logging.getLogger("hld-agent")

# Регулярное выражение для поиска include-макросов с <ri:page> ссылками.
# Обрабатывает "include" и "excerpt-include" — оба содержат ссылку на другую страницу
# и должны быть рекурсивно разрешены.
# Извлекает: ri:space-key (может быть None для страниц того же space), ri:content-title
# Поддерживает:
#   - <ri:page ... /> (self-closing)
#   - <ri:page ...></ri:page> (open/close)
#   - <ri:page> внутри <ac:link> (включая когда <ri:page> полностью внутри <ac:link>)
#   - без ri:space-key (страница из того же space)
_INCLUDE_MACRO_NAMES = ("include", "excerpt-include")
_INCLUDE_REF_RE = re.compile(
    r'<ac:structured-macro[^>]*?ac:name="(?:' + '|'.join(_INCLUDE_MACRO_NAMES) + r')"[^>]*?>'
    r'.*?<ri:page\s+'
    r'(?:ri:space-key="([^"]*)")?\s*'
    r'ri:content-title="([^"]*)"'
    r'\s*/?>'
    r'(?:</ri:page>)?'
    r'.*?</ac:structured-macro>',
    re.DOTALL,
)

# Макросы без полезного текста (TOC, Jira, PlantUML, вложения и т.д.)
# NOTE: "include" макрос не входит в список — он содержит ссылку на другую страницу,
# и парсер извлекает её как текст "[Included page: SPACE > Title]"
_SKIP_MACROS = frozenset({
    "toc",
    "children",
    "children-display",
    "jira",
    "jiraissues",
    "plantuml",
    "code",
    "iframe",
    # "excerpt-include" НЕ входит — обрабатывается как include (загрузка целевой страницы)
    "view-file",
    "attachments",
    "gallery",
    "roadmap",
    "status",
    "recently-updated",
    "content-by-label",
    "detailssummary",
    "contrib",
    "livesearch",
    "navmap",
    "network",
    "blog-posts",
    "drawio",
    "gliffy",
    "pagetree",
    "spaces",
})

_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"})
_CELL_TAGS = frozenset({"td", "th"})


def _attrs_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {k: v for k, v in attrs if k and v is not None}


class _StorageHtmlToText(HTMLParser):
    """Извлекает текст из Confluence storage XHTML, включая таблицы и expand-блоки."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._macro_stack: list[str] = []
        self._skip_depth = 0
        self._attachment_skip = 0
        self._param_skip = 0
        self._rich_text_depth = 0
        self._in_title_param = False
        self._in_include_link = False  # True when inside include > parameter > link
        self._title_buffer: list[str] = []
        self._pending_expand_title: str | None = None

    def _content_active(self) -> bool:
        if self._skip_depth or self._attachment_skip or self._param_skip:
            return False
        if not self._macro_stack:
            return True
        if self._rich_text_depth > 0 or self._in_title_param or self._in_include_link:
            return True
        return False

    def _emit_block_break(self) -> None:
        self._parts.append("\n")

    def _flush_expand_title(self) -> None:
        if self._pending_expand_title:
            self._parts.append(f"\n[{self._pending_expand_title}]\n")
            self._pending_expand_title = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle self-closing XHTML tags like <ri:page .../>."""
        am = _attrs_map(attrs)
        if tag == "ri:page":
            space_key = am.get("ri:space-key", "")
            content_title = am.get("ri:content-title", "")
            if space_key and content_title:
                self._parts.append(f"[Included page: {space_key} > {content_title}]")
            elif content_title:
                self._parts.append(f"[Included page: {content_title}]")
            return
        # Default: delegate to starttag + endtag
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        am = _attrs_map(attrs)

        if tag == "ri:attachment":
            self._attachment_skip += 1
            return

        if tag == "ri:page":
            # Handled in handle_startendtag for self-closing form,
            # but also handle here for separate open/close tags (unlikely for ri:page)
            return

        if tag == "ac:structured-macro":
            name = am.get("ac:name", "").lower()
            self._macro_stack.append(name)
            if name in _SKIP_MACROS:
                self._skip_depth += 1
            return

        if tag == "ac:parameter":
            if self._skip_depth:
                self._param_skip += 1
                return
            if self._macro_stack:
                macro_name = self._macro_stack[-1]
                param_name = am.get("ac:name", "")
                if macro_name in _INCLUDE_MACRO_NAMES and param_name == "":
                    # include/excerpt-include macro's default parameter contains <ri:page> link
                    self._in_include_link = True
                    return
                if param_name == "title":
                    self._in_title_param = True
                    self._title_buffer = []
                else:
                    self._param_skip += 1
            return

        if tag == "ac:link":
            if self._in_include_link:
                return  # Don't skip content inside include > parameter > link
            if not self._content_active():
                return
            # Fall through to block break logic below if content active
            # (for links not inside include parameter)

        if tag == "ac:plain-text-body":
            return  # Skip plain-text body wrapper, text is in handle_data

        if tag == "ac:rich-text-body":
            if self._skip_depth:
                self._param_skip += 1
                return
            self._flush_expand_title()
            self._rich_text_depth += 1
            return

        if not self._content_active():
            return

        if tag in _BLOCK_TAGS or tag in _CELL_TAGS or tag == "tr":
            self._emit_block_break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "ri:attachment":
            if self._attachment_skip:
                self._attachment_skip -= 1
            return

        if tag == "ac:structured-macro":
            if self._macro_stack:
                name = self._macro_stack.pop()
                if name in _SKIP_MACROS and self._skip_depth:
                    self._skip_depth -= 1
            self._pending_expand_title = None
            return

        if tag == "ac:parameter":
            if self._param_skip:
                self._param_skip -= 1
                return
            if self._in_include_link:
                self._in_include_link = False
                return
            if self._in_title_param:
                title = "".join(self._title_buffer).strip()
                if title:
                    self._pending_expand_title = title
                self._in_title_param = False
                self._title_buffer = []
            return

        if tag == "ac:link":
            return

        if tag == "ac:plain-text-body":
            return

        if tag == "ac:rich-text-body":
            if self._param_skip:
                self._param_skip -= 1
                return
            if self._rich_text_depth:
                self._rich_text_depth -= 1
            return

        if not self._content_active():
            return

        if tag in _CELL_TAGS:
            self._parts.append(" | ")
        elif tag == "tr" or tag in _BLOCK_TAGS:
            self._emit_block_break()

    def handle_data(self, data: str) -> None:
        if self._in_title_param:
            self._title_buffer.append(data)
            return
        if not self._content_active():
            return
        if data.strip():
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r" *\| *\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _fallback_tag_strip(storage_html: str) -> str:
    """Простое извлечение текста: удаление всех HTML-тегов, нормализация пробелов.
    
    Используется как fallback, когда кастомный парсер даёт подозрительно короткий результат.
    """
    # Удаляем все HTML-теги
    text = re.sub(r"<[^>]+>", " ", storage_html)
    # Декодируем основные HTML-сущности
    text = text.replace("\u0026nbsp;", " ")
    text = text.replace("\u0026amp;", "\u0026")
    text = text.replace("\u0026lt;", "<")
    text = text.replace("\u0026gt;", ">")
    # quot entity: "
    text = text.replace("\u0026quot;", '\u0022')
    # Нормализуем пробелы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_FALLBACK_RATIO = 0.10  # Если кастомный парсер извлёк < 10% от длины HTML — используем fallback
_FALLBACK_MIN_LENGTH = 200  # Абсолютный минимум: если извлекли < 200 символов из большого HTML — тоже fallback


def storage_html_to_text(storage_html: str) -> str:
    """Конвертация Confluence Storage Format (XHTML) в plain text.
    
    Использует кастомный XHTML-парсер, понимающий Confluence-макросы.
    Если парсер извлёк подозрительно мало текста — автоматически применяет
    простой tag-strip fallback.
    """
    if not storage_html or not storage_html.strip():
        return ""
    
    # Кастомный парсер
    parser = _StorageHtmlToText()
    try:
        parser.feed(storage_html)
        parser.close()
    except Exception:
        logger.warning("Custom HTML parser failed, falling back to tag strip")
        return _fallback_tag_strip(storage_html)
    
    body_text = parser.get_text()
    html_len = len(storage_html)
    text_len = len(body_text)
    
    # Проверяем: не подозрительно ли короткий результат?
    # Условия для fallback:
    # 1. HTML > 500 символов И (текст < 200 символов ИЛИ текст < 10% от HTML)
    needs_fallback = (
        html_len > 500
        and text_len > 0
        and (text_len < _FALLBACK_MIN_LENGTH or (text_len / html_len) < _FALLBACK_RATIO)
    )
    
    if needs_fallback:
        logger.warning(
            "Custom parser extracted only %d chars from %d-char HTML (%.1f%%), "
            "falling back to simple tag strip. HTML preview: %s",
            text_len,
            html_len,
            text_len / html_len * 100,
            storage_html[:300].replace("\n", "\\n"),
        )
        fallback_text = _fallback_tag_strip(storage_html)
        logger.info(
            "Fallback tag strip result: %d chars from %d-char HTML (%.1f%%). Preview: %s",
            len(fallback_text),
            html_len,
            len(fallback_text) / html_len * 100 if html_len else 0,
            fallback_text[:200].replace("\n", "\\n"),
        )
        return fallback_text
    
    return body_text


def _extract_storage_body(page_json: dict[str, Any]) -> str:
    """Извлечение storage-тела страницы из JSON ответа Confluence API."""
    body = page_json.get("body") or {}
    storage = body.get("storage") or {}
    return str(storage.get("value") or "")


def _normalize_block(text: str) -> str:
    """Нормализация блока текста для дедупликации."""
    return re.sub(r"\s+", " ", text.strip().lower())


def merge_and_deduplicate(pages: list[dict[str, str]]) -> str:
    """Объединяет тексты страниц и удаляет дублирующиеся абзацы."""
    seen_blocks: set[str] = set()
    sections: list[str] = []

    for page in pages:
        body = page.get("body_text", "").strip()
        if not body:
            continue

        unique_blocks: list[str] = []
        for block in re.split(r"\n{2,}", body):
            chunk = block.strip()
            if not chunk:
                continue
            key = _normalize_block(chunk)
            if key in seen_blocks:
                continue
            seen_blocks.add(key)
            unique_blocks.append(chunk)

        if not unique_blocks:
            continue

        title = page.get("title", "").strip() or f"Страница {page.get('page_id', '?')}"
        sections.append(f"## {title}\n\n" + "\n\n".join(unique_blocks))

    return "\n\n---\n\n".join(sections)


def _try_extract_error_detail(response: httpx.Response) -> str:
    """Пытается извлечь детали ошибки из тела ответа Confluence API."""
    try:
        body = response.json()
        message = body.get("message") or body.get("errorDescription") or ""
        if message:
            return str(message)[:200]
    except Exception:
        pass
    try:
        text = response.text
        if text:
            return text.strip()[:200]
    except Exception:
        pass
    return ""


class ConfluenceClient:
    """Клиент для работы с Confluence API."""

    def __init__(self):
        self.timeout = settings.CONFLUENCE_TIMEOUT_SECONDS

    def _get_base_url(self, page_url: str) -> str:
        """Получение base_url из URL страницы."""
        match = re.match(r'(https?://[^/]+)', page_url)
        if match:
            return match.group(1)
        raise ValueError(f"Не удалось определить base URL из: {page_url}")

    def _get_headers(self, pat: Optional[str] = None) -> dict[str, str]:
        """Формирование заголовков для API-запросов к Confluence."""
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if pat:
            # Confluence Data Center / Server использует Bearer Token с PAT
            headers["Authorization"] = f"Bearer {pat}"
        return headers


    def _extract_page_id(self, url: str) -> Optional[str]:
        """Извлечение ID страницы из URL Confluence.
        
        Поддерживаемые форматы:
          - /display/SPACE/Page+Title
          - /spaces/SPACE/pages/PAGE_ID
          - /spaces/SPACE/pages/PAGE_ID/Title+Slug  (с титульным слегом)
          - /pages/viewpage.action?pageId=PAGE_ID&...  (action-формат)
          - pageId=PAGE_ID (прямой query-параметр)
        """
        # /display/SPACE/Page+Title
        match = re.search(r'/display/[^/]+/([^/?]+)', url)
        if match:
            return match.group(1)
        # /spaces/SPACE/pages/PAGE_ID (с опциональным слегом после ID)
        match = re.search(r'/pages/(\d+)(?:/|$)', url)
        if match:
            return match.group(1)
        # /pages/viewpage.action?pageId=PAGE_ID&... (action-формат)
        match = re.search(r'[?&]pageId=(\d+)', url)
        if match:
            return match.group(1)
        return None

    async def _fetch_page_json(self, page_id: str, api_base: str, pat: Optional[str] = None) -> dict[str, Any]:
        """GET /rest/api/content/{id} — тело страницы в Storage Format."""
        url = f"{api_base}/rest/api/content/{page_id}"
        logger.debug("Fetching Confluence page: GET %s (pat=%s)", url, "***" if pat else "None")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                response = await client.get(
                    url,
                    params={"expand": "body.storage,title,space,version"},
                    headers=self._get_headers(pat),
                )
                response.raise_for_status()
                data = response.json()
                # Debug: log available body keys and storage value presence
                body = data.get("body") or {}
                storage = body.get("storage") or {}
                storage_value = str(storage.get("value") or "")
                has_storage_value = bool(storage_value)
                logger.info(
                    "Confluence page response: keys=%s, title=%s, has_storage_value=%s, "
                    "body_keys=%s, storage_keys=%s, storage_value_len=%d, storage_value_preview=%s",
                    list(data.keys()),
                    data.get("title", "N/A"),
                    has_storage_value,
                    list(body.keys()),
                    list(storage.keys()),
                    len(storage_value),
                    storage_value[:200].replace("\n", "\\n") if storage_value else "(empty)",
                )
                if not has_storage_value:
                    logger.info("Confluence page body content (first 500 chars): %s", str(body)[:500])
                return data
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            detail = _try_extract_error_detail(e.response)
            if status == 401:
                raise ValueError(
                    f"Ошибка авторизации Confluence (401). "
                    f"Проверьте корректность PAT-токена."
                ) from e
            elif status == 403:
                raise ValueError(
                    f"Доступ к странице Confluence запрещён (403). "
                    f"У PAT-токена недостаточно прав."
                ) from e
            elif status == 404:
                raise ValueError(
                    f"Страница Confluence не найдена (404): page_id={page_id}. "
                    f"Проверьте URL страницы."
                ) from e
            else:
                raise ValueError(
                    f"Ошибка Confluence API (HTTP {status}): {detail}"
                ) from e
        except httpx.TimeoutException:
            raise ValueError(
                f"Тайм-аут соединения с Confluence ({self.timeout}с). "
                f"Проверьте доступность {api_base}"
            ) from None
        except httpx.ConnectError:
            raise ValueError(
                f"Не удалось подключиться к Confluence: {api_base}. "
                f"Проверьте сетевое соединение."
            ) from None

    async def _fetch_child_pages_json(self, page_id: str, api_base: str, pat: Optional[str] = None) -> list[dict[str, Any]]:
        """GET /rest/api/content/{id}/child/page — прямые подстраницы."""
        url = f"{api_base}/rest/api/content/{page_id}/child/page"
        logger.debug("Fetching Confluence child pages: GET %s", url)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                response = await client.get(
                    url,
                    params={"limit": 100, "expand": "space"},
                    headers=self._get_headers(pat),
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results") or []
                return [item for item in results if item.get("type") == "page"]
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            detail = _try_extract_error_detail(e.response)
            raise ValueError(
                f"Ошибка загрузки дочерних страниц Confluence (HTTP {status}): {detail}"
            ) from e

    async def _find_page_by_title(
        self, space_key: str, title: str, api_base: str, pat: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Поиск ID страницы Confluence по title и space key.
        
        GET /rest/api/content?title=TITLE&spaceKey=SPACE
        """
        url = f"{api_base}/rest/api/content"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                response = await client.get(
                    url,
                    params={
                        "title": title,
                        "spaceKey": space_key,
                        "expand": "body.storage,title,space,version",
                        "limit": 1,
                    },
                    headers=self._get_headers(pat),
                )
                if response.status_code != 200:
                    logger.warning("Failed to find page by title: space=%s title=%s status=%d", space_key, title, response.status_code)
                    return None
                data = response.json()
                results = data.get("results") or []
                if not results:
                    logger.warning("Page not found by title: space=%s title=%s", space_key, title)
                    return None
                return results[0]
        except Exception as e:
            logger.warning("Error finding page by title '%s' in space '%s': %s", title, space_key, str(e))
            return None

    async def _resolve_page_with_includes(
        self,
        page_id: str,
        api_base: str,
        pat: Optional[str] = None,
        max_depth: int = 5,
        visited: Optional[set[str]] = None,
        _depth: int = 0,
    ) -> Optional[dict[str, str]]:
        """Загрузка страницы с рекурсивным разрешением include-макросов.
        
        Включает содержимое страниц, на которые ссылаются <ac:structured-macro ac:name="include">.
        Защита от циклов через visited set.
        """
        if visited is None:
            visited = set()
        
        if _depth >= max_depth:
            logger.warning("Max include depth (%d) reached for page %s", max_depth, page_id)
            return None
        
        if page_id in visited:
            logger.warning("Circular include detected for page %s", page_id)
            return None
        
        visited.add(page_id)
        
        try:
            page_json = await self._fetch_page_json(page_id, api_base, pat)
        except Exception as e:
            logger.warning("Failed to fetch included page %s: %s", page_id, str(e))
            return None
        
        title = str(page_json.get("title") or "").strip() or f"Страница {page_id}"
        storage_html = _extract_storage_body(page_json)
        
        if not storage_html.strip():
            return {
                "page_id": page_id,
                "title": title,
                "body_text": "",
            }
        
        # Определяем space key родительской страницы (для include без ri:space-key)
        parent_space = page_json.get("space") or {}
        parent_space_key = str(parent_space.get("key") or "").strip()
        
        # Находим все include-макросы с <ri:page> ссылками
        included_bodies: list[str] = []
        resolved_count = 0
        
        for match in _INCLUDE_REF_RE.finditer(storage_html):
            inc_space_key = match.group(1).strip() if match.group(1) else ""
            inc_content_title = match.group(2).strip()
            
            # Если ri:space-key не указан — используем space родительской страницы
            if not inc_space_key and parent_space_key:
                inc_space_key = parent_space_key
                logger.info(
                    "Include without space-key, using parent space '%s' for title '%s' (depth=%d)",
                    inc_space_key, inc_content_title, _depth,
                )
            
            if not inc_space_key or not inc_content_title:
                logger.warning(
                    "Skipping include with missing space-key or title: space=%s title=%s (depth=%d)",
                    inc_space_key or "(missing)", inc_content_title or "(missing)", _depth,
                )
                continue
            
            logger.info(
                "Resolving include: space=%s title=%s (depth=%d)",
                inc_space_key, inc_content_title, _depth,
            )
            
            # Ищем страницу по title + space key
            found_page = await self._find_page_by_title(inc_space_key, inc_content_title, api_base, pat)
            if not found_page:
                included_bodies.append(f"\n[Included page NOT FOUND: {inc_space_key} > {inc_content_title}]\n")
                continue
            
            inc_page_id = str(found_page.get("id", ""))
            if not inc_page_id:
                included_bodies.append(f"\n[Included page NOT FOUND: {inc_space_key} > {inc_content_title}]\n")
                continue
            
            # Рекурсивно разрешаем include в найденной странице
            resolved = await self._resolve_page_with_includes(
                inc_page_id, api_base, pat,
                max_depth=max_depth, visited=visited, _depth=_depth + 1,
            )
            
            if resolved and resolved.get("body_text", "").strip():
                included_bodies.append(
                    f"\n## [Include: {inc_space_key} > {inc_content_title}]\n\n{resolved['body_text']}"
                )
                resolved_count += 1
            else:
                included_bodies.append(f"\n[Included page EMPTY: {inc_space_key} > {inc_content_title}]\n")
        
        if resolved_count > 0:
            logger.info("Resolved %d include(s) for page %s (depth=%d)", resolved_count, page_id, _depth)
        
        # Парсим основное содержимое страницы (без include-макросов, которые уже обработаны)
        # Удаляем include-макросы из storage_html, чтобы парсер их не дублировал
        clean_storage = _INCLUDE_REF_RE.sub("", storage_html)
        body_text = storage_html_to_text(clean_storage)
        
        # DIAGNOSTIC: Always log storage HTML details
        logger.info(
            "Page body extraction (resolve): page_id=%s, title=%s, "
            "storage_html_len=%d, clean_storage_len=%d, body_text_len=%d, "
            "storage_html_preview=%s",
            page_id,
            title,
            len(storage_html),
            len(clean_storage),
            len(body_text),
            storage_html[:300].replace("\n", "\\n") if storage_html else "(empty)",
        )
        
        # Always log a warning if the extracted text is suspiciously short
        # Use a more aggressive threshold: if body_text < 5% of HTML OR body_text < 200 chars
        _body_short = (
            len(storage_html) > 500
            and (len(body_text) < 200 or len(body_text) < len(storage_html) * 0.05)
        )
        if _body_short:
            logger.warning(
                "SUSPICIOUS PARSE: page_id=%s, storage_html=%d chars, "
                "body_text only %d chars (%.1f%% of HTML). "
                "Dumping storage HTML for debugging:\n"
                "--- STORAGE HTML START ---\n%s\n--- STORAGE HTML END ---",
                page_id,
                len(storage_html),
                len(body_text),
                len(body_text) / len(storage_html) * 100 if storage_html else 0,
                storage_html[:5000],
            )
        
        # Если страница сама не содержит текста, но у неё есть include — используем только их
        if not body_text.strip() and included_bodies:
            body_text = "\n\n".join(included_bodies)
        elif included_bodies:
            body_text = body_text + "\n\n" + "\n\n".join(included_bodies)
        
        return {
            "page_id": page_id,
            "title": title,
            "body_text": body_text.strip(),
        }

    async def create_or_update_page(
        self,
        title: str,
        body: str,
        parent_page_id: Optional[str] = None,
        pat: Optional[str] = None,
        confluence_base_url: Optional[str] = None,
        space_key: Optional[str] = None,
    ) -> str:
        """
        Создание или обновление страницы в Confluence.
        - confluence_base_url: базовый URL Confluence
          Если не указан, используется CONFLUENCE_URL из настроек.
        - space_key: ключ space (например, BRITEA). Если не указан, извлекается из CONFLUENCE_URL.
        Возвращает ID созданной/обновлённой страницы.
        """
        if not pat:
            raise ValueError("PAT не указан для публикации в Confluence")

        # Определяем api_base: из параметра
        api_base = confluence_base_url
        if not api_base:
            raise ValueError(
                "confluence_base_url не указан. Передайте URL Confluence."
            )

        # Определяем space key: из параметра
        effective_space_key = space_key
        if not effective_space_key:
            raise ValueError(
                "space_key не указан. Передайте ключ space."
            )




        headers = self._get_headers(pat)
        headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            existing_page_id: Optional[str] = None
            existing_version: Optional[int] = None

            # Шаг 1: если задан родитель — ищем дочернюю страницу с ТОЧНО таким заголовком.
            # ВАЖНО: GET /rest/api/content/{parent}/child/page НЕ поддерживает фильтр title
            # (параметр игнорируется — всегда возвращается первая дочерняя страница),
            # поэтому перебираем дочерние страницы постранично и сравниваем заголовки.
            if parent_page_id:
                page_size = 100
                start = 0
                while True:
                    child_response = await client.get(
                        f"{api_base}/rest/api/content/{parent_page_id}/child/page",
                        params={
                            "limit": page_size,
                            "start": start,
                            "expand": "version",
                        },
                        headers=self._get_headers(pat),
                    )
                    if child_response.status_code != 200:
                        break
                    child_data = child_response.json()
                    child_results = child_data.get("results") or []
                    for child in child_results:
                        if (child.get("title") or "") == title:
                            existing_page_id = child.get("id")
                            version = child.get("version") or {}
                            existing_version = version.get("number", 0)
                            break
                    if existing_page_id or len(child_results) < page_size:
                        break
                    start += page_size

            # Шаг 2: если среди дочерних совпадения нет — создаём новую страницу.
            # Confluence не даёт создать страницу с названием, которое уже есть в space
            # (400 "A page with this title already exists"), поэтому генерируем уникальное
            # название (см. _find_unique_title).

            if existing_page_id and existing_version:
                # Обновляем существующую страницу
                update_data = {
                    "id": existing_page_id,
                    "type": "page",
                    "title": title,
                    "space": {"key": effective_space_key},

                    "body": {
                        "storage": {
                            "value": body,
                            "representation": "storage",
                        }
                    },
                    "version": {
                        "number": existing_version + 1,
                        "message": "Updated by HLD Agent",
                    },
                }

                logger.debug(
                    "Updating Confluence page: id=%s, title=%s, body_len=%d, body_preview=%s",
                    existing_page_id, title, len(body), body[:500].replace("\n", "\\n"),
                )

                response = await client.put(
                    f"{api_base}/rest/api/content/{existing_page_id}",
                    json=update_data,
                    headers=headers,
                )
                if response.status_code >= 400:
                    logger.error(
                        "Confluence update failed: status=%d, body=%s",
                        response.status_code,
                        response.text[:1000] if response.text else "(empty)",
                    )
                response.raise_for_status()
                logger.info("Updated existing Confluence page: %s (v%d)", existing_page_id, existing_version + 1)
                return str(existing_page_id)
            else:
                # Создаём новую страницу с уникальным названием
                create_title = await self._find_unique_title(
                    client, api_base, effective_space_key, title, pat
                )

                create_data: dict[str, Any] = {
                    "type": "page",
                    "title": create_title,
                    "space": {"key": effective_space_key},

                    "body": {
                        "storage": {
                            "value": body,
                            "representation": "storage",
                        }
                    },
                }

                if parent_page_id:
                    create_data["ancestors"] = [{"id": parent_page_id}]

                response = await client.post(
                    f"{api_base}/rest/api/content",
                    json=create_data,
                    headers=headers,
                )
                if response.status_code >= 400:
                    logger.error(
                        "Confluence create failed: status=%d, parent=%s, space=%s, title=%s, body_len=%d, "
                        "response=%s",
                        response.status_code,
                        parent_page_id,
                        effective_space_key,
                        create_title,
                        len(body),
                        response.text[:2000] if response.text else "(empty)",
                    )
                response.raise_for_status()
                new_page_id = response.json().get("id", "")
                logger.info("Created new Confluence page: %s (title=%s)", new_page_id, create_title)
                return str(new_page_id)

    async def _find_unique_title(
        self,
        client: httpx.AsyncClient,
        api_base: str,
        space_key: str,
        base_title: str,
        pat: str,
    ) -> str:
        """
        Возвращает название страницы, которого ещё нет в space.
        Последовательность: <base_title> → '<base_title> Impact' → '<base_title> Impact N'.
        Нужно, т.к. Confluence не даёт создать страницу с названием, уже существующим
        в space (400 "A page with this title already exists").
        """
        n = 0
        while True:
            if n == 0:
                candidate = base_title
            elif n == 1:
                candidate = f"{base_title} Impact"
            else:
                candidate = f"{base_title} Impact {n}"

            search_response = await client.get(
                f"{api_base}/rest/api/content",
                params={"title": candidate, "spaceKey": space_key, "limit": 1},
                headers=self._get_headers(pat),
            )
            exists = False
            if search_response.status_code == 200:
                exists = len(search_response.json().get("results") or []) > 0
            else:
                # При ошибке поиска консервативно считаем название занятым и пробуем следующее
                logger.warning(
                    "Confluence title uniqueness check failed: status=%d, title=%s",
                    search_response.status_code,
                    candidate,
                )
                exists = True

            if not exists:
                logger.info(
                    "Confluence unique title for create: base=%s -> %s",
                    base_title,
                    candidate,
                )
                return candidate
            n += 1

    @property
    def space_key(self) -> str:
        """Извлечение space key из CONFLUENCE_URL (больше не используется, всё передаётся с фронтенда)."""
        return ""

    async def get_page_title(self, page_url: str, pat: Optional[str] = None) -> str:
        """Получение заголовка страницы Confluence."""
        page_id = self._extract_page_id(page_url)
        if not page_id:
            raise ValueError(f"Не удалось извлечь ID страницы из URL: {page_url}")

        api_base = self._get_base_url(page_url)

        page_json = await self._fetch_page_json(page_id, api_base, pat)
        return str(page_json.get("title") or "")

    async def get_page_content(self, page_url: str, include_children: bool = False, pat: Optional[str] = None) -> str:
        """
        Получение текстового содержимого страницы Confluence.
        Использует Confluence Storage Format с кастомным XHTML-парсером.
        При include_children=True загружает и объединяет дочерние страницы с дедупликацией.
        Автоматически разрешает include-макросы (рекурсивно загружает встроенные страницы).
        """
        page_id = self._extract_page_id(page_url)
        if not page_id:
            raise ValueError(f"Не удалось извлечь ID страницы из URL: {page_url}")

        api_base = self._get_base_url(page_url)

        # Загружаем основную страницу с рекурсивным разрешением include-макросов
        main_page = await self._resolve_page_with_includes(page_id, api_base, pat)

        pages: list[dict[str, str]] = []
        if main_page and main_page.get("body_text", "").strip():
            pages.append(main_page)

        # Опционально: дочерние страницы (тоже с разрешением include)
        if include_children:
            try:
                raw_children = await self._fetch_child_pages_json(page_id, api_base, pat)
                for child in raw_children:
                    child_id = str(child.get("id") or "").strip()
                    if not child_id:
                        continue
                    try:
                        child_page = await self._resolve_page_with_includes(
                            child_id, api_base, pat,
                            visited={page_id},  # предотвращаем зацикливание на родительской
                            _depth=0,
                        )
                        if child_page and child_page.get("body_text", "").strip():
                            pages.append(child_page)
                    except Exception as e:
                        logger.warning("Failed to load child page %s: %s", child_id, str(e))
            except Exception as e:
                logger.warning("Failed to fetch child pages: %s", str(e))

        # Объединяем и дедуплицируем
        if not pages:
            raise ValueError("Не удалось извлечь текст со страницы Confluence")

        merged = merge_and_deduplicate(pages)
        if not merged.strip():
            raise ValueError("После объединения страниц не осталось текста")

        logger.info(
            "Confluence merge complete: page_id=%s, title=%s, pages=%d, merged_len=%d, merged_preview=%s",
            page_id,
            pages[0].get("title", "?"),
            len(pages),
            len(merged),
            merged[:200].replace("\n", "\\n") if merged else "(empty)",
        )

        return merged


# Singleton
confluence_client = ConfluenceClient()

