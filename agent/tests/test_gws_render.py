"""GWS content rendering: Docs (tabs/tables), Sheets, Slides -> text."""

from typing import Any

from agents.wsagent.tools.gws import render_gdoc, render_gsheet_values, render_slides


def _para(text: str, style: str = "NORMAL_TEXT", bullet: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {
        "elements": [{"textRun": {"content": text + "\n"}}],
        "paragraphStyle": {"namedStyleType": style},
    }
    if bullet:
        p["bullet"] = {"listId": "kix.x"}
    return {"paragraph": p}


def test_gdoc_tabs_render_all_content() -> None:
    doc = {
        "tabs": [
            {
                "tabProperties": {"title": "概要"},
                "documentTab": {
                    "body": {"content": [_para("見出し", "HEADING_1"), _para("本文です")]}
                },
                "childTabs": [
                    {
                        "tabProperties": {"title": "詳細"},
                        "documentTab": {"body": {"content": [_para("箇条書き", bullet=True)]}},
                    }
                ],
            }
        ]
    }
    md = render_gdoc(doc)
    assert "# 概要" in md
    assert "## 見出し" in md  # heading depth offset by tab depth
    assert "本文です" in md
    assert "## 詳細" in md  # child tab one level deeper
    assert "- 箇条書き" in md


def test_gdoc_table_renders_pipe_rows() -> None:
    doc = {
        "body": {
            "content": [
                {
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {"content": [_para("a|b")]},
                                    {"content": [_para("c")]},
                                ]
                            }
                        ]
                    }
                }
            ]
        }
    }
    assert render_gdoc(doc) == "| a\\|b | c |"


def test_gsheet_values_render() -> None:
    md = render_gsheet_values({"集計": [["名前", "数"], ["x", 3]]})
    assert md.splitlines() == ["# 集計", "| 名前 | 数 |", "| x | 3 |"]


def test_slides_render() -> None:
    pres = {
        "slides": [
            {
                "pageElements": [
                    {
                        "shape": {
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "タイトル\n"}},
                                    {"textRun": {"content": "\n"}},
                                ]
                            }
                        }
                    }
                ]
            }
        ]
    }
    assert render_slides(pres) == "# Slide 1\nタイトル"
