#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
NEWSLETTER_OLLAMA_MODEL = "qwen3:8b"
POEM_OLLAMA_MODEL = "qwen3:8b"
OLLAMA_TIMEOUT_SECONDS = 600
INSTAGRAM_REQUEST_TIMEOUT_SECONDS = 60
LOOKBACK_DAYS = 3
PROFILES_FILE = "profiles.txt"
INSTAGRAM_CACHE_DIR = Path(".cache/instagram_profiles")
INSTAGRAM_CACHE_TTL_SECONDS = 24 * 60 * 60
INSTAGRAM_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS = 24 * 60 * 60
MIN_CACHED_PROFILES_FOR_REDUCED_MODE = 11
POEM_MAX_LINES = 7
MAX_PROMPT_POSTS_PER_ACCOUNT = 2
MAX_PROMPT_POSTS_TOTAL = 40
MAX_PROMPT_CAPTION_CHARS = 280
POEM_STYLES = [
    "Rumi",
    "Mary Oliver",
    "Shakespeare",
    "Robert Frost",
    "Ada Limon",
    "Emily Dickinson",
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


def rotate_profiles_for_fetch_priority(profile_urls: list[str]) -> list[str]:
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
    if start_username:
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


def build_prompt(
    posts_by_user: dict[str, list[Post]], lookback_days: int
) -> str:
    sections: list[str] = []
    selected_posts_by_user: dict[str, list[Post]] = {}
    all_selected_posts: list[Post] = []

    for username, posts in posts_by_user.items():
        if not posts:
            continue
        chosen = posts[-MAX_PROMPT_POSTS_PER_ACCOUNT:]
        selected_posts_by_user[username] = chosen
        all_selected_posts.extend(chosen)

    all_selected_posts.sort(key=lambda p: p.taken_at_timestamp or 0, reverse=True)
    limited_posts = all_selected_posts[:MAX_PROMPT_POSTS_TOTAL]

    limited_by_user: dict[str, list[Post]] = {}
    for post in limited_posts:
        limited_by_user.setdefault(post.username, []).append(post)

    total_posts = len(limited_posts)
    for username, posts in limited_by_user.items():
        section_lines = [f"PROFILE: @{username}"]
        for post in sorted(posts, key=lambda p: p.taken_at_timestamp or 0):
            raw_caption = post.caption or "[No caption]"
            caption = raw_caption
            if len(caption) > MAX_PROMPT_CAPTION_CHARS:
                caption = caption[:MAX_PROMPT_CAPTION_CHARS].rstrip() + "..."
            section_lines.append(f"DATE: {post.taken_at_iso}")
            section_lines.append(f"POST URL: {post.post_url}")
            section_lines.append(f"CAPTION: {caption}")
            section_lines.append("---")
        sections.append("\n".join(section_lines))

    if total_posts == 0:
        return (
            f"No posts were found in the last {lookback_days} days. "
            "Reply with a short note saying there were no recent updates."
        )

    body = "\n\n".join(sections)

    return f"""
You are creating a short newsletter-style summary from recent Instagram captions.

Use only the information provided below.
Do not invent products, events, dates, or claims.
Group related updates together.
Prefer concrete updates over vague marketing language.
Prioritize posts about local/community events and specific happenings (for example events, workshops, talks, exhibits, meetups, deadlines, and announcements with locations/times).
Also prioritize genuinely new offerings (for example new menu items, new products, new services, or newly announced programs).
Prioritize updates that include specific dates, deadlines, event times, or time windows.
In each bullet, explicitly name the specific Instagram account(s) the information came from (for example, "@accountname").
Every bullet must include at least one @account handle.
If attribution is uncertain, write "@unknown" instead of guessing.
Do not merge facts across accounts.
Do not use boilerplate phrases like "Key takeaway".
Do not add generic restatements that repeat the same point with less detail.
Do not write generalizations about an account's overall mission or identity unless that wording appears in the provided captions.

Return exactly:
1. A title line
2. 6 to 12 bullet points
3. A short newsletter paragraph

Bullet format requirement:
- [@account] concise factual update

Time window: last {lookback_days} days

Instagram data:
{body}
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
    print(
        f"Prompt size for Ollama: {prompt_chars} chars (~{prompt_tokens_estimate} tokens estimate)",
        file=sys.stderr,
    )

    request_timeout = OLLAMA_TIMEOUT_SECONDS if use_timeouts else None
    stop_progress = threading.Event()

    def _progress_printer() -> None:
        print(
            f"Generating {task_label} with Ollama model '{model}'",
            end="",
            file=sys.stderr,
            flush=True,
        )
        while not stop_progress.wait(1.0):
            print(".", end="", file=sys.stderr, flush=True)

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


def generate_newsletter(
    use_timeouts: bool = True,
) -> tuple[str, float, int, int, str, str, str]:
    ensure_profiles_file()
    pruned_cache_files = prune_old_profile_cache_files(
        INSTAGRAM_CACHE_MAX_AGE_SECONDS
    )
    if pruned_cache_files:
        print(f"Pruned {pruned_cache_files} old Instagram cache file(s).", file=sys.stderr)

    start_time = time.time()
    try:
        profile_urls = read_profiles(PROFILES_FILE)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Missing {PROFILES_FILE}. Create it with one Instagram profile URL per line."
        )

    if not profile_urls:
        raise ValueError(f"{PROFILES_FILE} is empty.")

    prioritized_profile_urls = rotate_profiles_for_fetch_priority(profile_urls)
    accounts_checked = len(profile_urls) 
    
    posts_by_user: dict[str, list[Post]] = {}

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
                        print(
                            f"Using cached @{username} profile data: {len(posts)} recent post(s)",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"No fresh cached data available for @{username}; skipping in reduced-data mode.",
                            file=sys.stderr,
                        )
                    continue

                profile_json = fresh_cache_by_username.get(username)
                if profile_json is not None:
                    posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
                    posts_by_user[username] = posts
                    print(
                        f"Using cached @{username} profile data: {len(posts)} recent post(s)",
                        file=sys.stderr,
                    )
                    consecutive_401_errors = 0
                    continue

                profile_json = fetch_profile_json(
                    username, session=instagram_session, use_timeouts=use_timeouts
                )
                _write_profile_cache(username, profile_json)
                posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
                posts_by_user[username] = posts

                print(f"Fetched @{username}: {len(posts)} recent post(s)", file=sys.stderr)

                # reset counter on success
                consecutive_401_errors = 0
                time.sleep(random.uniform(0.5, 1.25)) # wait a bit to not hammer w requests

            except requests.Timeout:
                print(
                    f"Instagram request timed out for {profile_url}. Skipping this profile for now.",
                    file=sys.stderr,
                )
            except requests.HTTPError as e:
                if e.response.status_code == 401:
                    consecutive_401_errors += 1
                    print(f"401 error while fetching {profile_url}", file=sys.stderr)

                    stale_profile_json = _read_profile_cache(
                        username, max_age_seconds=INSTAGRAM_CACHE_MAX_AGE_SECONDS
                    )
                    if stale_profile_json is not None:
                        posts = extract_recent_posts(stale_profile_json, username, LOOKBACK_DAYS)
                        posts_by_user[username] = posts
                        consecutive_401_errors = 0
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
                print(f"Error for {profile_url}: {e}", file=sys.stderr)


    accounts_with_posts = sum(1 for posts in posts_by_user.values() if posts)
    prompt = build_prompt(posts_by_user, LOOKBACK_DAYS)
    summary = ask_ollama(
        prompt,
        model=NEWSLETTER_OLLAMA_MODEL,
        task_label="newsletter",
        use_timeouts=use_timeouts,
    )
    selected_poet_style = secrets.choice(POEM_STYLES)
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
        ) = generate_newsletter(use_timeouts=not args.no_timeouts)
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
