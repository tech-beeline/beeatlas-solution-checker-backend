# Copyright (c) 2024 PJSC VimpelCom
"""
Бизнес-логика этапа Publish — генерация Markdown и публикация в Confluence.

Все функции stateless — не хранят состояние между вызовами.
"""

import logging
import re
from typing import Optional

from app.integrations.confluence import confluence_client

logger = logging.getLogger("hld-agent")


def generate_markdown(
    title: str,
    source: str,
    source_url: Optional[str] = None,
    structured_requirements: Optional[list[dict]] = None,
    impact_tcs: Optional[list[dict]] = None,
    task_description: Optional[str] = None,
    impact_level: Optional[str] = None,
    impact_level_label: Optional[str] = None,
) -> str:
    """
    Генерация HLD-отчёта в формате Markdown.

    Структура:
    1. Header — название задачи, источник, дата, дисклеймер
    2. Summary — сводная таблица (FR, NFR, OQ, reuse TC, systems, new TC)
    3. Detailed lists:
       - FR (code, requirement) — маркированный список
       - NFR (code, requirement) — маркированный список
       - OQ (number, requirement) — маркированный список
       - TC (code, new/reused, name, description, system, list of FRs) — таблица
    4. Impact Estimation — оценка уровня влияния по методологии
    5. Capabilities — описание бизнес/технических возможностей решения

    Все данные передаются явно, без обращения к сессии.
    impact_tcs — единый источник данных по TC (содержит code, name, description,
    action, fr_ids, system).
    """
    from datetime import datetime

    lines: list[str] = []

    # ============================================================
    # 1. HEADER
    # ============================================================
    lines.append(f"# HLD Report: {title}")
    lines.append("")
    lines.append(f"- **Источник**: {source}")
    if source_url:
        lines.append(f"- **URL**: {source_url}")
    lines.append(f"- **Дата создания**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("> ⚠️ **Дисклеймер**: Данный отчёт сгенерирован автоматически с использованием AI-агента (HLD Agent). "
                 "Результаты требуют проверки и утверждения ответственными лицами.")
    lines.append("")

    # Task Description
    if task_description:
        lines.append("## 1. Описание задачи")
        lines.append("")
        # Экранируем HTML-спецсимволы, чтобы не сломать Confluence Storage Format
        lines.append(_escape_html(task_description))
        lines.append("")

    lines.append("---")
    lines.append("")

    # ============================================================
    # 2. SUMMARY
    # ============================================================
    lines.append("## 2. Сводка (Summary)")
    lines.append("")

    # Подсчёты
    frs = [r for r in (structured_requirements or []) if r.get("type") == "FR"]
    nfrs = [r for r in (structured_requirements or []) if r.get("type") == "NFR"]
    oqs = [r for r in (structured_requirements or []) if r.get("type") == "OQ"]

    # Подсчёты TC из impact_tcs
    reuse_tc_count = sum(1 for tc in (impact_tcs or []) if tc.get("action") == "reuse")
    new_tc_count = sum(1 for tc in (impact_tcs or []) if tc.get("action") == "create_new")

    # Уникальные системы из impact_tcs
    unique_systems = set()
    for tc in (impact_tcs or []):
        sys = tc.get("system")
        if sys and isinstance(sys, dict):
            code = sys.get("code", "")
            if code:
                unique_systems.add(code)
    total_systems = len(unique_systems)

    lines.append("| Метрика | Значение |")
    lines.append("|---------|----------|")
    lines.append(f"| Всего функциональных требований (FR) | {len(frs)} |")
    lines.append(f"| Всего нефункциональных требований (NFR) | {len(nfrs)} |")
    lines.append(f"| Всего открытых вопросов (OQ) | {len(oqs)} |")
    lines.append(f"| Переиспользуемых Technical Capability (Reuse) | {reuse_tc_count} |")
    lines.append(f"| Задействовано систем ландшафта | {total_systems} |")
    lines.append(f"| Новых Technical Capability (Create New) | {new_tc_count} |")
    lines.append("")

    lines.append("---")
    lines.append("")

    # ============================================================
    # 3. DETAILED LISTS
    # ============================================================
    lines.append("## 3. Детальные списки (Detailed Lists)")
    lines.append("")

    # 3.1 Functional Requirements — маркированный список
    lines.append("### 3.1 Функциональные требования (FR)")
    lines.append("")
    if frs:
        for r in frs:
            rid = r.get("id", "")
            title_r = r.get("title", "")
            desc = r.get("description", "").replace("\n", " ")
            lines.append(f"- **{rid}** — **{title_r}** — {desc}")
        lines.append("")
    else:
        lines.append("*Нет функциональных требований.*")
        lines.append("")

    # 3.2 Non-Functional Requirements — маркированный список
    lines.append("### 3.2 Нефункциональные требования (NFR)")
    lines.append("")
    if nfrs:
        for r in nfrs:
            rid = r.get("id", "")
            title_r = r.get("title", "")
            desc = r.get("description", "").replace("\n", " ")
            lines.append(f"- **{rid}** — **{title_r}** — {desc}")
        lines.append("")
    else:
        lines.append("*Нет нефункциональных требований.*")
        lines.append("")

    # 3.3 Open Questions — маркированный список
    lines.append("### 3.3 Открытые вопросы (OQ)")
    lines.append("")
    if oqs:
        for i, r in enumerate(oqs, 1):
            title_r = r.get("title", "")
            desc = r.get("description", "").replace("\n", " ")
            lines.append(f"- **OQ-{i}** — **{title_r}** — {desc}")
        lines.append("")
    else:
        lines.append("*Нет открытых вопросов.*")
        lines.append("")

    # 3.4 Technical Capability — таблица с системой
    lines.append("### 3.4 Technical Capability (TC)")
    lines.append("")

    if impact_tcs:
        lines.append("| Код | Тип | Название | Родительская BC | Описание | Система | Применяемые FR |")
        lines.append("|-----|-----|----------|----------------|----------|--------|----------------|")
        for tc_entry in impact_tcs:
            code = tc_entry.get("code", "")
            name = tc_entry.get("name", "")
            action = tc_entry.get("action", "reuse")
            action_label = "♻️ Reuse" if action == "reuse" else "✨ New"
            desc = tc_entry.get("description", "").replace("\n", " ")
            fr_list = ", ".join(tc_entry.get("fr_ids", [])) if tc_entry.get("fr_ids") else "—"

            # Родительская BC (эксперимент feature/exp-bc-search)
            parent_label = "—"
            parent = tc_entry.get("parent_bc")
            if parent and isinstance(parent, dict):
                parent_code = parent.get("code", "")
                parent_name = parent.get("name", "")
                if parent_code:
                    parent_label = f"{parent_code} — {parent_name}" if parent_name else parent_code

            # Система
            system_label = "—"
            sys = tc_entry.get("system")
            if sys and isinstance(sys, dict):
                sys_code = sys.get("code", "")
                sys_name = sys.get("name", "")
                if sys_code:
                    system_label = f"{sys_code} — {sys_name}"
                elif sys_name:
                    system_label = sys_name

            logger.info(
                "  generate_markdown TC: code=%s name=%s action=%s parent_bc=%s system_label=%s",
                code, name, action, parent_label, system_label,
            )

            lines.append(f"| {code} | {action_label} | {name} | {parent_label} | {desc} | {system_label} | {fr_list} |")
        lines.append("")
    else:
        lines.append("*Нет Technical Capability.*")
        lines.append("")

    # ============================================================
    # 3.5 SYSTEMS
    # ============================================================
    lines.append("### 3.5 Затронутые системы (Systems)")
    lines.append("")

    # Группируем TC по системам
    system_tc_map: dict[str, dict] = {}
    for tc_entry in (impact_tcs or []):
        sys = tc_entry.get("system")
        if sys and isinstance(sys, dict):
            sys_code = sys.get("code", "")
            sys_name = sys.get("name", "")
            if sys_code:
                if sys_code not in system_tc_map:
                    system_tc_map[sys_code] = {"name": sys_name, "tcs": []}
                tc_code = tc_entry.get("code", "")
                tc_name = tc_entry.get("name", "")
                tc_action = "♻️ Reuse" if tc_entry.get("action") == "reuse" else "✨ New"
                system_tc_map[sys_code]["tcs"].append(f"{tc_code} — {tc_name} ({tc_action})")

    if system_tc_map:
        lines.append("| Код системы | Название системы | Применяемые TC |")
        lines.append("|-------------|------------------|----------------|")
        for sys_code in sorted(system_tc_map.keys()):
            sys_data = system_tc_map[sys_code]
            tc_list = "; ".join(sys_data["tcs"])
            lines.append(f"| {sys_code} | {sys_data['name']} | {tc_list} |")
        lines.append("")
    else:
        lines.append("*Нет затронутых систем.*")
        lines.append("")

    # ============================================================
    # 4. IMPACT ESTIMATION
    # ============================================================
    lines.append("## 4. Оценка влияния (Impact Estimation)")
    lines.append("")

    # Методология
    lines.append("### 4.1 Методология оценки")
    lines.append("")
    lines.append("Установлена оценка в соответствии с методологией (все новые возможности считаем, что попадают в одну систему):")
    lines.append("")
    lines.append("| Уровень | Наименование | Критерий |")
    lines.append("|---------|--------------|----------|")
    lines.append("| S | Минимальная | Доработка 1 системы |")
    lines.append("| M | Низкая | Доработка не более 1-2 систем |")
    lines.append("| L | Средняя | Доработка не более 4 систем |")
    lines.append("| XL | Высокая | Доработка 5 и более систем |")
    lines.append("")

    # Результат оценки
    if impact_level:
        lines.append("### 4.2 Результат оценки")
        lines.append("")
        lines.append(f"**Уровень влияния: {impact_level_label or impact_level}**")
        lines.append("")

        # Подсчёт систем
        unique_system_codes: set[str] = set()
        for tc_entry in (impact_tcs or []):
            sys = tc_entry.get("system")
            if sys and isinstance(sys, dict):
                code = sys.get("code", "")
                if code:
                    unique_system_codes.add(code)
        lines.append(f"- Количество затронутых систем: **{len(unique_system_codes)}**")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Отчёт сгенерирован HLD Agent. Дата создания: {}*".format(
        datetime.now().strftime('%Y-%m-%d %H:%M')
    ))

    return "\n".join(lines)


