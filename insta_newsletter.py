#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


INSTAGRAM_APP_ID = "936619743392459"
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_SHOW_URL = "http://localhost:11434/api/show"
NEWSLETTER_OLLAMA_MODEL = "qwen3:8b"
POEM_OLLAMA_MODEL = "qwen3:8b"
NEWSLETTER_TARGET_WORDS = 600
TOKENS_PER_WORD_ESTIMATE = 1.3
OLLAMA_TIMEOUT_SECONDS = 600
OLLAMA_CONTEXT_WARNING_RATIO = 0.9
OLLAMA_MODEL_INFO_CACHE_FILE = Path(".cache/ollama_model_info.json")
OLLAMA_MODEL_INFO_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
INSTAGRAM_REQUEST_TIMEOUT_SECONDS = 60
LOOKBACK_DAYS = 3
PROFILES_FILE = "profiles.txt"
INSTAGRAM_CACHE_DIR = Path(".cache/instagram_profiles")
INSTAGRAM_EXTRACTION_CACHE_DIR = Path(".cache/instagram_extractions")
INSTAGRAM_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
INSTAGRAM_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60  # 24 hours
INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS = 24 * 60 * 60  # 24 hours
MIN_CACHED_PROFILES_FOR_REDUCED_MODE = 11
POEM_MAX_LINES = 7
MAX_PROMPT_POSTS_PER_ACCOUNT = 2
MAX_PROMPT_CAPTION_CHARS = 280
POEM_STYLES = [
    "Rumi",
    "Mary Oliver",
    "Shakespeare",
    "Robert Frost",
    "Ada Limon",
    "Emily Dickinson",
]
STAR_SPINNER_FRAMES = [
    "*  ",
    "** ",
    "***",
    " **",
    "  *",
    " **",
    "***",
    "** ",
]
INSTAGRAM_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "x-ig-app-id": INSTAGRAM_APP_ID,
}
_MODEL_CONTEXT_LENGTH_CACHE: dict[str, int | None] = {}


