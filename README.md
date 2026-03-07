# insta-newsletter
a simple tool to reduce reliance on instagram for learning about things you care about


A small Python tool that monitors Instagram profiles, extracts captions from posts in the last few days, and generates a newsletter-style summary using a local LLM running with Ollama.

This project is designed to run locally and can be scheduled with a cron job to generate periodic summaries.

---
✨🌿☕📸📝🤖📬📝📸☕🌿✨
# Overview

The script:

1. Reads a list of Instagram profiles from a file
2. Fetches recent posts from each profile
3. Extracts captions from posts within a specified time window
4. Sends the captions to a local LLM via Ollama
5. Produces a concise newsletter-style summary

Example use cases:

- monitoring community organizations
- tracking local businesses or events
- reducing personal reliance on instagram app

---

## Example Output
TITLE:
Bay Area Literary & Community Updates
UPDATES:
Litquake announced submission deadlines for its upcoming festival.
Lit Crawl SF promoted upcoming literary events and author programming.
Oaktown Spice Shop highlighted new seasonal spice blends.
NEWSLETTER:
Several Bay Area organizations shared updates this week. Litquake and Lit Crawl SF both announced upcoming programming and submission deadlines for literary events, while local businesses such as Oaktown Spice Shop highlighted new seasonal offerings.

---

## Requirements

- Python 3.9+
- Ollama installed locally
- A local LLM model (for example `llama3.2` or `qwen3`)

Install Ollama:

https://ollama.com

Pull a model:

ollama pull llama3.2

# Installation
## Clone the repository:
git clone https://github.com/yourusername/insta-newsletter.git
cd insta-newsletter
## Create a virtual environment:
python3 -m venv venv
source venv/bin/activate
## Install dependencies:
pip install requests
## Configuration
Profiles are stored in a separate file.

Create profiles.txt:
https://www.instagram.com/officialelaichico/
https://www.instagram.com/litquake/
Add one profile per line.

# Usage
##Run the script:

python insta_newsletter.py

The script will:
- fetch recent posts
- extract captions
- send them to the LLM
- print a newsletter-style summary

## Running Automatically
You can run the script periodically using cron.
Example (every 3 days):
0 9 */3 * * cd /path/to/project && ./venv/bin/python insta_newsletter.py

# Notes
This project summarizes captions only.
Image analysis could be added using a multimodal model such as qwen2.5vl.
Instagram endpoints may change over time.
## Disclaimer
Users are responsible for complying with Instagram's Terms of Service and any applicable data usage policies when accessing or processing content.

## AI Assistance Disclosure
Some portions of this project were developed with the assistance of AI tools (including large language models) to help generate code, documentation, and implementation ideas.

## License
MIT License
