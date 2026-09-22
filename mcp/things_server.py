#!/usr/bin/env python3
"""Narrow local MCP bridge for reviewed, workload-aware Things 3 capture."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP


WORKLOAD_WEIGHTS = {
    "🍅": 1,
    "🍅🍅": 2,
    "🍅🍅🍅": 3,
    "☀️": 5,
    "☀️☀️": 10,
}
LEGACY_WORKLOAD_TAGS = {
    "35min × 1": "🍅",
    "35min × 2": "🍅🍅",
    "35min × 3": "🍅🍅🍅",
    "35min × 4 +": "☀️",
}


def _local_timezone() -> Any:
    """Honor an override, then use the Mac's configured IANA timezone."""
    configured = os.environ.get("THINGS_TIMEZONE") or os.environ.get("TZ")
    if configured:
        try:
            return ZoneInfo(configured)
        except Exception as exc:
            raise RuntimeError(f"Invalid timezone: {configured}") from exc

    try:
        target = str(Path("/etc/localtime").resolve())
        marker = "/zoneinfo/"
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except (OSError, ValueError):
        pass
    return datetime.now().astimezone().tzinfo


LOCAL_TIMEZONE = _local_timezone()


mcp = FastMCP(
    "things3",
    instructions=(
        "Local Things 3 bridge for listing structure and open workload, checking "
        "email source markers, initializing known workload tags, and creating "
        "reviewed to-dos. It cannot complete, delete, cancel, or edit to-dos."
    ),
)


