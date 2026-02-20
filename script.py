import os
import yaml
import requests
import anthropic
import gspread
import json
import time
import re
from google.oauth2.service_account import Credentials
from datetime import datetime

# ── Load config ──────────────────────────────────────────────────────────────
with open("config.yml", "r") as f:
    config = yaml.safe_load(f)

CRUSTDATA_KEY = os.environ["CRUSTDATA_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

HEADERS = {
    "Authorization": f"Token {CRUSTDATA_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

SHEET_HEADERS = [
    "Run Date", "Keyword/Profile URL", "Post Author", "Post Date",
    "Post URL", "Post Content", "Comment",
    "Commenter Name", "Commenter Title", "Commenter Company",
    "Sentiment", "Pain Point"
]

# ── Google Sheets setup ───────────────────────────────────────────────────────
def get_sheet(tab_name):
    creds_dict = json.loads(GOOGLE_CREDS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(config["google_sheets"]["spreadsheet_id"])
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows="5000", cols="20")
    return worksheet

# ── Load keywords and profiles from Config tab ────────────────────────────────
def load_config_from_sheets():
    print("Loading keywords and profiles from Config tab...")
    ws = get_sheet(config["google_sheets"]["config_tab"])
    rows = ws.get_all_values()
    keywords = []
    profiles = []
    for row in rows[1:]:  # skip header row
        if len(row) > 0 and row[0].strip():
            keywords.append(row[0].strip())
        if len(row) > 1 and row[1].strip():
            profiles.append(row[1].strip())
    print(f"Found {len(keywords)} keywords: {keywords}")
    print(f"Found {len(profiles)} profiles: {profiles}")
    return keywords, profiles

# ── Sentiment via Claude ──────────────────────────────────────────────────────
def analyse_sentiment(text, keyword="HubSpot"):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Analyse this LinkedIn comment about {keyword}. "
                f"Reply with JSON only, no explanation, no markdown, "
                f"no quotes inside string values:\n"
                f"{{\n"
                f'  "sentiment": "positive" or "neutral" or "negative",\n'
                f'  "pain_point": "one sentence summary of complaint or null if none"\n'
                f"}}\n\n"
                f"Comment: {text}"
            )
        }]
    )
    try:
        raw = message.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        return result.get("sentiment", "neutral"), result.get("pain_point", "") or ""
    except Exception as e:
        try:
            raw = message.content[0].text.strip()
            sentiment = re.search(r'"sentiment"\s*:\s*"(\w+)"', raw)
            pain_point = re.search(r'"pain_point"\s*:\s*"([^"]*)"', raw)
            return (
                sentiment.group(1) if sentiment else "neutral",
                pain_point.group(1) if pain_point else ""
            )
        except:
            print(f"Sentiment analysis failed: {e}")
            print(f"Raw response was: {message.content[0].text[:200]}")
            return "neutral", ""

# ── Extract commenter details ─────────────────────────────────────────────────
def get_commenter_details(comment):
    details  = comment.get("commenter_details", {}) or {}

    name     = details.get("name", "") or ""
    title    = details.get("default_position_title", "") or ""
    headline = details.get("headline", "") or ""
    company  = ""

    employers = details.get("employer", [])
    if employers and isinstance(employers, list):
        for emp in employers:
            if emp.get("end_date") is None:
                company = emp.get("company_name", "")
                break
        if not company and employers:
            company = employers[0].get("company_name", "")

    if not title and headline:
        title = headline

    return name, title, company

# ── Build a row ───────────────────────────────────────────────────────────────
def build_row(source, post, keyword="", comment=None):
    post_text   = post.get("text", "")
    post_url    = post.get("share_url", "")
    author      = post.get("actor_name", "")
    date_posted = post.get("date_posted", "")
    run_date    = datetime.utcnow().strftime("%Y-%m-%d")

    if comment:
        comment_text = comment.get("comment_text", "")
        commenter_name, commenter_title, commenter_company = get_commenter_details(comment)
        sentiment, pain_point = analyse_sentiment(comment_text, keyword)
        return [
            run_date, source, author, date_posted,
            post_url, post_text[:500], comment_text[:300],
            commenter_name, commenter_title, commenter_company,
            sentiment, pain_point
        ]
    else:
        sentiment, pain_point = analyse_sentiment(post_text, keyword)
        return [
            run_date, source, author, date_posted,
            post_url, post_text[:500], "",
            "", "", "",
            sentiment, pain_point
        ]

