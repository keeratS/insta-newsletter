#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import requests

from insta_newsletter import (
    INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS,
    PROFILES_FILE,
    _cache_file_for_username,
    _read_profile_cache,
    _write_profile_cache,
    ensure_profiles_file,
    extract_username,
    fetch_profile_json,
    make_instagram_session,
    read_profiles,
    rotate_profiles_for_fetch_priority,
)


def _cache_fetched_at(username: str) -> int:
    cache_file: Path = _cache_file_for_username(username)
    if not cache_file.exists():
        return 0

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        fetched_at = int(payload.get("fetched_at", 0))
        if fetched_at > 0:
            return fetched_at
    except Exception:
        pass

    try:
        return int(cache_file.stat().st_mtime)
    except OSError:
        return 0


def refresh_instagram_cache(use_timeouts: bool = True) -> int:
    ensure_profiles_file()
    try:
        profile_urls = read_profiles(PROFILES_FILE)
    except FileNotFoundError:
        print(f"Missing {PROFILES_FILE}.", file=sys.stderr)
        return 1

    if not profile_urls:
        print(f"{PROFILES_FILE} is empty.", file=sys.stderr)
        return 1

    prioritized_profile_urls = rotate_profiles_for_fetch_priority(profile_urls)
    recent_cache_by_url: dict[str, bool] = {}
    username_by_url: dict[str, str] = {}
    for profile_url in prioritized_profile_urls:
        try:
            username = extract_username(profile_url)
        except Exception:
            continue
        username_by_url[profile_url] = username
        recent_cache_by_url[profile_url] = (
            _read_profile_cache(
                username, max_age_seconds=INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS
            )
            is not None
        )

    force_refresh_all_oldest_first = (
        len(recent_cache_by_url) == len(prioritized_profile_urls)
        and all(recent_cache_by_url.values())
    )
    if force_refresh_all_oldest_first:
        print(
            "All profiles already have cache from last 24 hours. "
            "Refreshing all profiles starting with the oldest cache entries.",
            file=sys.stderr,
        )
        prioritized_profile_urls = sorted(
            prioritized_profile_urls,
            key=lambda profile_url: _cache_fetched_at(username_by_url[profile_url]),
        )

    fetched = 0
    skipped_recent_cache = 0
    errors = 0
    consecutive_401_errors = 0

    with make_instagram_session() as instagram_session:
        for profile_url in prioritized_profile_urls:
            try:
                username = extract_username(profile_url)
            except Exception as e:
                print(f"Skipping invalid profile URL '{profile_url}': {e}", file=sys.stderr)
                errors += 1
                continue

            recent_cache = _read_profile_cache(
                username,
                max_age_seconds=INSTAGRAM_DEPRIORITIZE_CACHE_AGE_SECONDS,
            )
            if recent_cache is not None and not force_refresh_all_oldest_first:
                skipped_recent_cache += 1
                print(
                    f"Skipping @{username}: cache from last 24 hours already available.",
                    file=sys.stderr,
                )
                continue

            try:
                profile_json = fetch_profile_json(
                    username,
                    session=instagram_session,
                    use_timeouts=use_timeouts,
                )
                _write_profile_cache(username, profile_json)
                fetched += 1
                consecutive_401_errors = 0
                print(f"Updated cache for @{username}", file=sys.stderr)
                time.sleep(random.uniform(0.5, 1.25))
            except requests.Timeout:
                errors += 1
                print(
                    f"Instagram request timed out while refreshing @{username}.",
                    file=sys.stderr,
                )
            except requests.HTTPError as e:
                errors += 1
                if e.response is not None and e.response.status_code == 401:
                    consecutive_401_errors += 1
                    print(f"401 error while refreshing @{username}", file=sys.stderr)
                    if consecutive_401_errors >= 3:
                        print(
                            "Stopping cache refresh after 3 consecutive Instagram 401 errors.",
                            file=sys.stderr,
                        )
                        break
                else:
                    print(f"HTTP error while refreshing @{username}: {e}", file=sys.stderr)
            except Exception as e:
                errors += 1
                print(f"Error while refreshing @{username}: {e}", file=sys.stderr)

    print("\n---")
    print(f"Cache refresh complete")
    print(f"Profiles listed: {len(profile_urls)}")
    print(f"Profiles fetched: {fetched}")
    print(f"Profiles skipped (recent cache): {skipped_recent_cache}")
    print(f"Errors: {errors}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh Instagram profile cache only."
    )
    parser.add_argument(
        "--no-timeouts",
        action="store_true",
        help="Disable network timeouts for Instagram requests.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(refresh_instagram_cache(use_timeouts=not args.no_timeouts))