@dataclass
class Post:
    username: str
    taken_at_timestamp: int | None
    caption: str
    shortcode: str

    @property
    def post_url(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/"

    @property
    def taken_at_iso(self) -> str:
        if self.taken_at_timestamp is None:
            return "Unknown (HTML fallback)"
        return datetime.fromtimestamp(
            self.taken_at_timestamp, tz=timezone.utc
        ).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def ensure_profiles_file():
    profiles_path = Path("profiles.txt")
    example_path = Path("profiles.example.txt")

    if not profiles_path.exists():
        if example_path.exists():
            shutil.copy(example_path, profiles_path)
            print(
                "Created profiles.txt from profiles.example.txt. "
                "Edit profiles.txt to add the accounts you want to monitor."
            )
        else:
            raise FileNotFoundError(
                "profiles.txt not found and profiles.example.txt is missing."
            )

def read_profiles(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    profiles = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        profiles.append(line)
    return profiles


def extract_username(profile_url: str) -> str:
    parsed = urlparse(profile_url)
    path = parsed.path.strip("/")
    if not path:
        raise ValueError(f"Could not extract username from URL: {profile_url}")
    return path.split("/")[0]


def fetch_profile_json(
    username: str, session: requests.Session | None = None, use_timeouts: bool = True
) -> dict[str, Any]:
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    request_timeout = INSTAGRAM_REQUEST_TIMEOUT_SECONDS if use_timeouts else None
    if session is None:
        response = requests.get(url, headers=INSTAGRAM_REQUEST_HEADERS, timeout=request_timeout)
    else:
        response = session.get(url, timeout=request_timeout)
    response.raise_for_status()
    return response.json()


def make_instagram_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(INSTAGRAM_REQUEST_HEADERS)
    return session


def _cache_file_for_username(username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
    return INSTAGRAM_CACHE_DIR / f"{safe_username}.json"


def _extraction_cache_file_for_username(username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
    return INSTAGRAM_EXTRACTION_CACHE_DIR / f"{safe_username}.json"


def _write_profile_cache(username: str, profile_json: dict[str, Any]) -> None:
    INSTAGRAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "username": username,
        "fetched_at": int(time.time()),
        "profile_json": profile_json,
    }
    _cache_file_for_username(username).write_text(
        json.dumps(cache_payload), encoding="utf-8"
    )


def _read_profile_cache(
    username: str, *, max_age_seconds: int
) -> dict[str, Any] | None:
    cache_file = _cache_file_for_username(username)
    if not cache_file.exists():
        return None

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        fetched_at = int(payload.get("fetched_at", 0))
        profile_json = payload.get("profile_json")
    except Exception:
        return None

    if not isinstance(profile_json, dict):
        return None

    age_seconds = int(time.time()) - fetched_at
    if age_seconds > max_age_seconds:
        return None
    return profile_json


def prune_old_profile_cache_files(max_age_seconds: int) -> int:
    if not INSTAGRAM_CACHE_DIR.exists():
        return 0

    deleted = 0
    now = int(time.time())
    for cache_file in INSTAGRAM_CACHE_DIR.glob("*.json"):
        should_delete = False
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            fetched_at = int(payload.get("fetched_at", 0))
            if fetched_at <= 0:
                fetched_at = int(cache_file.stat().st_mtime)
            should_delete = (now - fetched_at) > max_age_seconds
        except Exception:
            should_delete = True

        if should_delete:
            try:
                cache_file.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted


def prune_old_extraction_cache_files(max_age_seconds: int) -> int:
    if not INSTAGRAM_EXTRACTION_CACHE_DIR.exists():
        return 0

    deleted = 0
    now = int(time.time())
    for cache_file in INSTAGRAM_EXTRACTION_CACHE_DIR.glob("*.json"):
        should_delete = False
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            extracted_at = int(payload.get("extracted_at", 0))
            if extracted_at <= 0:
                extracted_at = int(cache_file.stat().st_mtime)
            should_delete = (now - extracted_at) > max_age_seconds
        except Exception:
            should_delete = True

        if should_delete:
            try:
                cache_file.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted


def rotate_profiles_for_fetch_priority(
    profile_urls: list[str], *, verbose: bool = True
) -> list[str]:
    if not profile_urls:
        return profile_urls

    start_index = 0
    start_username = None
    found_no_recent_cache = False

    for idx, profile_url in enumerate(profile_urls):
        try:
            username = extract_username(profile_url)
        except Exception:
            start_index = idx
            found_no_recent_cache = True
            break

        recent_cache = _read_profile_cache(
            username, max_age_seconds=INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS
        )
        if recent_cache is None:
            start_index = idx
            start_username = username
            found_no_recent_cache = True
            break

    if not found_no_recent_cache:
        return profile_urls

    rotated = profile_urls[start_index:] + profile_urls[:start_index]
    if start_username and verbose:
        print(
            f"Fetch priority rotated: starting at @{start_username} (no cache from last 24 hours).",
            file=sys.stderr,
        )
    return rotated


def extract_recent_posts(profile_json: dict[str, Any], username: str, lookback_days: int) -> list[Post]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_ts = int(cutoff.timestamp())

    edges = (
        profile_json.get("data", {})
        .get("user", {})
        .get("edge_owner_to_timeline_media", {})
        .get("edges", [])
    )

    posts: list[Post] = []
    for edge in edges:
        node = edge.get("node", {})
        ts = node.get("taken_at_timestamp")
        if not isinstance(ts, int) or ts < cutoff_ts:
            continue

        caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
        caption = ""
        if caption_edges:
            caption = caption_edges[0].get("node", {}).get("text", "") or ""

        shortcode = node.get("shortcode", "")
        if not shortcode:
            continue

        posts.append(
            Post(
                username=username,
                taken_at_timestamp=ts,
                caption=caption.strip(),
                shortcode=shortcode,
            )
        )

    posts.sort(key=lambda p: p.taken_at_timestamp)
    return posts


def _trim_caption(caption: str) -> str:
    text = caption or "[No caption]"
    if len(text) > MAX_PROMPT_CAPTION_CHARS:
        return text[:MAX_PROMPT_CAPTION_CHARS].rstrip() + "..."
    return text


def _trim_quote(text: str, max_words: int = 20) -> str:
    words = (text or "").split()
    if not words:
        return ""
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip() + "..."


def _clean_json_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _clean_json_text(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = cleaned[start : end + 1]
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _build_account_extraction_prompt(username: str, posts: list[Post], lookback_days: int) -> str:
    lines: list[str] = []
    for post in posts[-MAX_PROMPT_POSTS_PER_ACCOUNT:]:
        lines.append(f"DATE: {post.taken_at_iso}")
        lines.append(f"POST URL: {post.post_url}")
        lines.append(f"CAPTION: {_trim_caption(post.caption)}")
        lines.append("---")
    body = "\n".join(lines)
    return f"""
Extract structured updates from this single Instagram account.
Only use facts present in the captions below.
Do not invent events, dates, locations, or offerings.
Prioritize concrete updates (events, deadlines, announcements, new offerings).

Return valid JSON only with this exact schema:
{{
  "account": "@{username}",
  "items": [
    {{
      "kind": "event|announcement|new_offering|other",
      "summary": "concise factual summary",
      "date_text": "exact date/time text if present, otherwise empty string",
      "location": "location text if present, otherwise empty string",
      "source_post_url": "instagram post url",
      "quote": "short direct caption quote (max 20 words) that captures the account voice, or empty string",
      "confidence": "high|medium|low"
    }}
  ]
}}

If no useful items are present, return:
{{"account":"@{username}","items":[]}}

Time window: last {lookback_days} days
Account data:
{body}
""".strip()


def _fallback_extract_items(posts: list[Post]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for post in posts[-MAX_PROMPT_POSTS_PER_ACCOUNT:]:
        caption = _trim_caption(post.caption)
        if caption == "[No caption]":
            continue
        quote = caption
        quote_words = quote.split()
        if len(quote_words) > 20:
            quote = " ".join(quote_words[:20]).rstrip() + "..."
        items.append(
            {
                "kind": "other",
                "summary": caption,
                "date_text": post.taken_at_iso,
                "location": "",
                "source_post_url": post.post_url,
                "quote": quote,
                "confidence": "low",
            }
        )
    return items


def _normalize_extracted_items(raw_items: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not isinstance(raw_items, list):
        return items

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary", "")).strip()
        source_post_url = str(item.get("source_post_url", "")).strip()
        if not summary or not source_post_url:
            continue
        items.append(
            {
                "kind": str(item.get("kind", "other")).strip() or "other",
                "summary": summary,
                "date_text": str(item.get("date_text", "")).strip(),
                "location": str(item.get("location", "")).strip(),
                "source_post_url": source_post_url,
                "quote": _trim_quote(str(item.get("quote", "")).strip(), 20),
                "confidence": str(item.get("confidence", "medium")).strip() or "medium",
            }
        )
    return items


def _extraction_signature_for_posts(posts: list[Post]) -> str:
    selected = posts[-MAX_PROMPT_POSTS_PER_ACCOUNT:]
    signature_payload = [
        {
            "shortcode": post.shortcode,
            "taken_at_timestamp": post.taken_at_timestamp,
            "caption": _trim_caption(post.caption),
        }
        for post in selected
    ]
    raw = json.dumps(signature_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_account_extraction_cache(
    username: str, posts: list[Post], model: str
) -> tuple[list[dict[str, str]] | None, str]:
    cache_file = _extraction_cache_file_for_username(username)
    if not cache_file.exists():
        return None, "no_file"
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_json"

    if not isinstance(payload, dict):
        return None, "invalid_payload"
    if str(payload.get("model", "")) != model:
        return None, "model_mismatch"
    if str(payload.get("signature", "")) != _extraction_signature_for_posts(posts):
        return None, "signature_mismatch"

    items = _normalize_extracted_items(payload.get("items"))
    if not items and payload.get("items") != []:
        return None, "invalid_items"
    return items, "hit"


def _write_account_extraction_cache(
    username: str, posts: list[Post], model: str, items: list[dict[str, str]]
) -> None:
    try:
        INSTAGRAM_EXTRACTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "username": username,
            "model": model,
            "signature": _extraction_signature_for_posts(posts),
            "extracted_at": int(time.time()),
            "items": items,
        }
        cache_file = _extraction_cache_file_for_username(username)
        serialized = json.dumps(payload, ensure_ascii=False)

        # Direct per-account write: completed accounts persist; interrupted writes only affect that account.
        with cache_file.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        return


def _extract_account_facts(
    posts_by_user: dict[str, list[Post]],
    lookback_days: int,
    use_timeouts: bool,
    verbose: bool,
) -> dict[str, list[dict[str, str]]]:
    facts_by_user: dict[str, list[dict[str, str]]] = {}
    account_usernames = [u for u, posts in posts_by_user.items() if posts]
    cached_extraction_accounts = 0
    cache_miss_reasons: dict[str, int] = {
        "no_file": 0,
        "invalid_json": 0,
        "invalid_payload": 0,
        "model_mismatch": 0,
        "signature_mismatch": 0,
        "invalid_items": 0,
    }
    print(
        f"Phase 1/2: extracting structured updates for {len(account_usernames)} account(s).",
        file=sys.stderr,
    )
    for username in account_usernames:
        posts = posts_by_user[username]
        cached_items, cache_status = _read_account_extraction_cache(
            username, posts, NEWSLETTER_OLLAMA_MODEL
        )
        if cached_items is not None:
            facts_by_user[username] = cached_items
            cached_extraction_accounts += 1
            if verbose:
                print(
                    f"Using cached Phase 1 extraction for @{username}: {len(cached_items)} item(s)",
                    file=sys.stderr,
                )
            continue
        if cache_status in cache_miss_reasons:
            cache_miss_reasons[cache_status] += 1
        if verbose:
            print(
                f"Phase 1 cache miss for @{username}: {cache_status}",
                file=sys.stderr,
            )

        extraction_prompt = _build_account_extraction_prompt(username, posts, lookback_days)
        try:
            raw = ask_ollama(
                extraction_prompt,
                model=NEWSLETTER_OLLAMA_MODEL,
                task_label=f"account extraction @{username}",
                use_timeouts=use_timeouts,
                verbose=verbose,
            )
            parsed = _extract_json_object(raw)
            if not parsed:
                raise ValueError("No parseable JSON object returned")

            items = _normalize_extracted_items(parsed.get("items", []))
            if items:
                facts_by_user[username] = items
                _write_account_extraction_cache(
                    username, posts, NEWSLETTER_OLLAMA_MODEL, items
                )
            else:
                # Do not cache fallback output.
                facts_by_user[username] = _fallback_extract_items(posts)
        except Exception as e:
            print(
                f"Account extraction fallback used for @{username}: {e}",
                file=sys.stderr,
            )
            facts_by_user[username] = _fallback_extract_items(posts)
    print(
        f"Phase 1 cache used for {cached_extraction_accounts} account(s).",
        file=sys.stderr,
    )
    total_misses = len(account_usernames) - cached_extraction_accounts
    miss_parts = [f"{k}={v}" for k, v in cache_miss_reasons.items() if v > 0]
    if miss_parts:
        print(
            "Phase 1 cache miss reasons: "
            f"{total_misses} miss(es) ({', '.join(miss_parts)}).",
            file=sys.stderr,
        )
    return facts_by_user


def build_newsletter_prompt_from_facts(
    facts_by_user: dict[str, list[dict[str, str]]]
) -> str:
    lines: list[str] = []
    total_items = 0
    for username, items in facts_by_user.items():
        if not items:
            continue
        lines.append(f"ACCOUNT: @{username}")
        for item in items:
            total_items += 1
            lines.append(f"KIND: {item['kind']}")
            lines.append(f"SUMMARY: {item['summary']}")
            lines.append(f"DATE_TEXT: {item['date_text']}")
            lines.append(f"LOCATION: {item['location']}")
            lines.append(f"SOURCE_POST_URL: {item['source_post_url']}")
            lines.append(f"QUOTE: {item.get('quote', '')}")
            lines.append(f"CONFIDENCE: {item['confidence']}")
            lines.append("---")

    if total_items == 0:
        return "No extracted updates were found. Reply with a short note saying there were no recent updates."

    accounts_with_nonzero_posts = sum(1 for items in facts_by_user.values() if items)
    min_additional_sections = accounts_with_nonzero_posts // 4
    today_local = datetime.now().astimezone().date().isoformat()

    facts_block = "\n".join(lines)
    return f"""
You are creating a short newsletter-style summary from extracted Instagram updates.
Use only the extracted facts below.
Do not invent products, events, dates, or claims.
Prefer concrete updates over vague statements.
Prioritize local/community events and new offerings.
Prioritize updates with dates, deadlines, event times, or time windows.
Do not use boilerplate phrases like "Key takeaway".
Do not add generic restatements.
Use quote snippets only if they add voice.
Any quote must come from QUOTE lines in the extracted updates.
Today's date (local): {today_local}
Minimum additional section count: {min_additional_sections} (computed as floor({accounts_with_nonzero_posts} / 4))

Return exactly:
1. A title line
2. Section heading: Upcoming
3. 2 to 6 bullets under Upcoming, containing only updates with date/time that are today ({today_local}) or in the future
4. At least {min_additional_sections} additional themed sections after Upcoming, each with a heading and 2 to 5 bullets
5. A short closing paragraph (3-5 sentences) that ties together the main trends

Bullet format requirement:
- concise factual update that names at least one specific account in parentheses, e.g. (@account)
- include date text when available

Length target:
- around {NEWSLETTER_TARGET_WORDS} words total
- at least 8 bullets overall across all sections

Extracted updates:
{facts_block}
""".strip()


def _clean_poem_output(raw_text: str) -> str:
    cleaned_lines: list[str] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("here's", "summary", "key themes", "notable highlights", "---", "###")):
            continue
        if line.startswith(("-", "*")) and ":" in line:
            continue
        cleaned_lines.append(line)
        if len(cleaned_lines) >= POEM_MAX_LINES:
            break

    if not cleaned_lines:
        return "No poem generated from current updates."
    return "\n".join(cleaned_lines)


def build_poem_prompt(newsletter_text: str, poet_style: str) -> str:
    source_text = newsletter_text.strip() or "No recent updates were found."
    return f"""
Write a short poem based only on this newsletter content.
Prioritize atypical, surprising, or unusual news/content over routine updates from the newsletter.
Keep it to no more than {POEM_MAX_LINES} lines.
Use a light style inspired by {poet_style} (without quoting or imitating exact copyrighted text).
Do not invent facts that are not present in the provided newsletter.
Output only the poem lines.
Do not include headings, bullets, labels, analysis, or any explanatory text.

Newsletter content:
{source_text}
""".strip()


def ask_ollama(
    prompt: str,
    model: str = NEWSLETTER_OLLAMA_MODEL,
    task_label: str = "newsletter",
    use_timeouts: bool = True,
    estimated_output_tokens: int | None = None,
    verbose: bool = False,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "stream": False,
        "keep_alive": "10m",
    }

    prompt_chars = len(prompt)
    prompt_tokens_estimate = max(1, prompt_chars // 4)
    output_tokens_estimate = (
        estimated_output_tokens
        if estimated_output_tokens is not None and estimated_output_tokens > 0
        else prompt_tokens_estimate
    )
    estimated_total_tokens = prompt_tokens_estimate + output_tokens_estimate
    model_context_length, used_cached_context = _get_model_context_length(
        model, use_timeouts=use_timeouts
    )
    if model_context_length:
        warning_context_length = model_context_length
        context_usage_ratio = estimated_total_tokens / warning_context_length
        warning_needed = (
            context_usage_ratio >= OLLAMA_CONTEXT_WARNING_RATIO
            or estimated_total_tokens > warning_context_length
        )

        # If warning is based on cached model info, re-check live model info first.
        if warning_needed and used_cached_context:
            live_context_length, _ = _get_model_context_length(
                model, use_timeouts=use_timeouts, force_refresh=True
            )
            if live_context_length:
                warning_context_length = live_context_length
                context_usage_ratio = estimated_total_tokens / warning_context_length

        if context_usage_ratio >= OLLAMA_CONTEXT_WARNING_RATIO:
            print(
                "Warning: estimated Ollama context usage is high for this request "
                f"({estimated_total_tokens}/{warning_context_length} tokens; "
                f"prompt~{prompt_tokens_estimate}, output~{output_tokens_estimate}). "
                "The model may truncate earlier instructions or source data.",
                file=sys.stderr,
            )
        if estimated_total_tokens > warning_context_length:
            print(
                "Warning: estimated prompt + output reserve exceeds model context length "
                f"({estimated_total_tokens}>{warning_context_length}). Truncation is likely.",
                file=sys.stderr,
            )
    request_timeout = OLLAMA_TIMEOUT_SECONDS if use_timeouts else None
    stop_progress = threading.Event()

    def _progress_printer() -> None:
        if not verbose and task_label.startswith("account extraction @"):
            line = f"[{model}] Generating {task_label}"
        else:
            line = (
                f"Generating {task_label} ({prompt_chars} char, ~{prompt_tokens_estimate} token est.) "
                f"with Ollama model '{model}'"
            )
        if verbose:
            print(line, end="", file=sys.stderr, flush=True)
            while not stop_progress.wait(1.0):
                print(".", end="", file=sys.stderr, flush=True)
            return

        frame_index = 0
        print(f"{line} {STAR_SPINNER_FRAMES[frame_index]}", end="", file=sys.stderr, flush=True)
        while not stop_progress.wait(0.2):
            frame_index = (frame_index + 1) % len(STAR_SPINNER_FRAMES)
            print(
                f"\r{line} {STAR_SPINNER_FRAMES[frame_index]}",
                end="",
                file=sys.stderr,
                flush=True,
            )

    progress_thread = threading.Thread(target=_progress_printer, daemon=True)
    progress_thread.start()
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=request_timeout)
        response.raise_for_status()
        data = response.json()
        return data["message"]["content"]
    except requests.Timeout as e:
        raise RuntimeError(
            f"Ollama timed out while generating {task_label}. "
            "Try again with --no-timeouts or reduce prompt size."
        ) from e
    finally:
        stop_progress.set()
        progress_thread.join(timeout=1.0)
        print("", file=sys.stderr)


def _maybe_parse_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            v = int(value)
            return v if v > 0 else None
        if isinstance(value, str):
            m = re.search(r"([0-9][0-9,]*)", value)
            if not m:
                return None
            v = int(m.group(1).replace(",", ""))
            return v if v > 0 else None
    except Exception:
        return None
    return None


def _extract_context_length_from_show_payload(payload: dict[str, Any]) -> int | None:
    # Prefer explicit model_info keys like "qwen3.context_length" or "llama.context_length".
    model_info = payload.get("model_info")
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if "context_length" in str(key).lower():
                parsed = _maybe_parse_int(value)
                if parsed is not None:
                    return parsed

    # Fallback: scan top-level keys that contain context length fields.
    for key in ("context_length", "num_ctx"):
        if key in payload:
            parsed = _maybe_parse_int(payload.get(key))
            if parsed is not None:
                return parsed

    # Last resort: scan string fields (e.g., parameters blobs) for context length hints.
    for key in ("parameters", "modelfile", "template"):
        value = payload.get(key)
        if isinstance(value, str) and "context" in value.lower():
            parsed = _maybe_parse_int(value)
            if parsed is not None:
                return parsed

    return None


def _read_ollama_model_info_cache() -> dict[str, dict[str, int]]:
    if not OLLAMA_MODEL_INFO_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(OLLAMA_MODEL_INFO_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        context_length = _maybe_parse_int(value.get("context_length"))
        fetched_at = _maybe_parse_int(value.get("fetched_at"))
        if context_length is None or fetched_at is None:
            continue
        normalized[key] = {"context_length": context_length, "fetched_at": fetched_at}
    return normalized


def _write_ollama_model_info_cache(cache_payload: dict[str, dict[str, int]]) -> None:
    try:
        OLLAMA_MODEL_INFO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OLLAMA_MODEL_INFO_CACHE_FILE.write_text(
            json.dumps(cache_payload, indent=2), encoding="utf-8"
        )
    except Exception:
        return


def _fetch_live_model_context_length(
    model: str, use_timeouts: bool = True
) -> int | None:
    request_timeout = INSTAGRAM_REQUEST_TIMEOUT_SECONDS if use_timeouts else None

    try:
        response = requests.post(
            OLLAMA_SHOW_URL,
            json={"model": model},
            timeout=request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        context_length = _extract_context_length_from_show_payload(payload)
        return context_length
    except Exception:
        return None


def _get_model_context_length(
    model: str, use_timeouts: bool = True, force_refresh: bool = False
) -> tuple[int | None, bool]:
    if not force_refresh and model in _MODEL_CONTEXT_LENGTH_CACHE:
        return _MODEL_CONTEXT_LENGTH_CACHE[model], True

    now = int(time.time())
    if not force_refresh:
        disk_cache = _read_ollama_model_info_cache()
        cached = disk_cache.get(model)
        if cached:
            fetched_at = cached["fetched_at"]
            if (now - fetched_at) <= OLLAMA_MODEL_INFO_CACHE_TTL_SECONDS:
                context_length = cached["context_length"]
                _MODEL_CONTEXT_LENGTH_CACHE[model] = context_length
                return context_length, True

    context_length = _fetch_live_model_context_length(model, use_timeouts=use_timeouts)
    _MODEL_CONTEXT_LENGTH_CACHE[model] = context_length

    if context_length is not None:
        disk_cache = _read_ollama_model_info_cache()
        disk_cache[model] = {"context_length": context_length, "fetched_at": now}
        _write_ollama_model_info_cache(disk_cache)
    return context_length, False


def generate_newsletter(
    use_timeouts: bool = True,
    verbose: bool = False,
) -> tuple[str, float, int, int, str, str, str]:
    ensure_profiles_file()
    pruned_cache_files = prune_old_profile_cache_files(
        INSTAGRAM_CACHE_MAX_AGE_SECONDS
    )
    pruned_extraction_cache_files = prune_old_extraction_cache_files(
        INSTAGRAM_CACHE_MAX_AGE_SECONDS
    )
    if pruned_cache_files:
        print(f"Pruned {pruned_cache_files} old Instagram cache file(s).", file=sys.stderr)
    if pruned_extraction_cache_files:
        print(
            f"Pruned {pruned_extraction_cache_files} old extraction cache file(s).",
            file=sys.stderr,
        )

    start_time = time.time()
    try:
        profile_urls = read_profiles(PROFILES_FILE)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Missing {PROFILES_FILE}. Create it with one Instagram profile URL per line."
        )

    if not profile_urls:
        raise ValueError(f"{PROFILES_FILE} is empty.")

    prioritized_profile_urls = rotate_profiles_for_fetch_priority(
        profile_urls, verbose=verbose
    )
    accounts_checked = len(profile_urls) 
    
    posts_by_user: dict[str, list[Post]] = {}
    live_data_accounts: set[str] = set()
    cached_data_accounts: set[str] = set()

    consecutive_401_errors = 0
    reduced_data_mode = False
    fresh_cache_by_username: dict[str, dict[str, Any]] = {}
    for profile_url in profile_urls:
        try:
            cached_username = extract_username(profile_url)
        except Exception:
            continue
        cached_profile_json = _read_profile_cache(
            cached_username, max_age_seconds=INSTAGRAM_CACHE_TTL_SECONDS
        )
        if cached_profile_json is not None:
            fresh_cache_by_username[cached_username] = cached_profile_json
   
    live_fetch_progress_started = False
    live_fetch_progress_line_open = False

    def _start_or_tick_live_fetch_progress() -> None:
        nonlocal live_fetch_progress_started, live_fetch_progress_line_open
        if verbose:
            return
        if not live_fetch_progress_line_open:
            print(
                "Fetching live Instagram data",
                end="",
                file=sys.stderr,
                flush=True,
            )
            live_fetch_progress_started = True
            live_fetch_progress_line_open = True
        print(".", end="", file=sys.stderr, flush=True)

    def _flush_live_fetch_progress_line() -> None:
        nonlocal live_fetch_progress_line_open
        if verbose:
            return
        if live_fetch_progress_line_open:
            print("", file=sys.stderr)
            live_fetch_progress_line_open = False

    with make_instagram_session() as instagram_session:
        for profile_url in prioritized_profile_urls:
            username: str | None = None
            try:
                username = extract_username(profile_url)

                if reduced_data_mode:
                    profile_json = fresh_cache_by_username.get(username)
                    if profile_json is not None:
                        posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
                        posts_by_user[username] = posts
                        cached_data_accounts.add(username)
                        if verbose:
                            print(
                                f"Using cached @{username} profile data: {len(posts)} recent post(s)",
                                file=sys.stderr,
                            )
                    else:
                        if verbose:
                            print(
                                f"No fresh cached data available for @{username}; skipping in reduced-data mode.",
                                file=sys.stderr,
                            )
                    continue

                profile_json = fresh_cache_by_username.get(username)
                if profile_json is not None:
                    posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
                    posts_by_user[username] = posts
                    cached_data_accounts.add(username)
                    if verbose:
                        print(
                            f"Using cached @{username} profile data: {len(posts)} recent post(s)",
                            file=sys.stderr,
                        )
                    consecutive_401_errors = 0
                    continue

                _start_or_tick_live_fetch_progress()
                profile_json = fetch_profile_json(
                    username, session=instagram_session, use_timeouts=use_timeouts
                )
                _write_profile_cache(username, profile_json)
                posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
                posts_by_user[username] = posts
                live_data_accounts.add(username)

                if verbose:
                    print(f"Fetched @{username}: {len(posts)} recent post(s)", file=sys.stderr)

                # reset counter on success
                consecutive_401_errors = 0
                time.sleep(random.uniform(0.5, 1.25)) # wait a bit to not hammer w requests

            except requests.Timeout:
                _flush_live_fetch_progress_line()
                print(
                    f"Instagram request timed out for {profile_url}. Skipping this profile for now.",
                    file=sys.stderr,
                )
            except requests.HTTPError as e:
                _flush_live_fetch_progress_line()
                if e.response.status_code == 401:
                    consecutive_401_errors += 1
                    print(f"401 error while fetching {profile_url}", file=sys.stderr)

                    stale_profile_json = _read_profile_cache(
                        username, max_age_seconds=INSTAGRAM_CACHE_MAX_AGE_SECONDS
                    )
                    if stale_profile_json is not None:
                        posts = extract_recent_posts(stale_profile_json, username, LOOKBACK_DAYS)
                        posts_by_user[username] = posts
                        cached_data_accounts.add(username)
                        consecutive_401_errors = 0
                        if verbose:
                            print(
                                f"Using stale cached @{username} data due to Instagram 401: "
                                f"{len(posts)} recent post(s)",
                                file=sys.stderr,
                            )
                        continue

                    if consecutive_401_errors >= 3:
                        profiles_collected_so_far = len(posts_by_user)
                        potential_profiles_available = len(
                            set(posts_by_user.keys()) | set(fresh_cache_by_username.keys())
                        )
                        if potential_profiles_available >= MIN_CACHED_PROFILES_FOR_REDUCED_MODE:
                            print(
                                "Multiple Instagram 401 errors detected. Proceeding with "
                                "newsletter generation because enough profile data has already been collected.",
                                file=sys.stderr,
                            )
                            print(
                                f"Continuing with reduced data: collected data for "
                                f"{profiles_collected_so_far} profile(s), with up to "
                                f"{potential_profiles_available} profile(s) available including fresh cache; "
                                "live fetches were interrupted by 401 errors.",
                                file=sys.stderr,
                            )
                            reduced_data_mode = True
                            continue
                        raise RuntimeError(
                            "Multiple authentication errors detected while contacting Instagram. "
                            "Instagram may be rate limiting requests from your IP address. "
                            "Please wait a few minutes before running the script again."
                        )

                else:
                    print(f"HTTP error for {profile_url}: {e}", file=sys.stderr)

            except Exception as e:
                _flush_live_fetch_progress_line()
                print(f"Error for {profile_url}: {e}", file=sys.stderr)

    if live_fetch_progress_started and not verbose:
        _flush_live_fetch_progress_line()


    accounts_with_posts = sum(1 for posts in posts_by_user.values() if posts)
    if not verbose:
        print(
            "\nCollected Instagram data for "
            f"{len(posts_by_user)} account(s): "
            f"{accounts_with_posts} with recent posts, "
            f"{len(live_data_accounts)} fresh, "
            f"{len(cached_data_accounts)} cached.",
            file=sys.stderr,
        )
    facts_by_user = _extract_account_facts(
        posts_by_user,
        LOOKBACK_DAYS,
        use_timeouts=use_timeouts,
        verbose=verbose,
    )
    total_extracted_items = sum(len(items) for items in facts_by_user.values())
    if verbose:
        print("\nPhase 1 extracted facts (JSON):", file=sys.stderr)
        print(json.dumps(facts_by_user, indent=2, ensure_ascii=False), file=sys.stderr)
    else:
        print(
            f"\nPhase 1 extraction complete: {len(facts_by_user)} account(s), "
            f"{total_extracted_items} extracted item(s).",
            file=sys.stderr,
        )
    print("\nPhase 2/2: generating newsletter from extracted account facts.", file=sys.stderr)
    prompt = build_newsletter_prompt_from_facts(facts_by_user)
    summary = ask_ollama(
        prompt,
        model=NEWSLETTER_OLLAMA_MODEL,
        task_label="newsletter",
        use_timeouts=use_timeouts,
        estimated_output_tokens=max(1, round(NEWSLETTER_TARGET_WORDS * TOKENS_PER_WORD_ESTIMATE)),
        verbose=verbose,
    )
    selected_poet_style = secrets.choice(POEM_STYLES)
    if verbose:
        print(
            f"Poem style selected for this run: {selected_poet_style}",
            file=sys.stderr,
        )
    poem_prompt = build_poem_prompt(summary, selected_poet_style)
    poem_raw = ask_ollama(
        poem_prompt,
        model=POEM_OLLAMA_MODEL,
        task_label="poem",
        use_timeouts=use_timeouts,
        verbose=verbose,
    )
    poem = _clean_poem_output(poem_raw)
    summary = (
        f"{summary}\n\nPOEM ({selected_poet_style} inspired):\n{poem}"
    )

    end_time = time.time()
    elapsed = end_time - start_time
    return (
        summary,
        elapsed,
        accounts_checked,
        accounts_with_posts,
        NEWSLETTER_OLLAMA_MODEL,
        POEM_OLLAMA_MODEL,
        selected_poet_style,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an Instagram newsletter.")
    parser.add_argument(
        "--no-timeouts",
        action="store_true",
        help="Disable network timeouts for Instagram and Ollama requests.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose logs, including full Phase 1 extracted JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
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
            use_timeouts=not args.no_timeouts,
            verbose=args.verbose,
        )
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    print(summary)

    print("\n---")
    print(f" Generated in {elapsed:.2f} seconds")
    print(f" Newsletter model used: {model_used}")
    print(f" Poem model used: {poem_model_used}")
    print(f" Poet inspiration: {poet_style}")
    print(f" Accounts checked: {accounts_checked}")
    print(f" Accounts with recent posts: {accounts_with_posts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
