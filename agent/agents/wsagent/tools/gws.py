"""Google Workspace tools: direct Google APIs. Scopes on the stored refresh
tokens are *.readonly only — the write scope never exists."""

from typing import Any

import httpx
from google.adk.tools.tool_context import ToolContext

from ..config import Project, registry
from ..gateway import secrets
from ..gateway.envelope import NotConfigured, fan_out
from ..gateway.provider import OAuthRefresher
from ..schemas import Item, Source

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

_refresher: OAuthRefresher | None = None


def _token(project: Project) -> str:
    global _refresher
    if project.gws is None:
        raise NotConfigured()
    if _refresher is None:
        import json
        import os

        client = json.loads(os.environ["WS_GOOGLE_OAUTH_CLIENT"])  # deploy-injected, non-secret id
        _refresher = OAuthRefresher(
            service="gws",
            token_endpoint=GOOGLE_TOKEN_ENDPOINT,
            client_id=client["client_id"],
            client_secret=secrets.store().get(client["client_secret_secret"]),
            secrets=secrets.store(),
        )
    return _refresher.get(project.id, project.gws.refresh_token_secret)


async def _api(project: Project, url: str, params: dict[str, Any]) -> dict[str, Any]:
    token = _token(project)
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return dict(resp.json())


# ---- Google Docs -> Markdown ----

_HEADING_DEPTH = {
    "TITLE": 1,
    "SUBTITLE": 2,
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}


def _render_paragraph(paragraph: dict[str, Any], base_depth: int) -> str:
    text = "".join(
        el.get("textRun", {}).get("content", "") for el in paragraph.get("elements", [])
    ).rstrip("\n")
    if not text.strip():
        return ""
    style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
    if style in _HEADING_DEPTH:
        depth = min(base_depth + _HEADING_DEPTH[style], 6)
        return f"{'#' * depth} {text.strip()}"
    if paragraph.get("bullet"):
        return f"- {text.strip()}"
    return text


def _render_body(content: list[dict[str, Any]], base_depth: int) -> list[str]:
    lines: list[str] = []
    for element in content:
        if paragraph := element.get("paragraph"):
            lines.append(_render_paragraph(paragraph, base_depth))
        elif table := element.get("table"):
            for row in table.get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    cell_text = " ".join(
                        line
                        for line in _render_body(cell.get("content", []), base_depth)
                        if line
                    )
                    cells.append(cell_text.replace("|", "\\|"))
                lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_tabs(tabs: list[dict[str, Any]], depth: int = 1) -> list[str]:
    out: list[str] = []
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "(untitled tab)")
        out.append(f"{'#' * min(depth, 6)} {title}")
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        out.extend(_render_body(body, base_depth=depth))
        out.extend(_render_tabs(tab.get("childTabs", []), depth + 1))
    return out


def render_gdoc(doc: dict[str, Any]) -> str:
    """Whole document (all tabs) as Markdown. Tabbed docs need
    includeTabsContent=true on the API call or most content is silently lost."""
    tabs = doc.get("tabs", [])
    if tabs:
        lines = _render_tabs(tabs, depth=1)
    else:
        lines = _render_body(doc.get("body", {}).get("content", []), base_depth=0)
    return "\n".join(line for line in lines if line)


def render_gsheet_values(tabs: dict[str, list[list[Any]]]) -> str:
    """batchGet values as Markdown pipe tables, one section per tab."""
    lines: list[str] = []
    for title, rows in tabs.items():
        lines.append(f"# {title}")
        for row in rows:
            lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def render_slides(presentation: dict[str, Any]) -> str:
    """Presentation text: one section per slide, text elements in order."""
    lines: list[str] = []
    for i, page in enumerate(presentation.get("slides", []), start=1):
        lines.append(f"# Slide {i}")
        for element in page.get("pageElements", []):
            for te in element.get("shape", {}).get("text", {}).get("textElements", []):
                content = te.get("textRun", {}).get("content", "").rstrip("\n")
                if content.strip():
                    lines.append(content)
    return "\n".join(lines)


async def _read_content(project: Project, file_id: str, mime: str) -> str:
    if mime == "application/vnd.google-apps.document":
        doc = await _api(
            project,
            f"https://docs.googleapis.com/v1/documents/{file_id}",
            {"includeTabsContent": True},
        )
        return render_gdoc(doc)
    if mime == "application/vnd.google-apps.spreadsheet":
        meta = await _api(
            project,
            f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}",
            {"includeGridData": False, "fields": "sheets(properties(title,sheetType))"},
        )
        # Chart/timeline sheets have no cell grid; requesting their range is a 400.
        titles = [
            s["properties"]["title"]
            for s in meta.get("sheets", [])
            if s["properties"].get("sheetType", "GRID") == "GRID"
        ]
        if not titles:
            return ""
        resp = await _api(
            project,
            f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}/values:batchGet",
            {
                "ranges": [f"'{t}'" for t in titles],
                "valueRenderOption": "FORMATTED_VALUE",
                "dateTimeRenderOption": "FORMATTED_STRING",
            },
        )
        values = {
            t: r.get("values", [])
            for t, r in zip(titles, resp.get("valueRanges", []), strict=False)
        }
        return render_gsheet_values(values)
    if mime == "application/vnd.google-apps.presentation":
        pres = await _api(
            project, f"https://slides.googleapis.com/v1/presentations/{file_id}", {}
        )
        return render_slides(pres)
    return f"(unsupported mime type: {mime}; use webViewLink)"


async def search_drive_files(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search Google Drive files (Docs, Sheets, Slides, and others).

    Use this to find documents by name or content keyword. Returns a snapshot
    envelope; follow up with read_drive_document for full content.

    Args:
        query: Keyword to match file names and content.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        q = f"fullText contains '{query}' and trashed = false"
        body = await _api(
            project,
            "https://www.googleapis.com/drive/v3/files",
            {
                "q": q,
                "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
                "pageSize": 20,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "corpora": "allDrives",
            },
        )
        return [
            Item(
                project=project.id,
                url=f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}",
                title=f["name"],
                extra={"file_id": f["id"], "mime": f["mimeType"], "modified": f["modifiedTime"]},
            )
            for f in body.get("files", [])
        ]

    return (await fan_out(Source.DRIVE, projects, fetch)).to_tool_result()


async def read_drive_document(file_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Read the content of one Drive file (Doc, Sheet, or Slides).

    Use this after search_drive_files. Dispatches by MIME type:
    Docs -> documents.get (includeTabsContent), Sheets -> values.batchGet,
    Slides -> presentations.get.

    Args:
        file_id: Drive file id from a previous search result.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        try:
            meta = await _api(
                project,
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                {"fields": "id,name,mimeType,webViewLink", "supportsAllDrives": True},
            )
        except httpx.HTTPStatusError as e:
            # Invisible to this project's credential, but another project may
            # hold the file: an empty result here, not a failure.
            if e.response.status_code in (403, 404):
                return []
            raise
        body = await _read_content(project, file_id, meta["mimeType"])
        return [
            Item(
                project=project.id,
                url=meta.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}",
                title=meta["name"],
                body=body,
                extra={"file_id": file_id, "mime": meta["mimeType"]},
            )
        ]

    return (await fan_out(Source.DRIVE, projects, fetch)).to_tool_result()
