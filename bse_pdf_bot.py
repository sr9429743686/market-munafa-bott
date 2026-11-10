import os
import re
import time
import json
import html
import hashlib
import logging
import sqlite3
import tempfile
import urllib.parse
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

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()

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
MIN_SOURCE_TEXT = int(os.getenv("MIN_SOURCE_TEXT", "200"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ---------------- DUPLICATE PROTECTION & STORAGE ----------------
def normalize_company_key(title):
    clean = re.sub(r"\(\d+\)", "", str(title))
    clean = clean.split("|")[0].split(":")[0]
    clean = re.sub(
        r"\b(q1|q2|q3|q4|fy\d+|h1|h2|results|result|ltd|limited|india|industries|pvt|private|corp|corporation)\b",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"[^a-zA-Z0-9]", "", clean).lower()
    return clean


def extract_file_key(url):
    if not url:
        return ""
    path = urlparse(url).path.lower()
    return path.rsplit("/", 1)[-1].strip()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted (
                item_id TEXT PRIMARY KEY,
                company_key TEXT,
                file_key TEXT,
                title TEXT,
                source_url TEXT,
                posted_at TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE posted ADD COLUMN company_key TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE posted ADD COLUMN file_key TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def was_posted_in_db(item_id, comp_key, file_key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM posted 
            WHERE item_id = ? 
               OR (file_key != '' AND file_key = ?)
               OR (company_key != '' AND company_key = ?)
            """,
            (item_id, file_key, comp_key),
        ).fetchone()
        return row is not None


def mark_posted(item_id, comp_key, file_key, title, source_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posted(item_id, company_key, file_key, title, source_url, posted_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (item_id, comp_key, file_key, title, source_url, datetime.now(timezone.utc).isoformat()),
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
                    f"Server returned {response.status_code}; rate-limited",
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


def to_float(val):
    try:
        cleaned = re.sub(r"[^\d.-]", "", str(val))
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


# ---------------- SCREENER FUNDAMENTALS (DEBT, D/E, QUARTERS & ANNUAL) ----------------
def get_screener_fundamentals(comp_title):
    clean_name = re.sub(r"\(\d+\)", "", comp_title)
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", clean_name, flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]
    
    queries = [clean_name]
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    if words:
        queries.append(words[0])

    info = {
        "price_str": "Live on BSE",
        "total_debt": "N/A",
        "debt_to_equity": "N/A",
        "quarters_text": "",
        "annual_text": "",
        "q_labels": [],
        "q_rev": [],
        "q_np": [],
        "y_labels": [],
        "y_rev": [],
        "y_np": [],
    }

    seen_q = set()
    for q in queries:
        if not q or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        try:
            search_url = f"https://www.screener.in/api/company/search/?q={urllib.parse.quote(q)}"
            res = c_requests.get(search_url, impersonate="chrome120", timeout=12)
            if res.status_code == 200 and isinstance(res.json(), list) and len(res.json()) > 0:
                comp_url = "https://www.screener.in" + res.json()[0]["url"]
                c_res = c_requests.get(comp_url, impersonate="chrome120", timeout=15)
                if c_res.status_code != 200:
                    continue
                soup = BeautifulSoup(c_res.content, "html.parser")

                top_ratios = soup.find("ul", id="top-ratios")
                if top_ratios:
                    for li in top_ratios.find_all("li"):
                        t = " ".join(li.get_text(" ", strip=True).split())
                        if "Current Price" in t:
                            num = li.find("span", class_="number")
                            if num:
                                info["price_str"] = f"₹{num.get_text(strip=True)}"
                        elif "Debt to equity" in t or "Debt to Equity" in t:
                            num = li.find("span", class_="number")
                            if num:
                                info["debt_to_equity"] = num.get_text(strip=True)

                borrowings, equity = None, None
                bs_sec = soup.find("section", id="balance-sheet")
                if bs_sec:
                    for row in bs_sec.find_all("tr"):
                        cols = [c.get_text(" ", strip=True).replace("+", "").replace(",", "").strip() for c in row.find_all(["td", "th"])]
                        if len(cols) >= 2:
                            r_name = cols[0].lower()
                            if "borrowings" in r_name or r_name == "debt":
                                try:
                                    borrowings = float(cols[-1])
                                    info["total_debt"] = f"₹{borrowings:,.0f} Cr"
                                except Exception:
                                    pass
                            elif "equity capital" in r_name or "reserves" in r_name:
                                try:
                                    equity = (equity or 0.0) + float(cols[-1])
                                except Exception:
                                    pass

                if info["debt_to_equity"] == "N/A" and borrowings is not None and equity and equity > 0:
                    info["debt_to_equity"] = f"{(borrowings / equity):.2f}"
                elif info["debt_to_equity"] == "N/A" and borrowings == 0:
                    info["debt_to_equity"] = "0.00"

                q_sec = soup.find("section", id="quarters")
                if q_sec:
                    q_lines = []
                    headers = []
                    for row in q_sec.find_all("tr"):
                        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]
                        if not cols:
                            continue
                        if not headers and len(cols) > 3:
                            headers = cols[1:]
                            info["q_labels"] = headers[-6:]
                        r_title = cols[0].lower()
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "ebitda", "net profit"]):
                            q_lines.append(" | ".join(cols))
                            nums = [to_float(c_val) for c_val in cols[1:][-6:]]
                            if "sales" in r_title or "revenue" in r_title:
                                info["q_rev"] = nums
                            elif "net profit" in r_title:
                                info["q_np"] = nums
                    if headers:
                        info["quarters_text"] = "Quarters Header: " + " | ".join(headers) + "\n" + "\n".join(q_lines)

                pl_sec = soup.find("section", id="profit-loss")
                if pl_sec:
                    pl_lines = []
                    pl_headers = []
                    for row in pl_sec.find_all("tr"):
                        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]
                        if not cols:
                            continue
                        if not pl_headers and len(cols) > 3:
                            pl_headers = cols[1:]
                            info["y_labels"] = pl_headers[-4:]
                        r_title = cols[0].lower()
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "net profit"]):
                            pl_lines.append(" | ".join(cols[-5:]))
                            nums = [to_float(c_val) for c_val in cols[1:][-4:]]
                            if "sales" in r_title or "revenue" in r_title:
                                info["y_rev"] = nums
                            elif "net profit" in r_title:
                                info["y_np"] = nums
                    if pl_headers:
                        info["annual_text"] = "Annual Header: " + " | ".join(pl_headers[-4:]) + "\n" + "\n".join(pl_lines)
                break
        except Exception as exc:
            log.warning("Screener lookup warning for %s: %s", q, exc)
    return info


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


# ---------------- CLASSIFICATION / AI (JSON STRUCTURED) ----------------
RESULT_WORDS = (
    "financial result", "unaudited financial", "audited financial",
    "quarterly result", "half year result", "annual result", "financial results",
    "standalone result", "consolidated result",
)


def classify_item(item):
    title_desc = (item["title"] + " " + item["description"]).lower()
    if "prior intimation" in title_desc or "is scheduled on" in title_desc:
        if item["kind"] != "result":
            return ""
    if item["kind"] == "result" or any(word in title_desc for word in RESULT_WORDS):
        return "result"
    return ""


def make_item_id(comp_key, file_key, item):
    basis = file_key or comp_key or item["guid"] or item["title"]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def analyze_with_gemini_json(company_title, source_text, screener_info):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    prompt = f"""
You are the Chief Financial Analyst for 'MarketMunafa'.
Analyze the following BSE Financial Result PDF text and supplementary Screener data.
Convert all figures into Rs. Crores (Rs. Cr.) if they are reported in Lakhs or Millions (1 Crore = 100 Lakhs = 10 Million).

Company Headline: {company_title}
Screener Price: {screener_info['price_str']}
Screener Total Debt: {screener_info['total_debt']}
Screener Debt-to-Equity: {screener_info['debt_to_equity']}
Screener Historical Quarters:
{screener_info['quarters_text']}
Screener Annual YoY Data:
{screener_info['annual_text']}

Return ONLY a valid JSON object (no markdown formatting outside JSON) with this exact structure:
{{
  "seo_title": "Catchy Google News SEO Title in English (under 110 chars, e.g. Divis Labs Q2 Results: Net Profit Jumps 63% to Rs 1,127 Cr, Revenue Up 35%)",
  "company_short_name": "Clean Company Name (e.g. Divis Labs)",
  "result_date": "Date of result (e.g. November 10, 2026)",
  "price_display": "Stock price or scrip code (e.g. {screener_info['price_str']})",
  "quarter_label": "e.g. Q2 FY26-27",
  "curr_period_label": "e.g. Sep 26",
  "prev_period_label": "e.g. Sep 25",
  "result_type": "Consolidated or Standalone",
  "revenue": {{"curr": "3,676", "prev": "2,715", "growth": "35%"}},
  "gross_profit": {{"curr": "1,424", "prev": "775", "growth": "83%"}},
  "net_profit": {{"curr": "1,127", "prev": "689", "growth": "63%"}},
  "total_debt": "Total Debt in Rs. Cr. (from PDF Balance Sheet or Screener data)",
  "debt_to_equity": "Debt to Equity ratio (e.g. 0.00 or 0.45)",
  "intraday_signal": "1-2 sentences in English explaining intraday/positional trading sentiment based on Debt-to-Equity ratio and profit growth.",
  "quarter_chart_labels": ["Jun 25", "Sep 25", "Dec 25", "Mar 26", "Jun 26", "Sep 26"],
  "quarter_chart_revenue": [2410, 2715, 2604, 2831, 3080, 3676],
  "quarter_chart_net_profit": [545, 689, 583, 751, 902, 1127],
  "yearly_chart_labels": ["FY25 (Prev Year)", "FY26/TTM (Curr Year)"],
  "yearly_chart_revenue": [10560, 12230],
  "yearly_chart_net_profit": [2568, 3363],
  "seo_article_paragraphs": [
    "Paragraph 1 (70-90 words): Objective English summary of quarterly revenue, gross/operating profit, and net profit growth for Google News and AdSense.",
    "Paragraph 2 (60-80 words): Balance sheet health covering Total Debt, Debt-to-Equity ratio, and key takeaways for market participants."
  ],
  "gujarati_summary": "2-3 sentences in natural Gujarati summarizing the company's quarterly & yearly result growth.",
  "gujarati_intraday_view": "1-2 sentences in natural Gujarati explaining the Debt-to-Equity ratio and whether the result is positive or negative for intraday/positional traders."
}}

BSE PDF Filing Text:
{source_text[:MAX_TEXT_CHARS]}
"""
    response = gemini_model.generate_content(prompt)
    raw = (response.text or "").strip()
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        raise RuntimeError("Gemini did not return valid JSON")
    return json.loads(match.group(0))


# ---------------- VISUAL CARD, THUMBNAIL & 2 COLUMN CHARTS BUILDER ----------------
def build_quarterly_column_chart_url(comp_name, labels, rev_list, np_list):
    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "Quarterly Revenue (Rs. Cr.)",
                    "backgroundColor": "#2563eb",
                    "data": rev_list
                },
                {
                    "label": "Quarterly Net Profit (Rs. Cr.)",
                    "backgroundColor": "#16a34a",
                    "data": np_list
                }
            ]
        },
        "options": {
            "title": {
                "display": True,
                "text": f"{comp_name} - All Quarters Performance Trend (Rs. Cr.)",
                "fontSize": 15
            },
            "legend": {"position": "bottom"},
            "plugins": {
                "datalabels": {
                    "anchor": "end",
                    "align": "top",
                    "color": "#1e293b",
                    "font": {"weight": "bold", "size": 10}
                }
            }
        }
    }
    encoded = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?w=720&h=380&bkg=white&c={encoded}"


def build_yoy_comparison_chart_url(comp_name, prev_lbl, curr_lbl, rev, gp, np_d, y_labels, y_rev, y_np):
    r_prev, r_curr = to_float(rev.get("prev", 0)), to_float(rev.get("curr", 0))
    gp_prev, gp_curr = to_float(gp.get("prev", 0)), to_float(gp.get("curr", 0))
    np_prev, np_curr = to_float(np_d.get("prev", 0)), to_float(np_d.get("curr", 0))

    labels = ["Revenue", "Gross Profit", "Net Profit"]
    prev_data = [r_prev, gp_prev, np_prev]
    curr_data = [r_curr, gp_curr, np_curr]

    if len(y_rev) >= 2 and len(y_np) >= 2:
        y_prev_r, y_curr_r = to_float(y_rev[-2]), to_float(y_rev[-1])
        y_prev_n, y_curr_n = to_float(y_np[-2]), to_float(y_np[-1])
        if y_prev_r > 0 or y_curr_r > 0:
            labels.extend(["Annual Revenue", "Annual Net Profit"])
            prev_data.extend([y_prev_r, y_prev_n])
            curr_data.extend([y_curr_r, y_curr_n])

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": f"Previous Year ({prev_lbl})",
                    "backgroundColor": "#94a3b8",
                    "data": prev_data
                },
                {
                    "label": f"Current Year ({curr_lbl})",
                    "backgroundColor": "#059669",
                    "data": curr_data
                }
            ]
        },
        "options": {
            "title": {
                "display": True,
                "text": f"{comp_name} - Previous Year vs Current Year Result Comparison (Rs. Cr.)",
                "fontSize": 15
            },
            "legend": {"position": "bottom"},
            "plugins": {
                "datalabels": {
                    "anchor": "end",
                    "align": "top",
                    "color": "#0f172a",
                    "font": {"weight": "bold", "size": 11}
                }
            }
        }
    }
    encoded = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?w=720&h=380&bkg=white&c={encoded}"


def growth_color(growth_str):
    if "-" in str(growth_str):
        return "#dc2626"
    return "#16a34a"


def build_post_html(data, screener_info, source_url):
    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", datetime.now().strftime("%B %d, %Y"))))
    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    curr_lbl = html.escape(str(data.get("curr_period_label", "Current")))
    prev_lbl = html.escape(str(data.get("prev_period_label", "Previous")))
    res_type = html.escape(str(data.get("result_type", "Consolidated")))

    rev = data.get("revenue", {"curr": "-", "prev": "-", "growth": "-"})
    gp = data.get("gross_profit", {"curr": "-", "prev": "-", "growth": "-"})
    np_d = data.get("net_profit", {"curr": "-", "prev": "-", "growth": "-"})

    total_debt = html.escape(str(data.get("total_debt") or screener_info["total_debt"]))
    de_ratio = html.escape(str(data.get("debt_to_equity") or screener_info["debt_to_equity"]))
    intraday_sig = html.escape(str(data.get("intraday_signal", "")))

    thumb_prompt = f"{comp_name} quarterly financial results stock market chart growth corporate india"
    thumb_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(thumb_prompt)}?width=800&height=450&nologo=true"

    q_labels = data.get("quarter_chart_labels") or screener_info["q_labels"] or [prev_lbl, curr_lbl]
    q_rev = data.get("quarter_chart_revenue") or screener_info["q_rev"] or [to_float(rev.get("prev")), to_float(rev.get("curr"))]
    q_np = data.get("quarter_chart_net_profit") or screener_info["q_np"] or [to_float(np_d.get("prev")), to_float(np_d.get("curr"))]
    quarter_chart_url = build_quarterly_column_chart_url(comp_name, q_labels, q_rev, q_np)

    y_labels = data.get("yearly_chart_labels") or screener_info["y_labels"] or []
    y_rev = data.get("yearly_chart_revenue") or screener_info["y_rev"] or []
    y_np = data.get("yearly_chart_net_profit") or screener_info["y_np"] or []
    yoy_chart_url = build_yoy_comparison_chart_url(comp_name, prev_lbl, curr_lbl, rev, gp, np_d, y_labels, y_rev, y_np)

    paragraphs = data.get("seo_article_paragraphs", [])
    para_html = "".join(f"<p style='color:#334155; font-size:16px; line-height:1.7; margin-bottom:14px;'>{html.escape(p)}</p>" for p in paragraphs)

    source_link_html = (
        f'<p style="margin-top:16px;"><a href="{html.escape(source_url, quote=True)}" '
        f'target="_blank" rel="nofollow noopener" style="color:#2563eb; font-weight:600; text-decoration:none;">'
        f'📄 View Official BSE Filing PDF</a></p>'
        if source_url else ""
    )

    html_body = f"""
    <article style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 680px; margin: 0 auto; color: #1e293b;">
      
      <figure style="margin: 0 0 22px 0; text-align: center;">
        <img src="{thumb_url}" alt="{comp_name} {qtr_lbl} Financial Results" style="width: 100%; max-height: 360px; object-fit: cover; border-radius: 12px;" />
        <figcaption style="font-size: 13px; color: #64748b; margin-top: 6px;">{comp_name} {qtr_lbl} Earnings & Fundamental Analysis | MarketMunafa</figcaption>
      </figure>

      <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 22px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
        <div style="display: flex; justify-content: space-between; color: #64748b; font-size: 14px; margin-bottom: 10px;">
          <span>{res_date}</span>
          <span>Rs.Cr.</span>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px;">
          <strong style="font-size: 22px; color: #0f172a;">{comp_name}</strong>
          <span style="font-size: 20px; font-weight: 600; color: #0f172a;">{price_disp}</span>
        </div>
        
        <table style="width: 100%; border-collapse: collapse; font-size: 16px;">
          <thead>
            <tr style="border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #64748b; font-weight: 500;">
              <th style="text-align: left; padding: 12px 4px;">{qtr_lbl}</th>
              <th style="text-align: right; padding: 12px 4px;">{curr_lbl}</th>
              <th style="text-align: right; padding: 12px 4px;">{prev_lbl}</th>
              <th style="text-align: right; padding: 12px 4px;">Growth</th>
            </tr>
          </thead>
          <tbody>
            <tr style="border-bottom: 1px solid #f1f5f9;">
              <td style="padding: 14px 4px; color: #1e293b; font-weight: 500;">Revenue</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600;">{html.escape(str(rev.get('curr', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; color: #475569;">{html.escape(str(rev.get('prev', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600; color: {growth_color(rev.get('growth', ''))};">{html.escape(str(rev.get('growth', '-')))}</td>
            </tr>
            <tr style="border-bottom: 1px solid #f1f5f9;">
              <td style="padding: 14px 4px; color: #1e293b; font-weight: 500;">Gross Profit</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600;">{html.escape(str(gp.get('curr', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; color: #475569;">{html.escape(str(gp.get('prev', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600; color: {growth_color(gp.get('growth', ''))};">{html.escape(str(gp.get('growth', '-')))}</td>
            </tr>
            <tr>
              <td style="padding: 14px 4px; color: #1e293b; font-weight: 500;">Net Profit</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600;">{html.escape(str(np_d.get('curr', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; color: #475569;">{html.escape(str(np_d.get('prev', '-')))}</td>
              <td style="padding: 14px 4px; text-align: right; font-weight: 600; color: {growth_color(np_d.get('growth', ''))};">{html.escape(str(np_d.get('growth', '-')))}</td>
            </tr>
          </tbody>
        </table>

        <div style="margin-top: 16px;">
          <span style="display: inline-block; border: 1px solid #cbd5e1; border-radius: 6px; padding: 4px 12px; font-size: 13px; color: #64748b;">
            {res_type}
          </span>
        </div>
      </div>

      <div style="background: #f8fafc; border-left: 4px solid #2563eb; border-radius: 8px; padding: 16px 18px; margin-bottom: 24px;">
        <h3 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">🏦 Company Debt & Intraday Strength Indicator</h3>
        <p style="margin: 6px 0; font-size: 15px;"><strong>Total Company Debt:</strong> {total_debt}</p>
        <p style="margin: 6px 0; font-size: 15px;"><strong>Debt-to-Equity Ratio:</strong> {de_ratio}</p>
        <p style="margin: 8px 0 0 0; font-size: 15px; color: #334155;"><strong>📊 Intraday & Fundamental View:</strong> {intraday_sig}</p>
      </div>

      <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin-bottom: 24px; text-align: center;">
        <h3 style="margin: 0 0 12px 0; font-size: 18px; color: #0f172a;">📊 1. Multi-Quarter Performance Column Chart</h3>
        <img src="{quarter_chart_url}" alt="{comp_name} Quarterly Column Chart" style="max-width: 100%; height: auto; border-radius: 8px;" />
      </div>

      <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin-bottom: 24px; text-align: center;">
        <h3 style="margin: 0 0 12px 0; font-size: 18px; color: #0f172a;">📈 2. Previous Year vs Current Year Result Chart</h3>
        <img src="{yoy_chart_url}" alt="{comp_name} Previous vs Current Year Column Chart" style="max-width: 100%; height: auto; border-radius: 8px;" />
      </div>

      <h3 style="font-size: 19px; color: #0f172a; margin-bottom: 10px;">📝 {comp_name} {qtr_lbl} Result Highlights</h3>
      {para_html}
      {source_link_html}

      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 22px 0;" />
      <p style="font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Disclaimer:</strong> This report on MarketMunafa is prepared automatically from official BSE corporate filings for educational and informational purposes only. It does not constitute a buy or sell recommendation.
      </p>
    </article>
    """
    return html_body, yoy_chart_url


# ---------------- TELEGRAM GUJARATI ALERT ----------------
def send_telegram_gujarati(data, screener_info, blog_url, source_url, chart_url):
    if not SEND_TO_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", "")))
    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    curr_lbl = html.escape(str(data.get("curr_period_label", "Current")))
    prev_lbl = html.escape(str(data.get("prev_period_label", "Previous")))
    res_type = html.escape(str(data.get("result_type", "Consolidated")))

    rev = data.get("revenue", {"curr": "-", "prev": "-", "growth": "-"})
    gp = data.get("gross_profit", {"curr": "-", "prev": "-", "growth": "-"})
    np_d = data.get("net_profit", {"curr": "-", "prev": "-", "growth": "-"})

    total_debt = html.escape(str(data.get("total_debt") or screener_info["total_debt"]))
    de_ratio = html.escape(str(data.get("debt_to_equity") or screener_info["debt_to_equity"]))
    guj_summary = html.escape(str(data.get("gujarati_summary", "")))
    guj_intraday = html.escape(str(data.get("gujarati_intraday_view", "")))

    msg_html = (
        f"📊 <b>{comp_name} - {qtr_lbl} પરિણામ અપડેટ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>તારીખ:</b> {res_date} | <b>પ્રકાર:</b> {res_type}\n"
        f"💰 <b>શેર ભાવ:</b> {price_disp} | <b>એકમ:</b> રૂ. કરોડ (Rs. Cr.)\n\n"
        f"📈 <b>નાણાકીય પરિણામ ({curr_lbl} vs {prev_lbl}):</b>\n"
        f"• <b>આવક (Revenue):</b> ₹{html.escape(str(rev.get('curr', '-')))} Cr (ગયા વર્ષે: ₹{html.escape(str(rev.get('prev', '-')))} Cr) | ગ્રોથ: <b>{html.escape(str(rev.get('growth', '-')))}</b>\n"
        f"• <b>કુલ નફો (Gross Profit):</b> ₹{html.escape(str(gp.get('curr', '-')))} Cr (ગયા વર્ષે: ₹{html.escape(str(gp.get('prev', '-')))} Cr) | ગ્રોથ: <b>{html.escape(str(gp.get('growth', '-')))}</b>\n"
        f"• <b>ચોખ્ખો નફો (Net Profit):</b> ₹{html.escape(str(np_d.get('curr', '-')))} Cr (ગયા વર્ષે: ₹{html.escape(str(np_d.get('prev', '-')))} Cr) | ગ્રોથ: <b>{html.escape(str(np_d.get('growth', '-')))}</b>\n\n"
        f"🏦 <b>કંપનીનું દેવું અને ઇન્ટ્રાડે સંકેત:</b>\n"
        f"• <b>કુલ દેવું (Total Debt):</b> {total_debt}\n"
        f"• <b>ડેટ-ટુ-ઇક્વિટી (Debt-to-Equity):</b> {de_ratio}\n"
        f"• <b>ઇન્ટ્રાડે વ્યૂ:</b> {guj_intraday}\n\n"
        f"📝 <b>સારાંશ:</b> {guj_summary}\n"
    )

    if blog_url:
        msg_html += f"\n🌐 <a href=\"{html.escape(blog_url, quote=True)}\"><b>બ્લોગ પોસ્ટ અને બંને ચાર્ટ અહીં જુઓ</b></a>"
    if source_url:
        msg_html += f" | <a href=\"{html.escape(source_url, quote=True)}\"><b>BSE PDF</b></a>"
    msg_html += "\n\n✅ <b>MarketMunafa</b>"

    try:
        if len(msg_html) <= 1000 and chart_url:
            photo_res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": chart_url,
                    "caption": msg_html,
                    "parse_mode": "HTML",
                },
                timeout=20,
            )
            if photo_res.status_code == 200:
                log.info("Telegram Gujarati photo+alert sent successfully")
                return

        if chart_url:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": chart_url,
                    "caption": f"📊 {comp_name} ({qtr_lbl}) - ગયા વર્ષ vs ચાલુ વર્ષ સ્તંભ આલેખ",
                },
                timeout=15,
            )

        txt_res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if txt_res.status_code != 200:
            plain_msg = re.sub(r"<[^>]+>", "", msg_html)
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": plain_msg},
                timeout=20,
            )
        log.info("Telegram Gujarati alert sent successfully")
    except Exception as exc:
        log.warning("Telegram send warning: %s", exc)


# ---------------- PUBLISHING & BLOGGER LIVE DEDUP ----------------
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


def get_existing_blogger_posts():
    existing_comp_keys = set()
    existing_file_keys = set()
    if not BLOG_ID:
        return existing_comp_keys, existing_file_keys

    try:
        token = blogger_access_token()
        res = requests.get(
            f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts",
            headers={"Authorization": f"Bearer {token}"},
            params={"maxResults": 50, "fetchBodies": "true"},
            timeout=20,
        )
        if res.status_code == 200:
            for post in res.json().get("items", []):
                c_key = normalize_company_key(post.get("title", ""))
                if c_key:
                    existing_comp_keys.add(c_key)
                content = post.get("content", "")
                for match in re.findall(r"[a-zA-Z0-9_-]{10,}\.(?:pdf|xml|xbrl|html)", content, flags=re.I):
                    existing_file_keys.add(match.lower())
    except Exception as exc:
        log.warning("Could not fetch existing Blogger posts for deduplication: %s", exc)

    return existing_comp_keys, existing_file_keys


def blogger_publish(title, content_html):
    if not BLOG_ID:
        raise RuntimeError("BLOG_ID is missing")
    token = blogger_access_token()
    
    clean_title = re.sub(r"<[^>]+>", "", str(title))
    clean_title = re.sub(r"[\r\n\t]+", " ", clean_title).strip()[:140]
    if not clean_title:
        clean_title = "Corporate Financial Result Update | MarketMunafa"

    payload = {
        "title": clean_title,
        "content": content_html,
        "labels": ["MARKET NEWS", "STOCK RESULT"]
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
    if response.status_code not in (200, 201):
        log.error("Blogger API Error (%s): %s", response.status_code, response.text)
    response.raise_for_status()
    return response.json().get("url", "")


def publish_item(item, ai_data, screener_info, item_id, comp_key, file_key):
    source_url = item["link"]
    raw_title = ai_data.get("seo_title") or f"{item['title']} Q2 Results & Debt Analysis | MarketMunafa"
    blog_html, yoy_chart_url = build_post_html(ai_data, screener_info, source_url)

    blog_url = ""
    if POST_TO_BLOGGER:
        blog_url = blogger_publish(raw_title, blog_html)
        mark_posted(item_id, comp_key, file_key, item["title"], source_url)
        log.info("Blogger post successful: %s", blog_url)
    else:
        log.warning("Blogger publishing is disabled")

    send_telegram_gujarati(ai_data, screener_info, blog_url, source_url, yoy_chart_url)


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
        log.warning("No feed items retrieved.")
        return

    live_comp_keys, live_file_keys = get_existing_blogger_posts()

    seen_comp_in_run = set()
    seen_file_in_run = set()

    for item in all_items:
        doc_type = classify_item(item)
        if not doc_type:
            continue

        comp_key = normalize_company_key(item["title"])
        file_key = extract_file_key(item["link"])
        item_id = make_item_id(comp_key, file_key, item)

        if (
            (comp_key and comp_key in seen_comp_in_run)
            or (file_key and file_key in seen_file_in_run)
            or (comp_key and comp_key in live_comp_keys)
            or (file_key and file_key in live_file_keys)
            or was_posted_in_db(item_id, comp_key, file_key)
        ):
            log.info("Skipping duplicate company/filing: %s", item["title"])
            continue

        if comp_key:
            seen_comp_in_run.add(comp_key)
        if file_key:
            seen_file_in_run.add(file_key)

        log.info("Processing result: %s", item["title"])
        try:
            source_text = get_document_text(item)
            if len(source_text) < MIN_SOURCE_TEXT:
                log.warning("Skipped: insufficient filing text (%d chars): %s", len(source_text), item["title"])
                continue

            screener_info = get_screener_fundamentals(item["title"])
            ai_data = analyze_with_gemini_json(item["title"], source_text, screener_info)
            publish_item(item, ai_data, screener_info, item_id, comp_key, file_key)
            
            if comp_key:
                live_comp_keys.add(comp_key)
            if file_key:
                live_file_keys.add(file_key)
            time.sleep(5)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
