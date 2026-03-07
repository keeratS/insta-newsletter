#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


INSTAGRAM_APP_ID = "936619743392459"
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"
LOOKBACK_DAYS = 3
PROFILES_FILE = "profiles.txt"


@dataclass
class Post:
    username: str
    taken_at_timestamp: int
    caption: str
    shortcode: str

    @property
    def post_url(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/"

    @property
    def taken_at_iso(self) -> str:
        return datetime.fromtimestamp(
            self.taken_at_timestamp, tz=timezone.utc
        ).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def read_profiles(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    profiles = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        profiles.append(line)
    return profiles


def extract_username(profile_url: str) -> str:
    parsed = urlparse(profile_url)
    path = parsed.path.strip("/")
    if not path:
        raise ValueError(f"Could not extract username from URL: {profile_url}")
    return path.split("/")[0]


def fetch_profile_json(username: str) -> dict[str, Any]:
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "x-ig-app-id": INSTAGRAM_APP_ID,
    }
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


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


def build_prompt(posts_by_user: dict[str, list[Post]], lookback_days: int) -> str:
    sections: list[str] = []
    total_posts = 0

    for username, posts in posts_by_user.items():
        if not posts:
            continue

        total_posts += len(posts)
        section_lines = [f"PROFILE: @{username}"]
        for post in posts:
            section_lines.append(f"DATE: {post.taken_at_iso}")
            section_lines.append(f"POST URL: {post.post_url}")
            section_lines.append(f"CAPTION: {post.caption or '[No caption]'}")
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

Return exactly:
1. A title line
2. 3 to 6 bullet points
3. A short newsletter paragraph

Time window: last {lookback_days} days

Instagram data:
{body}
""".strip()


def ask_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
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

    response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    response.raise_for_status()
    data = response.json()
    return data["message"]["content"]


def main() -> int:
    start_time=time.time()
    try:
        profile_urls = read_profiles(PROFILES_FILE)
    except FileNotFoundError:
        print(f"Missing {PROFILES_FILE}. Create it with one Instagram profile URL per line.", file=sys.stderr)
        return 1

    if not profile_urls:
        print(f"{PROFILES_FILE} is empty.", file=sys.stderr)
        return 1

    accounts_checked = len(profile_urls) 
    
    posts_by_user: dict[str, list[Post]] = {}

    consecutive_401_errors = 0
   
    for profile_url in profile_urls:
        try:
            username = extract_username(profile_url)
            profile_json = fetch_profile_json(username)
            posts = extract_recent_posts(profile_json, username, LOOKBACK_DAYS)
            posts_by_user[username] = posts

            print(f"Fetched @{username}: {len(posts)} recent post(s)", file=sys.stderr)

            # reset counter on success
            consecutive_401_errors = 0
            time.sleep(random.uniform(0.5, 1.25)) # wait a bit to not hammer w requests

        except requests.HTTPError as e:
            if e.response.status_code == 401:
                consecutive_401_errors += 1
                print(f"401 error while fetching {profile_url}", file=sys.stderr)

                if consecutive_401_errors >= 3:
                    print(
                        "\nFYI: Multiple authentication errors detected while contacting Instagram.\n"
                        "Instagram may be rate limiting requests from your IP address.\n"
                        "Please wait a few minutes before running the script again.\n",
                        file=sys.stderr,
                    )
                    return 1 # do not continue to model

            else:
                print(f"HTTP error for {profile_url}: {e}", file=sys.stderr)

        except Exception as e:
            print(f"Error for {profile_url}: {e}", file=sys.stderr)


    accounts_with_posts = sum(1 for posts in posts_by_user.values() if posts)

    prompt = build_prompt(posts_by_user, LOOKBACK_DAYS)
    summary = ask_ollama(prompt, model=OLLAMA_MODEL)

    end_time = time.time()
    elapsed = end_time - start_time
    runtime = f"{elapsed:.2f}"

    print(summary)
    
    print(summary)

    print("\n---")
    print(f" Generated in {elapsed:.2f} seconds")
    print(f" Accounts checked: {accounts_checked}")
    print(f" Accounts with recent posts: {accounts_with_posts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
