import os
import re
import time
import json
import html
import hashlib
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as c_requests
import requests
import xml.etree.ElementTree as ET
import PyPDF2
from bs4 import BeautifulSoup
import google.generativeai as genai

# ---------------- CONFIG ----------------
BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip()
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(GEMINI_MODEL)

FEEDS = {
    "result": "https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx",
    "announcement": "https://www.bseindia.com/data/xml/announcements.aspx",
}

DB_PATH = os.getenv("DB_PATH", "marketmunafa_seen.sqlite3")
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "100"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "12"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "14000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
MIN_SOURCE_TEXT = int(os.getenv("MIN_SOURCE_TEXT", "250"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ---------------- STORAGE / HTTP ----------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted (
                item_id TEXT PRIMARY KEY,
                title TEXT,
                source_url TEXT,
                posted_at TEXT
            )
        """)
        conn.commit()


def was_posted(item_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM posted WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None


def mark_posted(item_id, title, source_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted(item_id,title,source_url,posted_at) VALUES(?,?,?,?)",
            (item_id, title, source_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_url(url, stream=False):
    last_error = None
    profiles = ["chrome120", "chrome110", "edge101", "chrome104"]
    
    for attempt in range(4):
        try:
            profile = profiles[attempt % len(profiles)]
            headers = {
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/pdf, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bseindia.com/",
            }
            response = c_requests.get(url, impersonate=profile, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code in (403, 429):
                raise requests.HTTPError(
                    f"BSE/server returned {response.status_code}; rate-limited",
                    response=response,
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            wait = min(3 * (2 ** attempt), 30)
            log.warning("GET failed (%s). Retry in %ss", exc, wait)
            if attempt < 3:
                time.sleep(wait)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


# ---------------- FEED PARSING ----------------
def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node, names):
    for child in list(node):
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def extract_links_from_xml_item(item):
    found = []
    for node in item.iter():
        tag = local_name(node.tag)
        href = node.attrib.get("href") or node.attrib.get("url")
        if href and tag in ("link", "enclosure"):
            found.append(href.strip())
        if tag == "link" and node.text and node.text.strip().startswith("http"):
            found.append(node.text.strip())
    for node in item.iter():
        if local_name(node.tag) == "link" and node.text and node.text.strip().startswith("http"):
            found.append(node.text.strip())
    return list(dict.fromkeys(found))


def parse_feed(feed_url, feed_kind):
    response = get_url(feed_url)
    root = ET.fromstring(response.content)
    items = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]
    output = []

    for node in items[:MAX_ITEMS_PER_FEED]:
        title = child_text(node, {"title"})
        description = child_text(node, {"description", "summary", "content", "encoded"})
        guid = child_text(node, {"guid", "id"})
        links = extract_links_from_xml_item(node)

        link = ""
        direct = [u for u in links if urlparse(u).path.lower().endswith((".pdf", ".xml", ".xbrl", ".html", ".htm"))]
        if direct:
            link = direct[0]
        elif links:
            link = links[0]

        if not title:
            continue

        output.append({
            "kind": feed_kind,
            "title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(),
            "description": html.unescape(re.sub(r"<[^>]+>", " ", description)).strip(),
            "link": urljoin(feed_url, link) if link else "",
            "guid": guid or link or title,
            "published": child_text(node, {"pubdate", "published", "updated", "date"}),
        })
    return output


# ---------------- DOCUMENT EXTRACTION ----------------
def clean_text(value):
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_pdf_text(url):
    if not url or not urlparse(url).path.lower().endswith(".pdf"):
        return ""
    response = get_url(url)
    data = response.content
    text_parts = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages[:MAX_PDF_PAGES]:
                text_parts.append(page.extract_text() or "")
    return clean_text("\n".join(text_parts))[:MAX_TEXT_CHARS]


def extract_xml_text(url):
    if not url or not urlparse(url).path.lower().endswith((".xml", ".xbrl")):
        return ""
    response = get_url(url)
    root = ET.fromstring(response.content)
    values = []
    for element in root.iter():
        if element.text and element.text.strip():
            values.append(element.text.strip())
    return clean_text(" ".join(values))[:MAX_TEXT_CHARS]


def extract_html_text(url):
    if not url or not urlparse(url).path.lower().endswith((".html", ".htm")):
        return ""
    response = get_url(url)
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return clean_text(soup.get_text(separator=" "))[:MAX_TEXT_CHARS]


def get_document_text(item):
    parts = []
    if item["description"]:
        parts.append("FEED DESCRIPTION:\n" + item["description"])

    url = item["link"]
    if url:
        path = urlparse(url).path.lower()
        try:
            if path.endswith((".xml", ".xbrl")):
                extracted = extract_xml_text(url)
            elif path.endswith((".html", ".htm")):
                extracted = extract_html_text(url)
            elif path.endswith(".pdf"):
                extracted = extract_pdf_text(url)
            else:
                extracted = ""
            if extracted:
                parts.append("ATTACHMENT TEXT:\n" + extracted)
        except Exception as exc:
            log.warning("Attachment extraction failed for %s: %s", url, exc)

    return clean_text("\n\n".join(parts))[:MAX_TEXT_CHARS]


# ---------------- CLASSIFICATION / AI (ENGLISH) ----------------
RESULT_WORDS = (
    "financial result", "unaudited financial", "audited financial",
    "quarterly result", "half year result", "annual result", "financial results",
    "standalone result", "consolidated result",
)
ORDER_WORDS = (
    "order", "contract", "work order", "letter of award", "loa",
    "purchase order", "project award", "work worth", "received an order",
)


def classify_item(item):
    title = (item["title"] + " " + item["description"]).lower()
    if item["kind"] == "result" or any(word in title for word in RESULT_WORDS):
        return "result"
    if any(word in title for word in ORDER_WORDS):
        return "order"
    return ""


def make_item_id(item):
    basis = "|".join((item["guid"], item["title"], item["link"]))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def analyze_with_gemini(company_title, doc_type, source_text, source_url):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    if doc_type == "result":
        task = """
Analyze this BSE financial result filing in English to generate a Google News & AdSense optimized post:
1. Generate an SEO-Friendly Catchy Title containing Company Name, Quarter, and key highlights (Revenue, Net Profit growth).
2. Extract strictly based on the provided document:
   - Revenue (Current Quarter vs Previous Year corresponding Quarter & Growth %)
   - Gross Profit (Current Quarter vs Previous Year & Growth %)
   - Net Profit / PAT (Current Quarter vs Previous Year & Growth %)
   - Company Total Debt & Debt-to-Equity Ratio (if available, with implications for Intraday/Positional trading).
3. Create a comparative Column/Bar Chart Table showing multi-quarter or YoY results.
4. Keep the summary professional, clear, and easy to read.
"""
    else:
        task = """
Analyze this new order/contract BSE filing in English:
- Mention client name, order value, scope of work, and timeline strictly if available.
- Highlight significance, potential risks, and impact.
"""
    prompt = f"""
You are an expert financial editor for 'MarketMunafa'. Write an objective, highly optimized, SEO-friendly article in English.

Company/Feed Headline: {company_title}
Document Type: {doc_type}
Source File: {source_url}

Strict Rules:
1. Do not invent numbers or data not present in the source text.
2. If specific debt or profit numbers are missing, state "Data not explicitly available in the filing."
3. Avoid buy/sell stock recommendations.

{task}

Output format must be strictly as follows:
<b>SEO Title:</b> ...
<b>Company Overview:</b> ...
<b>Key Financial Metrics (Revenue, Gross Profit, Net Profit):</b> ...
<b>Debt & Debt-to-Equity Analysis (Intraday Insight):</b> ...
<b>Quarterly Comparison Chart (Table Format):</b> ...
<b>Conclusion & Watchpoints:</b> ...

Source Text:
{source_text[:MAX_TEXT_CHARS]}
"""
    response = gemini_model.generate_content(prompt)
    result = (response.text or "").strip()
    if not result:
        raise RuntimeError("Gemini returned an empty response")
    return result


# ---------------- PUBLISHING (BLOGGER ONLY - ENGLISH) ----------------
def blogger_access_token():
    required = (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN)
    if not all(required):
        raise RuntimeError("Google Blogger OAuth secrets are missing")
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def blogger_publish(title, content_html):
    if not BLOG_ID:
        raise RuntimeError("BLOG_ID is missing")
    token = blogger_access_token()
    
    payload = {
        "kind": "blogger#post",
        "title": title,
        "content": content_html,
        "labels": ["BSE UPDATE", "MARKET NEWS", "MarketMunafa", "Financial Results"]
    }
    
    response = requests.post(
        f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("url", "")


def format_summary_html(text):
    escaped = html.escape(text)
    escaped = escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return escaped.replace("\n", "<br>\n")


def publish_item(item, doc_type, summary, item_id):
    source_url = item["link"]
    title = item["title"][:180]
    
    # Extract SEO Title if generated by AI
    post_title = f"{title} | MarketMunafa"
    if "SEO Title:" in summary:
        parts = summary.split("SEO Title:")
        if len(parts) > 1:
            extracted_title = parts[1].split("<b>")[0].split("<br>")[0].strip()
            if len(extracted_title) > 10:
                post_title = extracted_title

    safe_summary = format_summary_html(summary)
    source_html = (
        f'<p><a href="{html.escape(source_url, quote=True)}" '
        f'rel="nofollow noopener" target="_blank">View Original BSE Filing</a></p>'
        if source_url else ""
    )
    
    blog_html = f"""
    <article style="font-family: Arial, sans-serif; line-height: 1.6;">
      <div style="background: #f4f6f8; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
        <p><i>MarketMunafa | Official BSE Corporate Disclosure & Financial Analysis</i></p>
      </div>
      <div>{safe_summary}</div>
      {source_html}
      <hr style="margin: 20px 0;">
      <p><small><b>Disclaimer:</b> This article is prepared for informational purposes based on public corporate filings. This is not financial advice. Please review original filing before making investment or intraday decisions.</small></p>
    </article>
    """

    if POST_TO_BLOGGER:
        try:
            blog_url = blogger_publish(post_title, blog_html)
            mark_posted(item_id, title, source_url)
            log.info("Blogger post successful: %s", blog_url)
        except Exception as exc:
            log.exception("Blogger publishing failed")
            raise RuntimeError(f"Blogger: {exc}")
    else:
        log.warning("Blogger publishing is disabled")


# ---------------- MAIN ----------------
def run():
    init_db()
    all_items = []

    for kind, feed_url in FEEDS.items():
        try:
            items = parse_feed(feed_url, kind)
            log.info("%s feed: %d items", kind, len(items))
            all_items.extend(items)
        except Exception:
            log.exception("Feed failed: %s", feed_url)

    if not all_items:
        log.warning("No feed items retrieved. Check BSE feed availability/network.")
        return

    seen_in_run = set()
    for item in all_items:
        item_id = make_item_id(item)
        if item_id in seen_in_run or was_posted(item_id):
            continue
        seen_in_run.add(item_id)

        doc_type = classify_item(item)
        if not doc_type:
            continue

        log.info("Processing %s: %s", doc_type, item["title"])
        try:
            source_text = get_document_text(item)
            if len(source_text) < MIN_SOURCE_TEXT:
                log.warning(
                    "Skipped: insufficient filing text (%d chars): %s",
                    len(source_text), item["title"]
                )
                continue

            summary = analyze_with_gemini(
                item["title"], doc_type, source_text, item["link"]
            )
            publish_item(item, doc_type, summary, item_id)
            time.sleep(5)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
