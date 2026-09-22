#!/usr/bin/env python3
"""Read-only Gmail MCP with concurrent, memory-only message prefetching."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header, make_header
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP


ACCOUNT_ALIAS = os.environ.get("GMAIL_ACCOUNT_ALIAS", "gmail")
PREFETCH_WORKERS = 8
MESSAGE_CACHE_SIZE = 512
MAX_BATCH_MESSAGES = 20


mcp = FastMCP(
    "gmail-readonly",
    instructions=(
        "Read-only Gmail access for auditable mail triage. Search concurrently "
        "prefetches full messages into a process-local memory cache; single and "
        "batch tools return cleaned bodies. It cannot send, draft, label, archive, "
        "trash, or delete messages."
    ),
)


def _gws_command() -> str:
    command = os.environ.get("GWS_COMMAND") or shutil.which("gws")
    if not command:
        raise RuntimeError(
            "Google Workspace CLI (gws) was not found. Install it or set GWS_COMMAND."
        )
    return command


def _run_gws(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        [_gws_command(), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        try:
            error = json.loads(result.stdout or result.stderr)
            message = error.get("error", {}).get("message") or str(error)
        except (json.JSONDecodeError, AttributeError):
            message = (result.stderr or result.stdout).strip() or "Unknown Gmail error"
        raise RuntimeError(f"Gmail read failed: {message}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gmail returned invalid JSON") from exc


@lru_cache(maxsize=1)
def _account_email() -> str:
    profile = _run_gws(
        "gmail",
        "users",
        "getProfile",
        "--params",
        json.dumps({"userId": "me"}),
    )
    email_address = str(profile.get("emailAddress", "")).strip()
    if not email_address:
        raise RuntimeError("Gmail profile did not include an account email")
    return email_address


def _validate_message_id(message_id: str) -> str:
    clean_id = str(message_id).strip()
    if not clean_id or len(clean_id) > 200:
        raise ValueError("invalid Gmail message ID")
    return clean_id


@lru_cache(maxsize=MESSAGE_CACHE_SIZE)
def _get_raw_message_cached(message_id: str) -> dict[str, Any]:
    """Fetch and retain one immutable full message only in process memory."""
    clean_id = _validate_message_id(message_id)
    return _run_gws(
        "gmail",
        "users",
        "messages",
        "get",
        "--params",
        json.dumps(
            {"userId": "me", "id": clean_id, "format": "full"},
            ensure_ascii=False,
        ),
    )


def _message_web_url(thread_id: str, account_email: str) -> str:
    if not thread_id:
        return ""
    return (
        "https://mail.google.com/mail/u/?authuser="
        + quote(account_email, safe="")
        + "#all/"
        + quote(thread_id, safe="")
    )


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    wanted = {"from", "to", "cc", "subject", "date", "message-id"}
    result: dict[str, str] = {}
    for item in payload.get("headers", []):
        name = str(item.get("name", "")).lower()
        if name in wanted:
            result[name] = _decode_header(str(item.get("value", "")))
    return result


def _decode_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


class _HTMLTextExtractor(HTMLParser):
    """Reduce email HTML while retaining visible text and actionable links."""

    _SKIP_TAGS = {"head", "script", "style", "svg", "template"}
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.link_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = next((value for key, value in attrs if key.lower() == "href"), None)
            self.link_stack.append(href)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "a" and self.link_stack:
            href = self.link_stack.pop()
            if href and not href.lower().startswith(("javascript:", "data:")):
                self.parts.append(f" <{href}>")
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _compact_text(value: str) -> str:
    value = (
        html.unescape(value)
        .replace("\u00a0", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines: list[str] = []
    previous_blank = False
    for raw_line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
            previous_blank = False
        elif not previous_blank and lines:
            lines.append("")
            previous_blank = True
    return "\n".join(lines).strip()


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return _compact_text(re.sub(r"<[^>]+>", " ", value))
    return _compact_text(parser.text())


def _body_parts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    mime_type = str(payload.get("mimeType", "")).lower()
    body_data = str(payload.get("body", {}).get("data", ""))

    if mime_type == "text/plain" and body_data:
        plain_parts.append(_decode_body(body_data))
    elif mime_type == "text/html" and body_data:
        html_parts.append(_decode_body(body_data))

    for part in payload.get("parts", []) or []:
        nested_plain, nested_html = _body_parts(part)
        plain_parts.extend(nested_plain)
        html_parts.extend(nested_html)
    return plain_parts, html_parts


def _message_body(payload: dict[str, Any]) -> str:
    plain_parts, html_parts = _body_parts(payload)
    nonempty_plain = [part for part in plain_parts if part.strip()]
    if nonempty_plain:
        return _compact_text("\n\n".join(nonempty_plain))
    nonempty_html = [part for part in html_parts if part.strip()]
    if nonempty_html:
        return _html_to_text("\n\n".join(nonempty_html))
    return ""


def _cache_delta(before: Any, after: Any) -> dict[str, int]:
    return {
        "hits": after.hits - before.hits,
        "misses": after.misses - before.misses,
        "current_size": after.currsize,
        "max_size": after.maxsize or MESSAGE_CACHE_SIZE,
    }


def _prefetch_messages(
    message_ids: list[str],
) -> tuple[list[dict[str, Any] | None], list[dict[str, str]], dict[str, int]]:
    clean_ids = [_validate_message_id(message_id) for message_id in message_ids]
    before = _get_raw_message_cached.cache_info()
    rows: list[dict[str, Any] | None] = [None] * len(clean_ids)
    failures: list[dict[str, str]] = []
    if not clean_ids:
        return rows, failures, _cache_delta(before, _get_raw_message_cached.cache_info())

    with ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(clean_ids))) as pool:
        future_indexes = {
            pool.submit(_get_raw_message_cached, message_id): (index, message_id)
            for index, message_id in enumerate(clean_ids)
        }
        for future in as_completed(future_indexes):
            index, message_id = future_indexes[future]
            try:
                rows[index] = future.result()
            except Exception as exc:
                failures.append({"id": message_id, "error": str(exc)})

    failures.sort(key=lambda item: clean_ids.index(item["id"]))
    after = _get_raw_message_cached.cache_info()
    return rows, failures, _cache_delta(before, after)


def _message_metadata(raw: dict[str, Any], account_email: str) -> dict[str, Any]:
    headers = _headers(raw.get("payload", {}))
    thread_id = str(raw.get("threadId", ""))
    return {
        "id": raw.get("id", ""),
        "thread_id": thread_id,
        "web_url": _message_web_url(thread_id, account_email),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "rfc_message_id": headers.get("message-id", ""),
        "labels": raw.get("labelIds", []),
        "snippet": raw.get("snippet", ""),
    }


def _message_with_body(
    raw: dict[str, Any], account_email: str, max_body_chars: int
) -> dict[str, Any]:
    result = _message_metadata(raw, account_email)
    body = _message_body(raw.get("payload", {}))
    truncated = len(body) > max_body_chars
    result.update(
        {
            "body": body[:max_body_chars] if truncated else body,
            "body_chars": len(body),
            "body_truncated": truncated,
        }
    )
    return result


def _search_messages(
    query: str,
    max_results: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    if not query.strip() or len(query) > 1000:
        raise ValueError("query must contain 1 to 1000 characters")
    if not 1 <= max_results <= 50:
        raise ValueError("max_results must be between 1 and 50")
    if page_token is not None and (not page_token.strip() or len(page_token) > 1000):
        raise ValueError("page_token must contain 1 to 1000 characters")

    list_params: dict[str, Any] = {
        "userId": "me",
        "q": query,
        "maxResults": max_results,
        "includeSpamTrash": False,
    }
    if page_token:
        list_params["pageToken"] = page_token
    listing = _run_gws(
        "gmail",
        "users",
        "messages",
        "list",
        "--params",
        json.dumps(list_params, ensure_ascii=False),
    )

    message_ids = [
        str(item.get("id", ""))
        for item in listing.get("messages", []) or []
        if item.get("id")
    ]
    rows, failures, cache = _prefetch_messages(message_ids)
    account_email = _account_email()
    messages = [
        _message_metadata(raw, account_email) for raw in rows if raw is not None
    ]
    return {
        "account_alias": ACCOUNT_ALIAS,
        "account_email": account_email,
        "matched_count": len(message_ids),
        "count": len(messages),
        "complete": not failures,
        "prefetched_full_messages": True,
        "cache": cache,
        "fetch_failures": failures,
        "next_page_token": listing.get("nextPageToken"),
        "messages": messages,
    }


def _get_messages(message_ids: list[str], max_body_chars: int = 12000) -> dict[str, Any]:
    if not isinstance(message_ids, list) or not message_ids:
        raise ValueError("message_ids must contain 1 to 20 message IDs")
    if len(message_ids) > MAX_BATCH_MESSAGES:
        raise ValueError("message_ids must contain at most 20 message IDs")
    if not 1000 <= max_body_chars <= 50000:
        raise ValueError("max_body_chars must be between 1000 and 50000")

    clean_ids = list(dict.fromkeys(_validate_message_id(item) for item in message_ids))
    rows, failures, cache = _prefetch_messages(clean_ids)
    account_email = _account_email()
    messages = [
        _message_with_body(raw, account_email, max_body_chars)
        for raw in rows
        if raw is not None
    ]
    return {
        "account_alias": ACCOUNT_ALIAS,
        "account_email": account_email,
        "requested_count": len(clean_ids),
        "count": len(messages),
        "complete": not failures,
        "cache": cache,
        "fetch_failures": failures,
        "messages": messages,
    }


@mcp.tool()
def gmail_search_messages(
    query: str,
    max_results: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Search one Gmail page and concurrently prefetch full messages into memory."""
    return _search_messages(query, max_results, page_token)


@mcp.tool()
def gmail_get_messages(
    message_ids: list[str], max_body_chars: int = 12000
) -> dict[str, Any]:
    """Read cleaned bodies for up to 20 messages, using the volatile prefetch cache."""
    return _get_messages(message_ids, max_body_chars)


@mcp.tool()
def gmail_get_message(message_id: str, max_body_chars: int = 20000) -> dict[str, Any]:
    """Read one cleaned Gmail body, allowing a larger limit for truncated messages."""
    if not 1000 <= max_body_chars <= 200000:
        raise ValueError("max_body_chars must be between 1000 and 200000")
    clean_id = _validate_message_id(message_id)
    before = _get_raw_message_cached.cache_info()
    raw = _get_raw_message_cached(clean_id)
    after = _get_raw_message_cached.cache_info()
    result = _message_with_body(raw, _account_email(), max_body_chars)
    result.update(
        {
            "account_alias": ACCOUNT_ALIAS,
            "account_email": _account_email(),
            "cache": _cache_delta(before, after),
        }
    )
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