# ── Use Case 2: Keyword search ────────────────────────────────────────────────
def run_keyword_search(keywords):
    print("Running keyword search...")
    cfg = config["crustdata"]
    all_rows = []

    for keyword in keywords:
        print(f"  Searching for: {keyword}")
        payload = {
            "keyword": keyword,
            "date_posted": cfg["date_posted"],
            "limit": cfg["max_posts"],
            "fields": "comments",
            "max_comments": cfg["max_comments"]
        }
        resp = requests.post(
            "https://api.crustdata.com/screener/linkedin_posts/keyword_search/",
            headers=HEADERS,
            json=payload
        )
        if resp.status_code != 200:
            print(f"  Keyword search failed for '{keyword}': {resp.status_code} {resp.text}")
            continue

        response = resp.json()
        posts = response if isinstance(response, list) else response.get("posts", [])

        for post in posts:
            comments = post.get("comments", [])
            if not comments:
                all_rows.append(build_row(keyword, post, keyword))
            else:
                for comment in comments:
                    if not comment.get("comment_text", ""):
                        continue
                    all_rows.append(build_row(keyword, post, keyword, comment))

        print(f"  Got {len(posts)} posts for '{keyword}'")

    return all_rows

# ── Use Case 1: Profile posts ─────────────────────────────────────────────────
def run_profile_posts(profiles):
    print("Running profile posts fetch...")
    cfg = config["crustdata"]
    rows = []

    for profile_url in profiles:
        print(f"  Fetching: {profile_url}")
        params = {
            "person_linkedin_url": profile_url,
            "limit": cfg["profile_posts_limit"],
            "fields": "comments",
            "max_comments": cfg["max_comments"]
        }
        resp = requests.get(
            "https://api.crustdata.com/screener/linkedin_posts",
            headers=HEADERS,
            params=params
        )
        if resp.status_code != 200:
            print(f"  Profile fetch failed for {profile_url}: {resp.status_code} {resp.text}")
            continue

        response = resp.json()
        posts = response if isinstance(response, list) else response.get("posts", [])

        for post in posts:
            comments = post.get("comments", [])
            if not comments:
                rows.append(build_row(profile_url, post))
            else:
                for comment in comments:
                    if not comment.get("comment_text", ""):
                        continue
                    rows.append(build_row(profile_url, post, "", comment))

        print(f"  Got {len(posts)} posts for {profile_url}")

    return rows

# ── Write latest (overwrite) ──────────────────────────────────────────────────
def write_latest(tab_name, rows):
    ws = get_sheet(tab_name)
    ws.clear()
    time.sleep(1)
    ws.append_row(SHEET_HEADERS, value_input_option="RAW")
    batch_size = 10
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ws.append_rows(batch, value_input_option="RAW")
        print(f"  Latest: written rows {i+1}–{i+len(batch)} of {len(rows)}")
        time.sleep(2)
    print(f"✓ '{tab_name}' updated — {len(rows)} rows")

# ── Write archive (append only) ───────────────────────────────────────────────
def write_archive(tab_name, rows):
    ws = get_sheet(tab_name)
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(SHEET_HEADERS, value_input_option="RAW")
        time.sleep(1)
    batch_size = 10
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ws.append_rows(batch, value_input_option="RAW")
        print(f"  Archive: written rows {i+1}–{i+len(batch)} of {len(rows)}")
        time.sleep(2)
    print(f"✓ '{tab_name}' updated — {len(rows)} rows appended")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Load keywords and profiles from Google Sheet Config tab
    keywords, profiles = load_config_from_sheets()

    if not keywords and not profiles:
        print("No keywords or profiles found in Config tab. Please add some and re-run.")
        exit(1)

    # Use Case 2 — Keyword Intel
    if keywords:
        print("=== Use Case 2: Keyword Intel ===")
        keyword_rows = run_keyword_search(keywords)
        if keyword_rows:
            write_latest(config["google_sheets"]["keyword_tab"], keyword_rows)
            write_archive(config["google_sheets"]["keyword_archive_tab"], keyword_rows)
        else:
            print("No keyword rows to write.")
    else:
        print("No keywords found in Config tab — skipping keyword search.")

    # Use Case 1 — Profile Posts
    if profiles:
        print("=== Use Case 1: Profile Posts ===")
        profile_rows = run_profile_posts(profiles)
        if profile_rows:
            write_latest(config["google_sheets"]["profile_tab"], profile_rows)
            write_archive(config["google_sheets"]["profile_archive_tab"], profile_rows)
        else:
            print("No profile rows to write.")
    else:
        print("No profiles found in Config tab — skipping profile posts.")

    print("=== All done! ===")
