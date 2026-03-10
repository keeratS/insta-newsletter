from __future__ import annotations

import argparse
import hashlib
import os
import re
from datetime import datetime, timezone
from html import escape
from typing import Any

import requests

from insta_newsletter import LOOKBACK_DAYS, generate_newsletter

READWISE_SAVE_URL = "https://readwise.io/api/v3/save/"
DEFAULT_TAGS = ["instagram", "newsletter"]
PROJECT_URL = "https://github.com/keeratS/insta-newsletter"


def _build_document_url(newsletter_text: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha256(newsletter_text.encode("utf-8")).hexdigest()[:12]
    return f"https://insta-newsletter.local/{today}/{digest}"


def _build_newsletter_html(
    newsletter_text: str,
    title: str,
    *,
    generation_seconds: float | None = None,
    newsletter_model_used: str | None = None,
    poem_model_used: str | None = None,
    poet_inspiration: str | None = None,
    accounts_checked: int | None = None,
    accounts_with_recent_posts: int | None = None,
) -> str:
    safe_title = escape(title)
    body_html = _render_newsletter_body_html(newsletter_text)
    stats_html = _render_stats_html(
        generation_seconds=generation_seconds,
        newsletter_model_used=newsletter_model_used,
        poem_model_used=poem_model_used,
        poet_inspiration=poet_inspiration,
        accounts_checked=accounts_checked,
        accounts_with_recent_posts=accounts_with_recent_posts,
    )
    return (
        f"<article><h1>{safe_title}</h1>"
        f"{body_html}"
        f"{stats_html}"
        "</article>"
    )


def _render_stats_html(
    *,
    generation_seconds: float | None,
    newsletter_model_used: str | None,
    poem_model_used: str | None,
    poet_inspiration: str | None,
    accounts_checked: int | None,
    accounts_with_recent_posts: int | None,
) -> str:
    if (
        generation_seconds is None
        and newsletter_model_used is None
        and poem_model_used is None
        and poet_inspiration is None
        and accounts_checked is None
        and accounts_with_recent_posts is None
    ):
        return ""

    parts: list[str] = []
    if generation_seconds is not None:
        parts.append(f"Generated in {generation_seconds:.2f} seconds")
    if newsletter_model_used is not None:
        parts.append(f"Newsletter model used: {newsletter_model_used}")
    if poem_model_used is not None:
        parts.append(f"Poem model used: {poem_model_used}")
    if poet_inspiration is not None:
        parts.append(f"Poet inspiration: {poet_inspiration}")
    if accounts_checked is not None:
        parts.append(f"Accounts checked: {accounts_checked}")
    if accounts_with_recent_posts is not None:
        parts.append(f"Accounts with recent posts: {accounts_with_recent_posts}")
    summary = " | ".join(parts)
    return (
        f"<hr/><p><em>{escape(summary)}</em></p>"
        f"<p><a href=\"{escape(PROJECT_URL)}\">Project: insta-newsletter on GitHub</a></p>"
    )


def _render_newsletter_body_html(newsletter_text: str) -> str:
    lines = newsletter_text.splitlines()
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        blocks.append(
            f"<p>{'<br/>'.join(_render_inline_formatting(line) for line in paragraph_lines)}</p>"
        )
        paragraph_lines.clear()

    def flush_list() -> None:
        if not list_items:
            return
        items_html = "".join(f"<li>{_render_inline_formatting(item)}</li>" for item in list_items)
        blocks.append(f"<ul>{items_html}</ul>")
        list_items.clear()

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            flush_paragraph()
            flush_list()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            flush_paragraph()
            flush_list()
            level = len(heading_match.group(1))
            text = _render_inline_formatting(heading_match.group(2).strip())
            blocks.append(f"<h{level}>{text}</h{level}>")
            continue

        if re.match(r"^(TITLE|UPDATES|NEWSLETTER):\s*$", line, flags=re.IGNORECASE):
            flush_paragraph()
            flush_list()
            label = line.split(":", 1)[0].strip().title()
            blocks.append(f"<h2>{escape(label)}</h2>")
            continue

        if line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:].strip())
            continue

        flush_list()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    return "".join(blocks) if blocks else "<p></p>"


def _render_inline_formatting(text: str) -> str:
    chunks = re.split(r"(\*\*.+?\*\*)", text)
    rendered: list[str] = []
    for chunk in chunks:
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4:
            inner = chunk[2:-2]
            rendered.append(f"<strong>{escape(inner)}</strong>")
        else:
            rendered.append(escape(chunk))
    return "".join(rendered)