async def publish_to_confluence(
    title: str,
    source: str,
    source_url: Optional[str] = None,
    page_title: Optional[str] = None,
    parent_page_url: Optional[str] = None,
    pat: Optional[str] = None,
    structured_requirements: Optional[list[dict]] = None,
    impact_tcs: Optional[list[dict]] = None,
    task_description: Optional[str] = None,
    impact_level: Optional[str] = None,
    impact_level_label: Optional[str] = None,
) -> str:
    """
    Публикация HLD-отчёта в Confluence.
    - title: название сессии (для заголовка отчёта)
    - source: источник ("text" | "confluence")
    - source_url: URL источника (опционально)
    - page_title: название страницы в Confluence (по умолчанию "HLD Report: {title}")
    - parent_page_url: полный URL родительской страницы
    - pat: Personal Access Token
    - structured_requirements: список структурированных требований
    - impact_tcs: список TC с системами (единый источник)
    Возвращает URL созданной страницы.
    """
    markdown = generate_markdown(
        title=title,
        source=source,
        source_url=source_url,
        structured_requirements=structured_requirements,
        impact_tcs=impact_tcs,
        task_description=task_description,
        impact_level=impact_level,
        impact_level_label=impact_level_label,
    )

    # Конвертируем Markdown в Confluence Storage Format (XHTML)
    storage_html = _markdown_to_confluence_storage(markdown)

    logger.info(
        "Generated Confluence storage HTML: markdown_len=%d, html_len=%d, html_preview=%s",
        len(markdown),
        len(storage_html),
        storage_html[:500].replace("\n", "\\n"),
    )

    # Извлекаем ID родительской страницы, base URL и space key из URL, если передан
    parent_page_id: Optional[str] = None
    confluence_base_url: Optional[str] = None
    space_key: Optional[str] = None
    if parent_page_url:
        parent_page_id = confluence_client._extract_page_id(parent_page_url)
        if not parent_page_id:
            raise ValueError(
                f"Не удалось извлечь ID страницы из URL родительской страницы: {parent_page_url}"
            )
        # Извлекаем base URL (https://host) из URL родительской страницы
        match = re.match(r"(https?://[^/]+)", parent_page_url)
        if match:
            confluence_base_url = match.group(1)
        # Извлекаем space key из URL родительской страницы
        match = re.search(r"/spaces/([^/]+)", parent_page_url)
        if match:
            space_key = match.group(1)

    # Название страницы
    confluence_title = (
        page_title.strip() if page_title else f"HLD Report: {title}"
    )

    # Публикуем через Confluence API
    page_id = await confluence_client.create_or_update_page(
        title=confluence_title,
        body=storage_html,
        parent_page_id=parent_page_id,
        pat=pat,
        confluence_base_url=confluence_base_url,
        space_key=space_key,
    )

    # Определяем base URL для формирования ссылки
    if confluence_base_url:
        base_url = confluence_base_url.rstrip("/")
    else:
        base_url = ""

    # Определяем space key для ссылки
    if not space_key and parent_page_url:
        match = re.search(r"/spaces/([^/]+)", parent_page_url)
        if match:
            space_key = match.group(1)

    page_url = f"{base_url}/spaces/{space_key}/pages/{page_id}"

    logger.info(
        "Published to Confluence: page_id=%s url=%s",
        page_id,
        page_url,
    )

    return page_url


