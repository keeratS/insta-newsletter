# insta-newsletter

A simple tool to reduce reliance on Instagram for learning about things you care about.

✨🌿☕📸📝🤖📬📝📸☕🌿✨

A small Python tool that monitors Instagram profiles, extracts captions from posts in the last few days, and generates:

- a plaintext newsletter-style summary
- a short follow-up poem inspired by the newsletter content

This project runs locally and can be scheduled with a cron job to generate periodic summaries.

---

## Overview

The main script:

1. Reads a list of Instagram profiles from a file
2. Uses cache-first profile data fetching, with live Instagram fallback
3. Extracts captions from posts within a specified time window
4. Sends selected post content to a local LLM via Ollama
5. Produces a newsletter-style summary plus a short poem
6. Prints runtime stats (model, generation time, account counts)

Example use cases:

- monitoring community organizations
- tracking local businesses or events
- reducing personal reliance on the Instagram app

---

## Example Output

TITLE:
Bay Area Literary & Community Updates

UPDATES:
- Litquake announced submission deadlines for its upcoming festival.
- Lit Crawl SF promoted upcoming literary events and author programming.
- Oaktown Spice Shop highlighted new seasonal spice blends.

NEWSLETTER:
Several Bay Area organizations shared updates this week. Litquake and Lit Crawl SF both announced upcoming programming and submission deadlines for literary events, while local businesses such as Oaktown Spice Shop highlighted new seasonal offerings.

---

## Requirements

- Python 3.9+
- Ollama installed locally
- A local LLM model (for example `llama3.2` or `qwen3`)

Install Ollama:
```
https://ollama.com
```

Pull a model:
```
ollama pull qwen3:8b
```
---

## Installation

### Clone the repository
```
git clone https://github.com/keeratS/insta-newsletter.git
cd insta-newsletter
```
### Create a virtual environment
```
python3 -m venv venv
source venv/bin/activate
```
### Install dependencies
```
pip install requests
```
---

## Configuration

Profiles are stored in a separate file.

Create profiles.txt:
```
https://www.instagram.com/officialelaichico/
https://www.instagram.com/litquake/
```
Add one profile per line.

---

## Usage

Run the script:
```
python insta_newsletter.py
```
This script will:

- fetch recent posts
- extract captions
- send selected content to the LLM
- print a newsletter-style summary
- generate a short poem (max 7 lines), with random inspiration style per run
- print runtime stats (generation time, model used, accounts checked, accounts with recent posts)

Timeout note:

- larger Ollama models can take much longer on some laptops, depending on CPU/GPU/RAM
- if generation runs into timeout errors on your hardware, rerun with:
  - `python insta_newsletter.py --no-timeouts`
  - `python send_readwise_insta_newsletter.py --no-timeouts`

---

## Readwise Integration

Readwise is a read-it-later and resurfacing tool for highlights and saved content.

Set a Readwise token:
```
export READWISE_ACCESS_TOKEN="your_readwise_access_token"
```

Run the wrapper to generate and then send to Readwise:
```
python send_readwise_insta_newsletter.py
```

The wrapper will:

- call `insta_newsletter` newsletter generation
- send the generated summary to Readwise via `POST /api/v3/save/`
- fail if `READWISE_ACCESS_TOKEN` is not set
- include stats/footer metadata in the saved Readwise entry

---

## Caching

Instagram profile responses are cached on disk so the script can make fewer live Instagram requests.
This helps reduce throttling/rate-limit issues.

Cache behavior:

- cache location: `.cache/instagram_profiles/`
- phase-1 extraction cache location: `.cache/instagram_extractions/`
- fresh cache lifetime: 24 hours
- if a live request returns `401`, the script can use stale cache up to 24 hours old for that profile
- cache files older than this window are cleaned up automatically during runs
- phase-1 extraction cache entries are keyed by account, selected posts, and extraction model

Fetch strategy:

- profiles are rotated so live fetching starts at the first profile that does not have cache from the last 24 hours
- if repeated `401` errors occur, the script can continue in reduced-data mode using available cache (instead of failing immediately)

Manual reset:

- delete `.cache/instagram_profiles` to clear cache manually
- delete `.cache/instagram_extractions` to clear phase-1 extraction cache manually
- if you change extraction prompt or extraction JSON format, clear both cache folders before running again
- the folder is recreated automatically on the next run

---

## Running Automatically

You can run the scripts periodically using cron.

Example newsletter run (every other day at 7:30am):
```
30 7 */2 * * cd /path/to/project && ./venv/bin/python send_readwise_insta_newsletter.py
```

To refresh only Instagram caches (no newsletter generation), run:
```
python refresh_instagram_cache.py
```

Example cron (twice daily cache refresh at 7:00 and 19:00):
```
0 7,19 * * * cd /path/to/project && ./venv/bin/python refresh_instagram_cache.py
```

Note: this is especially useful when your profile list is long and Instagram throttling (`401` responses) interrupts runs partway through the list.

---

## Notes

- This project summarizes captions only.
- Image analysis could be added using a multimodal model such as qwen2.5vl.
- Instagram endpoints may change over time.
- currently tested with qwen3:8b, qwen3:14b, gemma3:12b on macbook m4 pro
- at time of testing, gemma outputs were generally less useful than qwen outputs for this newsletter task
- the newsletter may incorrectly attribute things from one post to a different account. in testing this happened for 2/40 events. 

---

## Disclaimer

Users are responsible for complying with Instagram's Terms of Service and any applicable data usage policies when accessing or processing content.

---

## AI Assistance Disclosure

Some portions of this project were developed with the assistance of AI tools (including large language models) to help generate code, documentation, and implementation ideas.

---

## License

MIT License