def send_newsletter_to_readwise(
    newsletter_text: str,
    *,
    access_token: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    lookback_days: int | None = None,
    generation_seconds: float | None = None,
    newsletter_model_used: str | None = None,
    poem_model_used: str | None = None,
    poet_inspiration: str | None = None,
    accounts_checked: int | None = None,
    accounts_with_recent_posts: int | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    if not newsletter_text.strip():
        raise ValueError("newsletter_text must not be empty")

    token = access_token or os.getenv("READWISE_ACCESS_TOKEN")
    if not token:
        raise ValueError("Missing Readwise token. Set READWISE_ACCESS_TOKEN.")

    lookback = lookback_days if lookback_days is not None else LOOKBACK_DAYS
    doc_title = title or f"{datetime.now().date().isoformat()} Instagram Newsletter"
    doc_summary = (
        summary
        or f"A short summary of selected Instagram profiles over the last {lookback} days."
    )
    payload: dict[str, Any] = {
        "url": _build_document_url(newsletter_text),
        "html": _build_newsletter_html(
            newsletter_text,
            doc_title,
            generation_seconds=generation_seconds,
            newsletter_model_used=newsletter_model_used,
            poem_model_used=poem_model_used,
            poet_inspiration=poet_inspiration,
            accounts_checked=accounts_checked,
            accounts_with_recent_posts=accounts_with_recent_posts,
        ),
        "title": doc_title,
        "summary": doc_summary,
        "category": "note",
        "saved_using": "insta-newsletter",
        "tags": tags or DEFAULT_TAGS,
    }
    if notes:
        payload["notes"] = notes

    try:
        response = requests.post(
            READWISE_SAVE_URL,
            headers={"Authorization": f"Token {token}"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.Timeout as e:
        raise RuntimeError(
            "Readwise API request timed out while saving the newsletter. "
            "Try again with --no-timeouts or retry later."
        ) from e


def run_newsletter_to_readwise(
    no_timeouts: bool = False,
    verbose: bool = False,
) -> int:
    token = os.getenv("READWISE_ACCESS_TOKEN")
    if not token:
        print(
            "Readwise wrapper failed: missing READWISE_ACCESS_TOKEN environment variable. "
            "Set it before running send_readwise_insta_newsletter.py (example: "
            "export READWISE_ACCESS_TOKEN=\"your_readwise_access_token\").",
        )
        return 1

    try:
        (
            summary,
            elapsed,
            accounts_checked,
            accounts_with_posts,
            model_used,
            poem_model_used,
            poet_style,
        ) = generate_newsletter(
            use_timeouts=not no_timeouts,
            verbose=verbose,
        )
    except Exception as e:
        print(f"Newsletter generation failed: {e}")
        if "Multiple authentication errors detected while contacting Instagram" in str(e):
            print("Newsletter was not sent to Readwise due to Instagram authentication errors.")
        return 1

    print(summary)
    print("\n---")
    print(f" Generated in {elapsed:.2f} seconds")
    print(f" Newsletter model used: {model_used}")
    print(f" Poem model used: {poem_model_used}")
    print(f" Poet inspiration: {poet_style}")
    print(f" Accounts checked: {accounts_checked}")
    print(f" Accounts with recent posts: {accounts_with_posts}")

    try:
        result = send_newsletter_to_readwise(
            summary,
            access_token=token,
            lookback_days=LOOKBACK_DAYS,
            generation_seconds=elapsed,
            newsletter_model_used=model_used,
            poem_model_used=poem_model_used,
            poet_inspiration=poet_style,
            accounts_checked=accounts_checked,
            accounts_with_recent_posts=accounts_with_posts,
            notes=f"Generated by insta-newsletter in {elapsed:.2f} seconds.",
            timeout=None if no_timeouts else 30,
        )
        print(f" Sent to Readwise feed (id={result.get('id', 'unknown')})")
        return 0
    except Exception as e:
        print(f"Readwise sync failed: {e}")
        return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate newsletter and send to Readwise."
    )
    parser.add_argument(
        "--no-timeouts",
        action="store_true",
        help="Disable network timeouts for Instagram, Ollama, and Readwise requests.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose logs, including full Phase 1 extracted JSON.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(
        run_newsletter_to_readwise(
            no_timeouts=args.no_timeouts,
            verbose=args.verbose,
        )
    )