def _markdown_to_confluence_storage(markdown: str) -> str:
    """
    Конвертация Markdown в Confluence Storage Format (XHTML).
    Поддерживает: заголовки h1-h3, таблицы, списки, жирный текст, ссылки, разделители.
    """
    lines = markdown.split("\n")
    html_parts: list[str] = []
    in_table = False
    in_list = False
    list_type: str | None = None

    def close_table():
        nonlocal in_table
        if in_table:
            html_parts.append("</table>")
            in_table = False

    def close_list():
        nonlocal in_list, list_type
        if in_list:
            html_parts.append(f"</{list_type}>")
            in_list = False
            list_type = None

    for line in lines:
        stripped = line.strip()

        # Разделитель
        if stripped == "---":
            close_table()
            close_list()
            html_parts.append("<hr/>")
            continue

        # Заголовки
        if stripped.startswith("#### "):
            close_table()
            close_list()
            html_parts.append(f"<h4>{_escape_html(stripped[5:])}</h4>")
            continue
        if stripped.startswith("### "):
            close_table()
            close_list()
            html_parts.append(f"<h3>{_escape_html(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            close_table()
            close_list()
            html_parts.append(f"<h2>{_escape_html(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            close_table()
            close_list()
            html_parts.append(f"<h1>{_escape_html(stripped[2:])}</h1>")
            continue

        # Таблицы
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not in_table:
                close_list()
                html_parts.append('<table data-layout="default">')
                in_table = True
                # Пропускаем строку разделителя (|---|)
                if all(
                    c.replace("-", "").replace(":", "").strip() == ""
                    for c in cells
                ):
                    continue
                html_parts.append("<tbody>")
            # Строка разделителя
            if all(
                c.replace("-", "").replace(":", "").strip() == ""
                for c in cells
            ):
                continue
            html_parts.append("<tr>")
            for cell in cells:
                html_parts.append(f"<td>{_escape_html(cell)}</td>")
            html_parts.append("</tr>")
            continue
        else:
            if in_table:
                html_parts.append("</tbody>")
                close_table()

        # Списки
        if stripped.startswith("- **") or stripped.startswith("- "):
            close_table()
            if not in_list:
                close_list()
                html_parts.append("<ul>")
                in_list = True
                list_type = "ul"
            content = stripped[2:]
            # Bold pattern: **text**
            content = _process_inline(content)
            html_parts.append(f"<li>{content}</li>")
            continue
        else:
            close_list()

        # Пустая строка
        if stripped == "":
            continue

        # Обычный текст (со звёздочками)
        close_table()
        close_list()
        content = _process_inline(stripped)
        html_parts.append(f"<p>{content}</p>")

    close_table()
    close_list()

    return "\n".join(html_parts)


def _process_inline(text: str) -> str:
    """Обработка inline-разметки: **жирный**, *курсив*.
    
    ВАЖНО: сначала экранируем HTML-спецсимволы, затем применяем
    markdown-подобную разметку, чтобы избежать генерации невалидного XHTML.
    """
    import html
    import re

    # 1. Экранируем HTML-спецсимволы, чтобы не сломать Confluence Storage Format
    text = html.escape(text, quote=True)

    # 2. Bold: **text** — html.escape не экранирует *, поэтому ** остаются как есть
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 3. Italic: *text*
    text = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text
    )
    return text


def _escape_html(text: str) -> str:
    """Экранирование HTML-спецсимволов."""
    import html

    return html.escape(text, quote=True)
