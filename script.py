import os
import yaml
import requests
import anthropic
import gspread
import json
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
        worksheet = sh.add_worksheet(title=tab_name, rows="1000", cols="20")
    return worksheet

# ── Sentiment via Claude ──────────────────────────────────────────────────────
def analyse_sentiment(comment_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Analyse this LinkedIn comment about HubSpot. "
                f"Reply with JSON only, no explanation:\n"
                f"{{\n"
                f'  "sentiment": "positive" or "neutral" or "negative",\n'
                f'  "pain_point": "one sentence summary of the complaint or null if none"\n'
                f"}}\n\n"
                f"Comment: {comment_text}"
            )
        }]
    )
    try:
        result = json.loads(message.content[0].text)
        return result.get("sentiment", "neutral"), result.get("pain_point", "")
    except Exception as e:
        print(f"Sentiment analysis failed: {e}")
        return "neutral", ""

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
        post_text   = post.get("text", "")
        post_url    = post.get("share_url", "")
        author      = post.get("actor_name", "")
        date_posted = post.get("date_posted", "")
        comments    = post.get("comments", [])

        if not comments:
            sentiment, pain_point = analyse_sentiment(post_text)
            rows.append([
                datetime.utcnow().strftime("%Y-%m-%d"),
                cfg["keyword"],
                author,
                date_posted,
                post_text[:500],
                post_url,
                "",
                sentiment,
                pain_point
            ])
        else:
            for comment in comments:
                comment_text = comment.get("comment_text", "")
                if not comment_text:
                    continue
                sentiment, pain_point = analyse_sentiment(comment_text)
                rows.append([
                    datetime.utcnow().strftime("%Y-%m-%d"),
                    cfg["keyword"],
                    author,
                    date_posted,
                    post_text[:500],
                    post_url,
                    comment_text[:300],
                    sentiment,
                    pain_point
                ])

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
            post_text   = post.get("text", "")
            post_url    = post.get("share_url", "")
            author      = post.get("actor_name", "")
            date_posted = post.get("date_posted", "")
            comments    = post.get("comments", [])

            if not comments:
                sentiment, pain_point = analyse_sentiment(post_text)
                rows.append([
                    datetime.utcnow().strftime("%Y-%m-%d"),
                    profile_url,
                    author,
                    date_posted,
                    post_text[:500],
                    post_url,
                    "",
                    sentiment,
                    pain_point
                ])
            else:
                for comment in comments:
                    comment_text = comment.get("comment_text", "")
                    if not comment_text:
                        continue
                    sentiment, pain_point = analyse_sentiment(comment_text)
                    rows.append([
                        datetime.utcnow().strftime("%Y-%m-%d"),
                        profile_url,
                        author,
                        date_posted,
                        post_text[:500],
                        post_url,
                        comment_text[:300],
                        sentiment,
                        pain_point
                    ])

    return rows

# ── Write to Sheets ───────────────────────────────────────────────────────────
def write_to_sheet(tab_name, headers, rows):
    ws = get_sheet(tab_name)
    ws.clear()
    ws.append_row(headers, value_input_option="RAW")
    for row in rows:
        ws.append_row(row, value_input_option="RAW")
    print(f"Written {len(rows)} rows to '{tab_name}'")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Use Case 2 — Keyword Intel
    print("=== Use Case 2: Keyword Intel ===")
    keyword_rows = run_keyword_search()
    if keyword_rows:
        write_to_sheet(
            config["google_sheets"]["keyword_tab"],
            ["Run Date", "Keyword", "Post Author", "Post Date",
             "Post Text", "Post URL", "Comment", "Sentiment", "Pain Point"],
            keyword_rows
        )
    else:
        print("No keyword rows to write.")

    # Use Case 1 — Profile Posts
    print("=== Use Case 1: Profile Posts ===")
    profile_rows = run_profile_posts()
    if profile_rows:
        write_to_sheet(
            config["google_sheets"]["profile_tab"],
            ["Run Date", "Profile URL", "Author", "Post Date",
             "Post Text", "Post URL", "Comment", "Sentiment", "Pain Point"],
            profile_rows
        )
    else:
        print("No profile rows to write.")

    print("=== Done! ===")
