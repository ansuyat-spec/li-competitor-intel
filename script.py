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

# ── Sentiment via Claude ──────────────────────────────────────────────────────
def analyse_sentiment(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Analyse this LinkedIn comment about HubSpot. "
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
        # Fallback: extract values with regex if JSON parsing fails
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
    # Debug: print raw comment structure once
    if not hasattr(get_commenter_details, "_debugged"):
        print(f"DEBUG comment keys: {list(comment.keys())}")
        print(f"DEBUG comment sample: {json.dumps(comment, default=str)[:800]}")
        get_commenter_details._debugged = True

    name    = comment.get("commenter_name", "") or ""
    title   = comment.get("commenter_title", "") or ""
    company = ""

    # Try direct field first
    employer = comment.get("default_position_company_name", "")
    if employer:
        company = employer
    else:
        # Try nested employer list
        employers = comment.get("employer", [])
        if employers and isinstance(employers, list):
            for emp in employers:
                if emp.get("end_date") is None:
                    company = emp.get("company_name", "")
                    break
            if not company and employers:
                company = employers[0].get("company_name", "")

    return name, title, company

# ── Build a row ───────────────────────────────────────────────────────────────
def build_row(source, post, comment=None):
    post_text   = post.get("text", "")
    post_url    = post.get("share_url", "")
    author      = post.get("actor_name", "")
    date_posted = post.get("date_posted", "")
    run_date    = datetime.utcnow().strftime("%Y-%m-%d")

    if comment:
        comment_text = comment.get("comment_text", "")
        commenter_name, commenter_title, commenter_company = get_commenter_details(comment)
        sentiment, pain_point = analyse_sentiment(comment_text)
        return [
            run_date, source, author, date_posted,
            post_url, post_text[:500], comment_text[:300],
            commenter_name, commenter_title, commenter_company,
            sentiment, pain_point
        ]
    else:
        sentiment, pain_point = analyse_sentiment(post_text)
        return [
            run_date, source, author, date_posted,
            post_url, post_text[:500], "",
            "", "", "",
            sentiment, pain_point
        ]

# ── Use Case 2: Keyword search ────────────────────────────────────────────────
def run_keyword_search():
    print("Running keyword search...")
    cfg = config["crustdata"]
    payload = {
        "keyword": cfg["keyword"],
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
        print(f"Keyword search failed: {resp.status_code} {resp.text}")
        return []

    response = resp.json()
    posts = response if isinstance(response, list) else response.get("posts", [])
    rows = []

    for post in posts:
        comments = post.get("comments", [])
        if not comments:
            rows.append(build_row(cfg["keyword"], post))
        else:
            for comment in comments:
                if not comment.get("comment_text", ""):
                    continue
                rows.append(build_row(cfg["keyword"], post, comment))

    return rows

# ── Use Case 1: Profile posts ─────────────────────────────────────────────────
def run_profile_posts():
    print("Running profile posts fetch...")
    cfg = config["crustdata"]
    rows = []

    for profile_url in cfg["profiles"]:
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
            print(f"Profile fetch failed for {profile_url}: {resp.status_code} {resp.text}")
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
                    rows.append(build_row(profile_url, post, comment))

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
    # Use Case 2 — Keyword Intel
    print("=== Use Case 2: Keyword Intel ===")
    keyword_rows = run_keyword_search()
    if keyword_rows:
        write_latest(config["google_sheets"]["keyword_tab"], keyword_rows)
        write_archive(config["google_sheets"]["keyword_archive_tab"], keyword_rows)
    else:
        print("No keyword rows to write.")

    # Use Case 1 — Profile Posts
    print("=== Use Case 1: Profile Posts ===")
    profile_rows = run_profile_posts()
    if profile_rows:
        write_latest(config["google_sheets"]["profile_tab"], profile_rows)
        write_archive(config["google_sheets"]["profile_archive_tab"], profile_rows)
    else:
        print("No profile rows to write.")

    print("=== All done! ===")