def _run_applescript(script: str, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/osascript", "-", *args],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Unknown AppleScript error"
        raise RuntimeError(f"Things 3 automation failed: {message}")
    return result.stdout.strip()


def _run_jxa(script: str, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-", *args],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Unknown JavaScript for Automation error"
        raise RuntimeError(f"Things 3 automation failed: {message}")
    return result.stdout.strip()


def _validate_iso_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
    return parsed.date().isoformat()


def _to_local_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=LOCAL_TIMEZONE)
    return instant.astimezone(LOCAL_TIMEZONE).date().isoformat()


def _split_tags(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _workload_summary(tags: list[str]) -> dict[str, Any]:
    matches = [tag for tag in tags if tag in WORKLOAD_WEIGHTS]
    if not matches:
        return {
            "workload_tag": None,
            "effective_workload_tag": "🍅",
            "workload_weight": 1,
            "workload_issue": "missing workload tag; counted as 🍅 without editing",
        }
    effective = max(matches, key=WORKLOAD_WEIGHTS.__getitem__)
    issue = None
    if len(matches) > 1:
        issue = "multiple workload tags; counted using the largest weight"
    return {
        "workload_tag": matches[0] if len(matches) == 1 else None,
        "effective_workload_tag": effective,
        "workload_weight": WORKLOAD_WEIGHTS[effective],
        "workload_issue": issue,
    }


def _list_projects() -> list[dict[str, str]]:
    script = r'''
on run argv
    set oldDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    tell application "Things3"
        set outputRows to {}
        repeat with p in projects
            set areaName to ""
            try
                set areaName to name of area of p as text
            end try
            set end of outputRows to ((id of p as text) & tab & (name of p as text) & tab & areaName)
        end repeat
    end tell
    set outputText to outputRows as text
    set AppleScript's text item delimiters to oldDelimiters
    return outputText
end run
'''
    output = _run_applescript(script)
    projects: list[dict[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            projects.append(
                {
                    "id": parts[0],
                    "title": parts[1],
                    "area": parts[2] if len(parts) == 3 else "",
                }
            )
    return projects


def _list_named_collection(collection_name: str) -> list[dict[str, str]]:
    if collection_name not in {"areas", "tags"}:
        raise ValueError("unsupported Things collection")
    script = r'''
on run argv
    set collectionName to item 1 of argv
    set oldDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    tell application "Things3"
        if collectionName is "areas" then
            set sourceItems to areas
        else
            set sourceItems to tags
        end if
        set outputRows to {}
        repeat with sourceItem in sourceItems
            set end of outputRows to ((id of sourceItem as text) & tab & (name of sourceItem as text))
        end repeat
    end tell
    set outputText to outputRows as text
    set AppleScript's text item delimiters to oldDelimiters
    return outputText
end run
'''
    output = _run_applescript(script, collection_name)
    items: list[dict[str, str]] = []
    for line in output.splitlines():
        item_id, separator, title = line.partition("\t")
        if separator:
            items.append({"id": item_id, "title": title})
    return items


def _read_open_todos() -> list[dict[str, Any]]:
    script = r'''
function run(argv) {
    const app = Application("Things3");
    function safe(fn, fallback) {
        try {
            const value = fn();
            return value === undefined ? fallback : value;
        } catch (error) {
            return fallback;
        }
    }
    function iso(fn) {
        const value = safe(fn, null);
        if (!value) return null;
        try { return value.toISOString(); } catch (error) { return null; }
    }
    const rows = app.toDos().map(function (todo) {
        return {
            id: safe(function () { return todo.id(); }, ""),
            title: safe(function () { return todo.name(); }, ""),
            status: safe(function () { return String(todo.status()); }, "open"),
            activation_date: iso(function () { return todo.activationDate(); }),
            due_date: iso(function () { return todo.dueDate(); }),
            area: safe(function () { return todo.area().name(); }, ""),
            project: safe(function () { return todo.project().name(); }, ""),
            tag_names: safe(function () { return todo.tagNames(); }, "")
        };
    });
    return JSON.stringify(rows);
}
'''
    output = _run_jxa(script)
    try:
        rows = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Things 3 returned invalid task data") from exc
    if not isinstance(rows, list):
        raise RuntimeError("Things 3 returned an unexpected task payload")
    return rows


def _list_open_todos(
    start_date: str | None = None,
    end_date: str | None = None,
    include_unscheduled: bool = True,
) -> dict[str, Any]:
    start = _validate_iso_date(start_date, "start_date")
    end = _validate_iso_date(end_date, "end_date")
    if start and end and start > end:
        raise ValueError("start_date must not be after end_date")

    todos: list[dict[str, Any]] = []
    daily_workload: dict[str, int] = {}
    for row in _read_open_todos():
        status = str(row.get("status", "open")).lower()
        if status and "open" not in status:
            continue
        scheduled_date = _to_local_date(row.get("activation_date"))
        due_date = _to_local_date(row.get("due_date"))
        if scheduled_date is None:
            if not include_unscheduled:
                continue
        elif (start and scheduled_date < start) or (end and scheduled_date > end):
            continue

        tags = _split_tags(row.get("tag_names"))
        workload = _workload_summary(tags)
        todo = {
            "id": str(row.get("id", "")),
            "title": str(row.get("title", "")),
            "status": "open",
            "start_date": scheduled_date,
            "deadline": due_date,
            "area": str(row.get("area", "")),
            "project": str(row.get("project", "")),
            "tags": tags,
            **workload,
        }
        todos.append(todo)
        if scheduled_date:
            daily_workload[scheduled_date] = (
                daily_workload.get(scheduled_date, 0) + workload["workload_weight"]
            )
    return {
        "count": len(todos),
        "timezone": str(LOCAL_TIMEZONE),
        "daily_workload": dict(sorted(daily_workload.items())),
        "todos": todos,
    }


def _find_source_marker(marker: str) -> list[dict[str, str]]:
    if not marker.startswith("mail-source:") or len(marker) > 500:
        raise ValueError("marker must be a mail-source marker of at most 500 characters")
    script = r'''
on run argv
    set markerText to item 1 of argv
    set oldDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    tell application "Things3"
        set outputRows to {}
        repeat with t in to dos
            set noteText to ""
            try
                set noteText to notes of t as text
            end try
            if noteText contains markerText then
                set end of outputRows to ((id of t as text) & tab & (name of t as text))
            end if
        end repeat
    end tell
    set outputText to outputRows as text
    set AppleScript's text item delimiters to oldDelimiters
    return outputText
end run
'''
    output = _run_applescript(script, marker)
    matches: list[dict[str, str]] = []
    for line in output.splitlines():
        item_id, separator, title = line.partition("\t")
        if separator:
            matches.append({"id": item_id, "title": title})
    return matches


def _initialize_workload_tags() -> dict[str, Any]:
    script = r'''
on tagNamed(tagName)
    tell application "Things3"
        repeat with candidateTag in tags
            if (name of candidateTag as text) is tagName then return candidateTag
        end repeat
    end tell
    return missing value
end tagNamed

on run argv
    set oldNames to {item 1 of argv, item 3 of argv, item 5 of argv, item 7 of argv}
    set newNames to {item 2 of argv, item 4 of argv, item 6 of argv, item 8 of argv}
    set finalName to item 9 of argv
    set outputRows to {}
    repeat with indexValue from 1 to count oldNames
        set oldName to item indexValue of oldNames
        set newName to item indexValue of newNames
        set oldTag to my tagNamed(oldName)
        set newTag to my tagNamed(newName)
        if newTag is not missing value then
            if oldTag is not missing value then
                set end of outputRows to ("conflict" & tab & oldName & tab & newName)
            else
                set end of outputRows to ("already-present" & tab & oldName & tab & newName)
            end if
        else if oldTag is not missing value then
            tell application "Things3" to set name of oldTag to newName
            set end of outputRows to ("renamed" & tab & oldName & tab & newName)
        else
            tell application "Things3" to make new tag with properties {name:newName}
            set end of outputRows to ("created" & tab & oldName & tab & newName)
        end if
    end repeat

    set finalTag to my tagNamed(finalName)
    if finalTag is missing value then
        tell application "Things3" to make new tag with properties {name:finalName}
        set end of outputRows to ("created" & tab & "" & tab & finalName)
    else
        set end of outputRows to ("already-present" & tab & "" & tab & finalName)
    end if

    set oldDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    set outputText to outputRows as text
    set AppleScript's text item delimiters to oldDelimiters
    return outputText
end run
'''
    args: list[str] = []
    for old_name, new_name in LEGACY_WORKLOAD_TAGS.items():
        args.extend([old_name, new_name])
    args.append("☀️☀️")
    output = _run_applescript(script, *args)
    actions: list[dict[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            actions.append({"action": parts[0], "from": parts[1], "to": parts[2]})
    conflicts = [item for item in actions if item["action"] == "conflict"]
    return {"complete": not conflicts, "actions": actions, "conflicts": conflicts}


@mcp.tool()
def things3_list_projects() -> dict[str, Any]:
    """List current Things 3 projects and their parent areas."""
    projects = _list_projects()
    return {"count": len(projects), "projects": projects}


@mcp.tool()
def things3_list_areas() -> dict[str, Any]:
    """List current Things 3 areas for dynamic destination selection."""
    areas = _list_named_collection("areas")
    return {"count": len(areas), "areas": areas}


@mcp.tool()
def things3_list_tags() -> dict[str, Any]:
    """List current Things 3 tags."""
    tags = _list_named_collection("tags")
    return {"count": len(tags), "tags": tags}


@mcp.tool()
def things3_list_open_todos(
    start_date: str | None = None,
    end_date: str | None = None,
    include_unscheduled: bool = True,
) -> dict[str, Any]:
    """List open to-dos and workload, optionally filtered by scheduled YYYY-MM-DD dates."""
    return _list_open_todos(start_date, end_date, include_unscheduled)


@mcp.tool()
def things3_find_source_marker(marker: str) -> dict[str, Any]:
    """Check whether an email source marker already exists in Things notes."""
    matches = _find_source_marker(marker)
    return {"found": bool(matches), "matches": matches}


@mcp.tool()
def things3_initialize_workload_tags(user_confirmed: bool = False) -> dict[str, Any]:
    """Rename known legacy workload tags and create missing emoji tags after confirmation."""
    if not user_confirmed:
        raise ValueError("Workload tag initialization requires explicit user confirmation")
    return _initialize_workload_tags()


@mcp.tool()
def things3_create_todo(
    title: str,
    notes: str = "",
    when: str | None = None,
    deadline: str | None = None,
    tags: list[str] | None = None,
    list_name: str | None = None,
    heading: str | None = None,
    checklist_items: list[str] | None = None,
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Create one reviewed Things 3 to-do with exactly one workload tag."""
    if not user_confirmed:
        raise ValueError("Creation requires explicit approval of the displayed preview")

    clean_title = title.strip()
    if not clean_title or len(clean_title) > 1000:
        raise ValueError("title must contain 1 to 1000 characters")
    if len(notes) > 10000:
        raise ValueError("notes must not exceed 10000 characters")
    if tags and len(tags) > 50:
        raise ValueError("at most 50 tags may be supplied")
    if checklist_items and len(checklist_items) > 100:
        raise ValueError("at most 100 checklist items may be supplied")

    clean_tags = list(dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip()))
    workload_tags = [tag for tag in clean_tags if tag in WORKLOAD_WEIGHTS]
    if len(workload_tags) != 1:
        raise ValueError("exactly one workload tag is required")
    existing_tags = {item["title"] for item in _list_named_collection("tags")}
    missing_tags = [tag for tag in clean_tags if tag not in existing_tags]
    if missing_tags:
        raise ValueError(f"unknown Things tags: {', '.join(missing_tags)}")

    clean_when = _validate_iso_date(when, "when")
    clean_deadline = _validate_iso_date(deadline, "deadline")
    if clean_when and clean_deadline and clean_when > clean_deadline:
        raise ValueError("when must not be after deadline")

    clean_list_name = list_name.strip() if list_name else None
    if clean_list_name in {"Inbox", "收件箱"}:
        clean_list_name = None
    if clean_list_name:
        valid_destinations = {
            item["title"] for item in _list_named_collection("areas")
        } | {item["title"] for item in _list_projects()}
        if clean_list_name not in valid_destinations:
            raise ValueError("list_name must match a current Things area or project")

    params: dict[str, str] = {"title": clean_title}
    optional = {
        "notes": notes or None,
        "when": clean_when,
        "deadline": clean_deadline,
        "tags": ",".join(clean_tags),
        "list": clean_list_name,
        "heading": heading,
        "checklist-items": "\n".join(checklist_items) if checklist_items else None,
    }
    params.update({key: value for key, value in optional.items() if value is not None})

    url = "things:///add?" + urlencode(params, quote_via=quote, safe="")
    result = subprocess.run(
        ["/usr/bin/open", url],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Unable to open Things URL"
        raise RuntimeError(message)
    return {
        "created": True,
        "title": clean_title,
        "destination": clean_list_name or "Inbox",
        "workload_tag": workload_tags[0],
        "when": clean_when,
        "deadline": clean_deadline,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
