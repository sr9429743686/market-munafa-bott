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
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
import xml.etree.ElementTree as ET
from pypdf import PdfReader
from google import genai

# ---------------- CONFIG ----------------
BLOG_ID = os.getenv("BLOG_ID", "").strip()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# BSE feeds listed publicly. If a feed is temporarily unavailable, the other
# feed continues to be checked. These are feeds, not a guarantee of full history.
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

session = requests.Session()
session.headers.update({
    "User-Agent": "MarketMunafaResearchBot/1.0 (contact: configure-your-email)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/pdf, */*",
    "Accept-Language": "en-IN,en;q=0.9",
})
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


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
    """Polite retries for transient failures; do not rotate identities or bypass blocks."""
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
            if response.status_code in (403, 429):
                raise requests.HTTPError(
                    f"BSE/server returned {response.status_code}; access may be restricted or rate-limited",
                    response=response,
                )
            response.raise_for_status()
            return response
        except (requests.RequestException, ValueError) as exc:
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
        direct = [u for u in links if urlparse(u).path.lower().endswith((".pdf", ".xml", ".xbrl"))]
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
    content_type = response.headers.get("Content-Type", "").lower()
    data = response.content
    if "pdf" not in content_type and not data.startswith(b"%PDF"):
        log.warning("Attachment URL did not return a PDF: %s", url)
        return ""

    text_parts = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        reader = PdfReader(tmp.name)
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
            elif path.endswith(".pdf"):
                extracted = extract_pdf_text(url)
            else:
                extracted = ""
            if extracted:
                parts.append("ATTACHMENT TEXT:\n" + extracted)
        except Exception as exc:
            log.warning("Attachment extraction failed for %s: %s", url, exc)

    return clean_text("\n\n".join(parts))[:MAX_TEXT_CHARS]


# ---------------- CLASSIFICATION / AI ----------------
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
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    if doc_type == "result":
        task = """
આ BSE filing માંથી Gujarati માં પરિણામનું વિશ્લેષણ કરો:
- ક્વાર્ટર/સમયગાળો અને standalone કે consolidated છે તે જણાવો.
- Revenue from Operations, Total Income, Net Profit/PAT, EPS અને અગાઉના સમાન સમયગાળાની સરખામણી — ફક્ત દસ્તાવેજમાં ઉપલબ્ધ હોય તો.
- આંકડા અને એકમો (₹ લાખ/કરોડ) બરાબર લખો.
- આવક અને નફામાં વધારો/ઘટાડો હોય તો દસ્તાવેજના આંકડાથી ગણતરી કરો; આધાર ન હોય તો ગણતરી ન કરો.
- એકંદર પરિણામની ગુણવત્તા અને મુખ્ય જોખમો 2-3 વાક્યમાં સમજાવો.
"""
    else:
        task = """
આ BSE filing માંથી Gujarati માં નવા ઓર્ડર/કોન્ટ્રાક્ટનું વિશ્લેષણ કરો:
- ઓર્ડર આપનાર ક્લાયન્ટ, ઓર્ડરનું મૂલ્ય, કામનું સ્વરૂપ અને સમયમર્યાદા — ફક્ત દસ્તાવેજમાં હોય તો.
- ઓર્ડર કંપનીના જાહેર કરેલા કદ/આવકની સરખામણીમાં કેટલો મહત્વનો છે તે માત્ર જરૂરી આંકડા ઉપલબ્ધ હોય ત્યારે જ કહો.
- જૂના ઓર્ડર/અનુમાનિત આવક સાથે ગૂંચવણ ન કરો.
- મુખ્ય શરતો, અમલીકરણનો સમય અને જોખમો જણાવો.
"""
    prompt = f"""
તમે MarketMunafa માટે નાણાકીય દસ્તાવેજ સમજાવતા ગુજરાતી સંપાદક છો.
નીચેની filing નો નિષ્પક્ષ, તથ્ય આધારિત સારાંશ લખો.

કંપની/ફીડ હેડલાઇન: {company_title}
દસ્તાવેજ પ્રકાર: {doc_type}
મૂળ ફાઇલ: {source_url}

કઠોર નિયમો:
1. નીચેના સ્રોત લખાણમાં ન હોય એવો કોઈ આંકડો, તારીખ, ક્લાયન્ટ કે દાવો બનાવશો નહીં.
2. માહિતી ઉપલબ્ધ ન હોય તો "ફાઇલમાં સ્પષ્ટ માહિતી ઉપલબ્ધ નથી" લખો.
3. શેર ખરીદો/વેચો, target price કે ખાતરીવાળો ભાવ-અસરનો દાવો ન કરો.
4. પરિણામ/ઓર્ડર સકારાત્મક છે કે નકારાત્મક તે સમજાવો, પણ નિશ્ચિત શેર ભાવની આગાહી ન કરો.
5. ભાષા સરળ અને વ્યાવસાયિક ગુજરાતી રાખો.

{task}

આઉટપુટ આ ફોર્મેટમાં આપો:
<b>કંપની:</b> ...
<b>મુખ્ય વિગતો:</b> ...
<b>આંકડાકીય વિશ્લેષણ:</b> ...
<b>નિષ્કર્ષ:</b> ...
<b>ધ્યાનમાં રાખવા જેવી બાબતો:</b> ...

સ્રોત લખાણ:
{source_text[:MAX_TEXT_CHARS]}
"""
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    result = (response.text or "").strip()
    if not result:
        raise RuntimeError("Gemini returned an empty response")
    return result


# ---------------- PUBLISHING (BLOGGER ONLY) ----------------
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
    response = requests.post(
        f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "kind": "blogger#post",
            "blog": {"id": BLOG_ID},
            "title": title,
            "content": content_html,
            "labels": ["BSE UPDATE", "MARKET NEWS", "MarketMunafa"],
        },
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("url", "")


def html_escape(text):
    return html.escape(text).replace("\n", "<br>\n")


def publish_item(item, doc_type, summary, item_id):
    source_url = item["link"]
    title = item["title"][:180]
    post_title = f"{title} | MarketMunafa"

    safe_summary = html_escape(summary)
    safe_title = html.escape(title)
    source_html = (
        f'<p><a href="{html.escape(source_url, quote=True)}" '
        f'rel="nofollow noopener" target="_blank">મૂળ BSE Filing જુઓ</a></p>'
        if source_url else ""
    )
    blog_html = f"""
    <article>
      <h2>{safe_title}</h2>
      <p><i>MarketMunafa | BSE Corporate Disclosure Analysis</i></p>
      <div>{safe_summary}</div>
      {source_html}
      <hr>
      <p><small>નોંધ: આ લેખ જાહેર કંપની ફાઇલિંગના આધારે માહિતી માટે તૈયાર કરાયો છે.
      આ રોકાણ સલાહ નથી. નિર્ણય લેતા પહેલાં મૂળ filing તપાસો.</small></p>
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
            time.sleep(2)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
