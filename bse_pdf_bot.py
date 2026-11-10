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
from datetime import datetime, timezone, timedelta
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
MIN_SOURCE_TEXT = int(os.getenv("MIN_SOURCE_TEXT", "80"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ---------------- DUPLICATE PROTECTION & STORAGE ----------------
def normalize_company_key(title, doc_type=""):
    clean = re.sub(r"\(\d+\)", "", str(title))
    clean = clean.split("|")[0].split(":")[0].split("-")[0]
    clean = re.sub(
        r"\b(q1|q2|q3|q4|fy\d+|h1|h2|results|result|order|orders|contract|ltd|limited|india|industries|pvt|private|corp|corporation)\b",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"[^a-zA-Z0-9]", "", clean).lower()
    return f"{doc_type}_{clean}" if doc_type and clean else clean


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


def get_url(url, extra_headers=None):
    last_error = None
    profiles = ["chrome120", "chrome110", "edge101", "chrome104"]
    
    for attempt in range(4):
        try:
            profile = profiles[attempt % len(profiles)]
            headers = {
                "Accept": "application/json, application/rss+xml, application/xml, text/xml, application/pdf, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bseindia.com/corporates/ann.html",
                "Origin": "https://www.bseindia.com",
            }
            if extra_headers:
                headers.update(extra_headers)
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
        num = float(cleaned) if cleaned else 0.0
        return int(round(num)) if abs(num - round(num)) < 0.05 else round(num, 2)
    except Exception:
        return 0


# ---------------- SCREENER FUNDAMENTALS ----------------
def get_screener_fundamentals(comp_title):
    clean_name = re.sub(r"\(\d+\)", "", comp_title)
    clean_name = clean_name.split("-")[0].strip()
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", clean_name, flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]
    
    queries = [clean_name]
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    if words:
        queries.append(words[0])

    info = {
        "price_str": "Live on BSE",
        "market_cap": "N/A",
        "pe_ratio": "N/A",
        "pb_ratio": "N/A",
        "industry_pe": "N/A",
        "roe": "N/A",
        "eps": "N/A",
        "div_yield": "N/A",
        "book_value": "N/A",
        "total_debt": "₹0 Cr",
        "curr_debt_num": 0,
        "prev_debt_num": 0,
        "debt_to_equity": "0.00",
        "face_value": "N/A",
        "quarters_text": "",
        "annual_text": "",
        "q_labels": [],
        "q_rev": [],
        "q_gp": [],
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
                        num_span = li.find("span", class_="number")
                        num_txt = num_span.get_text(strip=True) if num_span else ""
                        
                        if "Current Price" in t and num_txt:
                            info["price_str"] = f"₹{num_txt}"
                        elif "Market Cap" in t and num_txt:
                            info["market_cap"] = f"₹{num_txt} Cr"
                        elif ("Stock P/E" in t or t.startswith("P/E")) and num_txt:
                            info["pe_ratio"] = num_txt
                        elif "Book Value" in t and num_txt:
                            info["book_value"] = f"₹{num_txt}"
                        elif "Div Yield" in t and num_txt:
                            info["div_yield"] = f"{num_txt}%"
                        elif "ROE" in t and num_txt:
                            info["roe"] = f"{num_txt}%"
                        elif "Face Value" in t and num_txt:
                            info["face_value"] = num_txt
                        elif ("Debt to equity" in t or t.startswith("Debt to")) and num_txt:
                            info["debt_to_equity"] = num_txt
                        elif "Industry P/E" in t and num_txt:
                            info["industry_pe"] = num_txt
                        elif "EPS" in t and num_txt:
                            info["eps"] = num_txt
                        elif ("P/B" in t or "Price to book" in t) and num_txt:
                            info["pb_ratio"] = num_txt

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
                                    info["curr_debt_num"] = to_float(cols[-1])
                                    info["prev_debt_num"] = to_float(cols[-2]) if len(cols) >= 3 else info["curr_debt_num"]
                                    info["total_debt"] = f"₹{borrowings:,.0f} Cr"
                                except Exception:
                                    pass
                            elif "equity capital" in r_name or "share capital" in r_name or "reserves" in r_name:
                                try:
                                    equity = (equity or 0.0) + float(cols[-1])
                                except Exception:
                                    pass

                if info["debt_to_equity"] in ("N/A", "0.00") and borrowings is not None and equity and equity > 0:
                    info["debt_to_equity"] = f"{(borrowings / equity):.2f}"

                if info["pb_ratio"] == "N/A":
                    p_val = to_float(info["price_str"])
                    bv_val = to_float(info["book_value"])
                    if p_val > 0 and bv_val > 0:
                        info["pb_ratio"] = f"{(p_val / bv_val):.2f}"

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
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "ebitda", "financing profit", "net profit", "eps"]):
                            q_lines.append(" | ".join(cols))
                            nums = [to_float(c_val) for c_val in cols[1:][-6:]]
                            if "sales" in r_title or "revenue" in r_title:
                                info["q_rev"] = nums
                            elif "operating profit" in r_title or "ebitda" in r_title or "financing profit" in r_title:
                                info["q_gp"] = nums
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
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "net profit", "eps"]):
                            pl_lines.append(" | ".join(cols[-5:]))
                            nums = [to_float(c_val) for c_val in cols[1:][-4:]]
                            if "sales" in r_title or "revenue" in r_title:
                                info["y_rev"] = nums
                            elif "net profit" in r_title:
                                info["y_np"] = nums
                            elif "eps" in r_title and info["eps"] == "N/A" and nums:
                                info["eps"] = f"{nums[-1]:.2f}"
                    if pl_headers:
                        info["annual_text"] = "Annual Header: " + " | ".join(pl_headers[-4:]) + "\n" + "\n".join(pl_lines)
                break
        except Exception as exc:
            log.warning("Screener lookup warning for %s: %s", q, exc)
    return info


# ---------------- BSE DIRECT ORDER API & FEED PARSING ----------------
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


def is_mutual_fund_nav_spam(title, description, scripcode=""):
    t_low = (title + " " + description).lower()
    if scripcode.startswith("90") and len(scripcode) >= 6:
        return True
    spam_phrases = (
        "nav as on", "nav as of", "current expense ratio", "monthly portfolio",
        "idcw reinvest", "direct plan - growth", "regular plan - growth",
    )
    return any(p in t_low for p in spam_phrases)


def parse_feed(feed_url, feed_kind):
    response = get_url(feed_url)
    root = ET.fromstring(response.content)
    items = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]
    output = []

    for node in items:
        title = child_text(node, {"title"})
        description = child_text(node, {"description", "summary", "content", "encoded"})
        scrip = child_text(node, {"scripcode"})
        guid = child_text(node, {"guid", "id"})
        links = extract_links_from_xml_item(node)

        if not title or is_mutual_fund_nav_spam(title, description, scrip):
            continue

        link = ""
        direct = [u for u in links if urlparse(u).path.lower().endswith((".pdf", ".xml", ".xbrl", ".html", ".htm"))]
        if direct:
            link = direct[0]
        elif links:
            link = links[0]

        output.append({
            "kind": feed_kind,
            "title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(),
            "description": html.unescape(re.sub(r"<[^>]+>", " ", description)).strip(),
            "link": urljoin(feed_url, link) if link else "",
            "guid": guid or link or title,
            "published": child_text(node, {"pubdate", "published", "updated", "date"}),
        })
        if len(output) >= MAX_ITEMS_PER_FEED:
            break
    return output


def fetch_bse_direct_order_announcements():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_str = ist_now.strftime("%Y%m%d")
    yesterday_str = (ist_now - timedelta(days=1)).strftime("%Y%m%d")

    api_urls = [
        (
            "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
            f"?pageno=1&strCat=Company+Update&strPrevDate={yesterday_str}"
            f"&strScrip=&strSearch=P&strToDate={today_str}&strType=C"
            "&subcategory=Award+of+Order+%2F+Receipt+of+Order"
        ),
        (
            "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
            f"?strCat=Company+Update&strPrevDate={today_str}"
            f"&strScrip=&strSearch=P&strToDate={today_str}&strType=C"
        ),
    ]

    orders = []
    for api_url in api_urls:
        try:
            res = get_url(api_url)
            data = res.json()
            table = data.get("Table", []) if isinstance(data, dict) else []
            for row in table:
                comp_name = str(row.get("SLONGNAME") or "").strip()
                scrip = str(row.get("SCRIP_CD") or "").strip()
                headline = str(row.get("NEWSSUB") or "").strip()
                more_desc = str(row.get("MORE") or "").strip()
                subcat = str(row.get("SUBCATNAME") or "").strip()
                att_name = str(row.get("ATTACHMENTNAME") or "").strip()
                news_id = str(row.get("NEWSID") or "").strip()
                dt_tm = str(row.get("DT_TM") or "").strip()

                full_text_check = f"{headline} {more_desc} {subcat}".lower()
                if "order" not in full_text_check and "contract" not in full_text_check and "award" not in full_text_check and "loa" not in full_text_check:
                    continue

                pdf_link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att_name}" if att_name else ""
                display_title = f"{comp_name} ({scrip})" if comp_name and scrip else (comp_name or headline)
                full_desc = f"{headline}. {more_desc}".strip()

                orders.append({
                    "kind": "order",
                    "title": display_title,
                    "description": full_desc,
                    "link": pdf_link,
                    "guid": news_id or pdf_link or display_title,
                    "published": dt_tm,
                })
        except Exception as exc:
            log.warning("Direct BSE Order API check warning: %s", exc)
    return orders


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


def get_document_text(item):
    parts = []
    if item["description"]:
        parts.append("FEED DESCRIPTION:\n" + item["description"])
    url = item["link"]
    if url:
        path = urlparse(url).path.lower()
        try:
            if path.endswith(".pdf"):
                extracted = extract_pdf_text(url)
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
ORDER_WORDS = (
    "award_of_order", "receipt_of_order", "award of order", "receipt of order",
    "receiving of orders", "received an order", "work order", "purchase order",
    "letter of acceptance", "letter of award", "loa from", "contract worth",
    "order worth", "orders worth", "bagged an order", "secured an order",
    "arbitral award", "orders / contracts", "order / contract",
)


def classify_item(item):
    if item["kind"] == "order":
        return "order"
    title_desc = (item["title"] + " " + item["description"]).lower()
    if any(word in title_desc for word in ORDER_WORDS):
        return "order"
    if "prior intimation" in title_desc or "is scheduled on" in title_desc:
        if item["kind"] != "result":
            return ""
    if item["kind"] == "result" or any(word in title_desc for word in RESULT_WORDS):
        return "result"
    return ""


def make_item_id(comp_key, file_key, item):
    basis = file_key or comp_key or item["guid"] or item["title"]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def analyze_with_gemini_json(company_title, doc_type, source_text, screener_info):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    prompt = f"""
You are the Chief Financial Analyst for 'MarketMunafa'.
Analyze the following BSE corporate filing ({doc_type.upper()}) and Screener fundamentals data.
IMPORTANT RULE: All English fields MUST be 100% in English (no Gujarati). Only 'gujarati_summary' and 'gujarati_intraday_view' should be in Gujarati.

Company Headline: {company_title}
Document Type: {doc_type}
Screener Fundamentals (Before Filing):
- Market Cap: {screener_info['market_cap']}
- Current Price: {screener_info['price_str']}
- P/E Ratio (TTM): {screener_info['pe_ratio']}
- P/B Ratio: {screener_info['pb_ratio']}
- Industry P/E: {screener_info['industry_pe']}
- ROE: {screener_info['roe']}
- EPS (TTM): {screener_info['eps']}
- Total Debt: {screener_info['curr_debt_num']} Cr (Previous: {screener_info['prev_debt_num']} Cr)
- Debt-to-Equity: {screener_info['debt_to_equity']}

Tasks:
If Document Type is 'result', extract Revenue, Gross Profit, Net Profit for current vs previous period.
If Document Type is 'order', fill 'order_details' with exact Client Name, Order Value, Scope of Work, and Timeline.
Calculate the Estimated Stock Return / Target Boost percentage in English (e.g., "+6.0% to +10.5% Bullish Upside").

Return ONLY a valid JSON object:
{{
  "seo_title": "Catchy Google News SEO Title in 100% English",
  "company_short_name": "Clean Company Name in English",
  "result_date": "Date of filing in English",
  "price_display": "{screener_info['price_str']}",
  "quarter_label": "e.g. Q2 FY26-27",
  "curr_period_label": "e.g. Sep 26",
  "prev_period_label": "e.g. Sep 25",
  "result_type": "Consolidated or Standalone",
  "revenue": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "gross_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "net_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "order_details": {{
    "client_or_awarder": "Client or Subsidiary Name (for order filings)",
    "order_value": "Order / Award Value in Rs. Cr.",
    "scope_of_work": "Brief description of order",
    "execution_timeline": "Execution timeline"
  }},
  "market_cap": "{screener_info['market_cap']}",
  "pe_ratio": "Updated P/E Ratio after filing (or {screener_info['pe_ratio']})",
  "pb_ratio": "{screener_info['pb_ratio']}",
  "industry_pe": "{screener_info['industry_pe']}",
  "roe": "{screener_info['roe']}",
  "eps": "{screener_info['eps']}",
  "div_yield": "{screener_info['div_yield']}",
  "book_value": "{screener_info['book_value']}",
  "total_debt": "{screener_info['total_debt']}",
  "debt_to_equity": "{screener_info['debt_to_equity']}",
  "face_value": "{screener_info['face_value']}",
  "fundamental_impact_note": "1-2 sentences in 100% English explaining what changed.",
  "estimated_stock_return": "Estimated % return in English (e.g. +8.0% to +12.5% Short-Term Bullish)",
  "intraday_signal": "1-2 sentences in 100% English explaining intraday/positional sentiment.",
  "quarter_chart_labels": ["Jun 25", "Sep 25", "Dec 25", "Mar 26", "Jun 26", "Sep 26"],
  "quarter_chart_revenue": [100, 110, 115, 120, 125, 135],
  "quarter_chart_net_profit": [10, 12, 11, 14, 15, 18],
  "yearly_chart_labels": ["FY25", "FY26"],
  "yearly_chart_revenue": [450, 520],
  "yearly_chart_net_profit": [48, 62],
  "seo_article_paragraphs": [
    "Paragraph 1 in 100% English: Journalistic breakdown of the financial result OR new order receipt.",
    "Paragraph 2 in 100% English: Fundamental valuation analysis (P/E, ROE, Debt)."
  ],
  "gujarati_summary": "2-3 sentences in natural Gujarati summarizing the result or new order.",
  "gujarati_intraday_view": "1-2 sentences in natural Gujarati explaining debt, valuation, and expected return."
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


# ---------------- VISUAL CHARTS BUILDER (WITH NUMBERS ON TOP) ----------------
def build_quarterly_column_chart_url(comp_name, labels, rev_list, gp_list, np_list):
    datasets = [
        {
            "label": "Revenue",
            "backgroundColor": "#1b5e20",
            "data": rev_list,
        },
        {
            "label": "Net Profit",
            "backgroundColor": "#2563eb",
            "data": np_list,
        },
    ]
    if gp_list and any(to_float(x) != 0 for x in gp_list):
        datasets.insert(
            1,
            {
                "label": "Gross Profit",
                "backgroundColor": "#90a4ae",
                "data": gp_list,
            },
        )

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": datasets,
        },
        "options": {
            "layout": {"padding": {"top": 24, "right": 12, "left": 8, "bottom": 8}},
            "title": {
                "display": True,
                "text": f"{comp_name} - Multi-Quarter Trend (Rs.Cr.)",
                "fontSize": 16,
                "fontColor": "#334155",
                "padding": 14,
            },
            "legend": {
                "display": True,
                "position": "top",
                "labels": {"fontSize": 12, "fontColor": "#334155"},
            },
            "scales": {
                "yAxes": [{"ticks": {"beginAtZero": True, "padding": 6}}],
                "xAxes": [{"ticks": {"fontSize": 12, "fontColor": "#334155"}}],
            },
            "plugins": {
                "datalabels": {
                    "display": True,
                    "anchor": "end",
                    "align": "top",
                    "offset": 2,
                    "clip": False,
                    "color": "#000000",
                    "font": {"weight": "bold", "size": 11},
                }
            },
        },
    }
    encoded = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={encoded}"


def build_yoy_comparison_chart_url(comp_name, qtr_lbl, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp):
    r_curr, r_prev = to_float(rev.get("curr", 0)), to_float(rev.get("prev", 0))
    gp_curr, gp_prev = to_float(gp.get("curr", 0)), to_float(gp.get("prev", 0))
    np_curr, np_prev = to_float(np_d.get("curr", 0)), to_float(np_d.get("prev", 0))
    d_curr, d_prev = to_float(debt_comp.get("curr", 0)), to_float(debt_comp.get("prev", 0))

    labels = ["Revenue", "Gross Profit", "Net Profit", "Total Debt"]
    curr_data = [r_curr, gp_curr, np_curr, d_curr]
    prev_data = [r_prev, gp_prev, np_prev, d_prev]

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": str(curr_lbl),
                    "backgroundColor": "#1b5e20",
                    "data": curr_data,
                },
                {
                    "label": str(prev_lbl),
                    "backgroundColor": "#90a4ae",
                    "data": prev_data,
                },
            ],
        },
        "options": {
            "layout": {"padding": {"top": 26, "right": 14, "left": 10, "bottom": 8}},
            "title": {
                "display": True,
                "text": f"{comp_name} - {qtr_lbl} (Rs.Cr.)",
                "fontSize": 17,
                "fontColor": "#475569",
                "padding": 14,
            },
            "legend": {
                "display": True,
                "position": "top",
                "labels": {"fontSize": 13, "fontColor": "#475569"},
            },
            "scales": {
                "yAxes": [{"ticks": {"beginAtZero": True, "padding": 6}}],
                "xAxes": [{"ticks": {"fontSize": 13, "fontColor": "#475569"}}],
            },
            "plugins": {
                "datalabels": {
                    "display": True,
                    "anchor": "end",
                    "align": "top",
                    "offset": 2,
                    "clip": False,
                    "color": "#000000",
                    "font": {"weight": "bold", "size": 12},
                }
            },
        },
    }
    encoded = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={encoded}"


def build_post_html(data, doc_type, screener_info, source_url):
    """ઓર્ડર માટે ફક્ત ઓર્ડર અને ફંડામેન્ટલ, રિઝલ્ટ માટે ચાર્ટ અને રિઝલ્ટ ટેબલ"""
    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", datetime.now().strftime("%B %d, %Y"))))
    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    curr_lbl = html.escape(str(data.get("curr_period_label", "Sep 26")))
    prev_lbl = html.escape(str(data.get("prev_period_label", "Sep 25")))
    res_type = html.escape(str(data.get("result_type", "Consolidated")))

    mkt_cap = html.escape(str(data.get("market_cap") or screener_info["market_cap"]))
    pe_ratio = html.escape(str(data.get("pe_ratio") or screener_info["pe_ratio"]))
    pb_ratio = html.escape(str(data.get("pb_ratio") or screener_info["pb_ratio"]))
    ind_pe = html.escape(str(data.get("industry_pe") or screener_info["industry_pe"]))
    roe = html.escape(str(data.get("roe") or screener_info["roe"]))
    eps = html.escape(str(data.get("eps") or screener_info["eps"]))
    div_yield = html.escape(str(data.get("div_yield") or screener_info["div_yield"]))
    book_value = html.escape(str(data.get("book_value") or screener_info["book_value"]))
    total_debt = html.escape(str(data.get("total_debt") or screener_info["total_debt"]))
    de_ratio = html.escape(str(data.get("debt_to_equity") or screener_info["debt_to_equity"]))
    face_val = html.escape(str(data.get("face_value") or screener_info["face_value"]))
    fund_impact = html.escape(str(data.get("fundamental_impact_note", "")))
    est_return = html.escape(str(data.get("estimated_stock_return", "N/A")))
    intraday_sig = html.escape(str(data.get("intraday_signal", "")))

    thumb_prompt = f"{comp_name} corporate financial update stock market india"
    thumb_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(thumb_prompt)}?width=800&height=450&nologo=true"

    paragraphs = data.get("seo_article_paragraphs", [])
    para_html = "".join(f"<p style='color:#334155; font-size:16px; line-height:1.7; margin-bottom:14px;'>{html.escape(p)}</p>" for p in paragraphs)

    source_link_html = (
        f'<p style="margin-top:16px;"><a href="{html.escape(source_url, quote=True)}" '
        f'target="_blank" rel="nofollow noopener" style="color:#2563eb; font-weight:600; text-decoration:none;">'
        f'📄 View Official BSE Filing PDF</a></p>'
        if source_url else ""
    )

    content_blocks = []

    # 1. ORDER LAYOUT (No result charts or empty result tables)
    if doc_type == "order":
        ord_info = data.get("order_details", {}) or {}
        client_str = html.escape(str(ord_info.get("client_or_awarder", "Disclosed in Filing")))
        val_str = html.escape(str(ord_info.get("order_value", "As per BSE Filing")))
        scope_str = html.escape(str(ord_info.get("scope_of_work", "Receipt of new commercial order/contract")))
        time_str = html.escape(str(ord_info.get("execution_timeline", "As per contract terms")))

        order_card = f"""
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 16px; padding: 22px; margin-bottom: 24px;">
          <h3 style="margin: 0 0 16px 0; font-size: 20px; color: #1e3a8a;">🏆 New Order / Contract Receipt</h3>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Client / Awarding Entity:</strong> {client_str}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Order / Award Value:</strong> <span style="color:#16a34a; font-weight:700;">{val_str}</span></p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Nature of Order / Scope:</strong> {scope_str}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Execution Timeline:</strong> {time_str}</p>
        </div>
        """
        content_blocks.append(order_card)
        yoy_chart_url = ""

    # 2. RESULT LAYOUT (Full tables and charts)
    else:
        rev = data.get("revenue", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        gp = data.get("gross_profit", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        np_d = data.get("net_profit", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        debt_comp = data.get("debt_comparison") or {"curr": screener_info["curr_debt_num"], "prev": screener_info["prev_debt_num"]}

        q_labels = data.get("quarter_chart_labels") or screener_info["q_labels"] or [prev_lbl, curr_lbl]
        q_rev = data.get("quarter_chart_revenue") or screener_info["q_rev"] or [to_float(rev.get("prev")), to_float(rev.get("curr"))]
        q_gp = data.get("quarter_chart_gross_profit") or screener_info["q_gp"] or []
        q_np = data.get("quarter_chart_net_profit") or screener_info["q_np"] or [to_float(np_d.get("prev")), to_float(np_d.get("curr"))]
        
        quarter_chart_url = build_quarterly_column_chart_url(comp_name, q_labels, q_rev, q_gp, q_np)
        yoy_chart_url = build_yoy_comparison_chart_url(comp_name, qtr_lbl, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp)

        def growth_color(growth_str):
            return "#dc2626" if "-" in str(growth_str) else "#16a34a"

        result_card = f"""
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 22px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
          <div style="display: flex; justify-content: space-between; color: #64748b; font-size: 14px; margin-bottom: 10px;">
            <span>{res_date}</span>
            <span>Rs.Cr.</span>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px;">
            <strong style="font-size: 22px; color: #0f172a;">{comp_name}</strong>
            <span style="font-size: 20px; font-weight: 600; color: #0f172a;">{price_disp}</span>
          </div>
          <table style="width: 100%; border-collapse: collapse; font-size: 15px;">
            <thead>
              <tr style="border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #64748b; font-weight: 500;">
                <th style="text-align: left; padding: 10px 4px;">{qtr_lbl}</th>
                <th style="text-align: right; padding: 10px 4px;">{curr_lbl}</th>
                <th style="text-align: right; padding: 10px 4px;">{prev_lbl}</th>
                <th style="text-align: right; padding: 10px 4px;">Difference</th>
                <th style="text-align: right; padding: 10px 4px;">Growth</th>
              </tr>
            </thead>
            <tbody>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500;">Revenue</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600;">{html.escape(str(rev.get('curr', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569;">{html.escape(str(rev.get('prev', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a;">{html.escape(str(rev.get('difference', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: {growth_color(rev.get('growth', ''))};">{html.escape(str(rev.get('growth', '-')))}</td>
              </tr>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500;">Gross Profit</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600;">{html.escape(str(gp.get('curr', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569;">{html.escape(str(gp.get('prev', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a;">{html.escape(str(gp.get('difference', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: {growth_color(gp.get('growth', ''))};">{html.escape(str(gp.get('growth', '-')))}</td>
              </tr>
              <tr>
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500;">Net Profit</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600;">{html.escape(str(np_d.get('curr', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569;">{html.escape(str(np_d.get('prev', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a;">{html.escape(str(np_d.get('difference', '-')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: {growth_color(np_d.get('growth', ''))};">{html.escape(str(np_d.get('growth', '-')))}</td>
              </tr>
            </tbody>
          </table>
          <div style="margin-top: 16px;">
            <span style="display: inline-block; border: 1px solid #cbd5e1; border-radius: 6px; padding: 4px 12px; font-size: 13px; color: #64748b;">
              {res_type}
            </span>
          </div>
        </div>
        
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center; box-shadow: 0 4px 14px rgba(0,0,0,0.04);">
          <h3 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📈 Previous vs Current Year Result & Debt Chart</h3>
          <img src="{yoy_chart_url}" alt="YoY Column Chart" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center; box-shadow: 0 4px 14px rgba(0,0,0,0.04);">
          <h3 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📊 Multi-Quarter Performance Column Chart</h3>
          <img src="{quarter_chart_url}" alt="Quarterly Column Chart" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        """
        content_blocks.append(result_card)

    # 3. COMMON FUNDAMENTALS BLOCK (For both Results and Orders)
    fundamentals_card = f"""
    <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 22px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
      <h3 style="margin: 0 0 16px 0; font-size: 20px; color: #0f172a;">Fundamentals (All 10 Indicators)</h3>
      <table style="width: 100%; border-collapse: collapse; font-size: 15px;">
        <tbody>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 6px; color: #64748b;">Mkt Cap</td>
            <td style="padding: 10px 12px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{mkt_cap}</td>
            <td style="padding: 10px 6px 10px 12px; color: #64748b; border-left: 1px solid #f1f5f9;">ROE</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{roe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 6px; color: #64748b;">P/E Ratio(TTM)</td>
            <td style="padding: 10px 12px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pe_ratio}</td>
            <td style="padding: 10px 6px 10px 12px; color: #64748b; border-left: 1px solid #f1f5f9;">EPS(TTM)</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{eps}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 6px; color: #64748b;">P/B Ratio</td>
            <td style="padding: 10px 12px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pb_ratio}</td>
            <td style="padding: 10px 6px 10px 12px; color: #64748b; border-left: 1px solid #f1f5f9;">Div Yield</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{div_yield}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 6px; color: #64748b;">Industry P/E</td>
            <td style="padding: 10px 12px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{ind_pe}</td>
            <td style="padding: 10px 6px 10px 12px; color: #64748b; border-left: 1px solid #f1f5f9;">Book Value</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{book_value}</td>
          </tr>
          <tr>
            <td style="padding: 10px 6px; color: #64748b;">Debt to Equity</td>
            <td style="padding: 10px 12px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{de_ratio}</td>
            <td style="padding: 10px 6px 10px 12px; color: #64748b; border-left: 1px solid #f1f5f9;">Face Value</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{face_val}</td>
          </tr>
        </tbody>
      </table>

      <p style="margin: 12px 0 6px 0; font-size: 14px; color: #475569;">
        <strong>Total Company Debt:</strong> {total_debt} | <strong>Post-Filing Impact:</strong> {fund_impact}
      </p>

      <div style="background: #d1fae5; border: 1px solid #10b981; padding: 12px 14px; border-radius: 8px; font-size: 16px; margin-top: 12px;">
        🎯 <strong>Estimated Stock Return / Target Boost:</strong> <span style="color: #065f46; font-weight: 700;">{est_return}</span>
      </div>
      <p style="margin: 10px 0 0 0; font-size: 14px; color: #334155;"><strong>Intraday & Positional View:</strong> {intraday_sig}</p>
    </div>
    """
    content_blocks.append(fundamentals_card)

    html_body = f"""
    <article style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 680px; margin: 0 auto; color: #1e293b;">
      <figure style="margin: 0 0 22px 0; text-align: center;">
        <img src="{thumb_url}" alt="{comp_name} Corporate Update" style="width: 100%; max-height: 360px; object-fit: cover; border-radius: 12px;" />
        <figcaption style="font-size: 13px; color: #64748b; margin-top: 6px;">{comp_name} Corporate Filing | MarketMunafa</figcaption>
      </figure>

      {"".join(content_blocks)}

      <h3 style="font-size: 19px; color: #0f172a; margin-bottom: 10px;">📝 Detailed Corporate & Valuation Analysis</h3>
      {para_html}
      {source_link_html}

      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 22px 0;" />
      <p style="font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Disclaimer:</strong> This report on MarketMunafa is prepared automatically from official BSE corporate filings and Screener data for educational and informational purposes only. It is not investment advice.
      </p>
    </article>
    """
    return html_body, yoy_chart_url


# ---------------- TELEGRAM GUJARATI ALERT ----------------
def send_telegram_gujarati(data, doc_type, screener_info, blog_url, source_url, chart_url):
    if not SEND_TO_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", "")))
    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    curr_lbl = html.escape(str(data.get("curr_period_label", "Current")))
    prev_lbl = html.escape(str(data.get("prev_period_label", "Previous")))

    mkt_cap = html.escape(str(data.get("market_cap") or screener_info["market_cap"]))
    pe_ratio = html.escape(str(data.get("pe_ratio") or screener_info["pe_ratio"]))
    pb_ratio = html.escape(str(data.get("pb_ratio") or screener_info["pb_ratio"]))
    ind_pe = html.escape(str(data.get("industry_pe") or screener_info["industry_pe"]))
    roe = html.escape(str(data.get("roe") or screener_info["roe"]))
    eps = html.escape(str(data.get("eps") or screener_info["eps"]))
    div_yield = html.escape(str(data.get("div_yield") or screener_info["div_yield"]))
    book_value = html.escape(str(data.get("book_value") or screener_info["book_value"]))
    total_debt = html.escape(str(data.get("total_debt") or screener_info["total_debt"]))
    de_ratio = html.escape(str(data.get("debt_to_equity") or screener_info["debt_to_equity"]))
    face_val = html.escape(str(data.get("face_value") or screener_info["face_value"]))
    est_return = html.escape(str(data.get("estimated_stock_return", "N/A")))
    guj_summary = html.escape(str(data.get("gujarati_summary", "")))
    guj_intraday = html.escape(str(data.get("gujarati_intraday_view", "")))

    if doc_type == "order":
        header_title = f"🏆 <b>{comp_name} - નવો ઓર્ડર / કોન્ટ્રેક્ટ અપડેટ</b>"
        ord_info = data.get("order_details", {}) or {}
        dynamic_block_gu = (
            f"📦 <b>ઓર્ડરની મુખ્ય વિગતો:</b>\n"
            f"• <b>ક્લાયન્ટ / કંપની:</b> {html.escape(str(ord_info.get('client_or_awarder', '-')))}\n"
            f"• <b>ઓર્ડરનું મૂલ્ય:</b> <b>{html.escape(str(ord_info.get('order_value', '-')))}</b>\n"
            f"• <b>કામની વિગત:</b> {html.escape(str(ord_info.get('scope_of_work', '-')))}\n\n"
        )
    else:
        header_title = f"📊 <b>{comp_name} - {qtr_lbl} રિઝલ્ટ અપડેટ</b>"
        rev = data.get("revenue", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        gp = data.get("gross_profit", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        np_d = data.get("net_profit", {"curr": "-", "prev": "-", "growth": "-", "difference": "-"})
        dynamic_block_gu = (
            f"📈 <b>નાણાકીય કામગીરી ({curr_lbl} vs {prev_lbl}):</b>\n"
            f"• <b>આવક (Revenue):</b> ₹{html.escape(str(rev.get('curr', '-')))} Cr (તફાવત: {html.escape(str(rev.get('difference', '-')))}) | ગ્રોથ: <b>{html.escape(str(rev.get('growth', '-')))}</b>\n"
            f"• <b>કુલ નફો (Gross Profit):</b> ₹{html.escape(str(gp.get('curr', '-')))} Cr (તફાવત: {html.escape(str(gp.get('difference', '-')))}) | ગ્રોથ: <b>{html.escape(str(gp.get('growth', '-')))}</b>\n"
            f"• <b>ચોખ્ખો નફો (Net Profit):</b> ₹{html.escape(str(np_d.get('curr', '-')))} Cr (તફાવત: {html.escape(str(np_d.get('difference', '-')))}) | ગ્રોથ: <b>{html.escape(str(np_d.get('growth', '-')))}</b>\n\n"
        )

    msg_html = (
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>તારીખ:</b> {res_date} | 💰 <b>ભાવ:</b> {price_disp}\n\n"
        f"{dynamic_block_gu}"
        f"🔍 <b>તમામ ૧૦ ફંડામેન્ટલ ઇન્ડિકેટર્સ:</b>\n"
        f"• <b>Mkt Cap:</b> {mkt_cap} | <b>ROE:</b> {roe}\n"
        f"• <b>P/E Ratio:</b> {pe_ratio} | <b>EPS (TTM):</b> {eps}\n"
        f"• <b>P/B Ratio:</b> {pb_ratio} | <b>Div Yield:</b> {div_yield}\n"
        f"• <b>Industry P/E:</b> {ind_pe} | <b>Book Value:</b> {book_value}\n"
        f"• <b>Debt to Equity:</b> {de_ratio} (Debt: {total_debt}) | <b>Face Value:</b> {face_val}\n\n"
        f"🎯 <b>અંદાજિત શેર રીટર્ન / બૂસ્ટ:</b> <b>{est_return}</b>\n\n"
        f"🏦 <b>ઇન્ટ્રાડે વ્યુ:</b> {guj_intraday}\n"
        f"📝 <b>સારાંશ:</b> {guj_summary}\n"
    )

    if blog_url:
        msg_html += f"\n🌐 <a href=\"{html.escape(blog_url, quote=True)}\"><b>સંપૂર્ણ પોસ્ટ અને ચાર્ટ બ્લોગ પર જુઓ</b></a>"
    if source_url:
        msg_html += f" | <a href=\"{html.escape(source_url, quote=True)}\"><b>BSE PDF</b></a>"
    msg_html += "\n\n✅ <b>MarketMunafa</b>"

    try:
        if chart_url and doc_type != "order":
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": chart_url,
                    "caption": f"📊 {comp_name} - પરિણામ અને ગ્રોથ ચાર્ટ",
                },
                timeout=15,
            )

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
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
                title_str = post.get("title", "")
                p_type = "order" if "order" in title_str.lower() or "contract" in title_str.lower() or "award" in title_str.lower() else "result"
                c_key = normalize_company_key(title_str, p_type)
                if c_key:
                    existing_comp_keys.add(c_key)
                content = post.get("content", "")
                for match in re.findall(r"[a-zA-Z0-9_-]{10,}\.(?:pdf|xml|xbrl|html)", content, flags=re.I):
                    existing_file_keys.add(match.lower())
    except Exception as exc:
        log.warning("Could not fetch existing Blogger posts: %s", exc)

    return existing_comp_keys, existing_file_keys


def blogger_publish(title, content_html, doc_type):
    if not BLOG_ID:
        raise RuntimeError("BLOG_ID is missing")
    token = blogger_access_token()
    
    clean_title = re.sub(r"<[^>]+>", "", str(title))
    clean_title = re.sub(r"[\r\n\t]+", " ", clean_title).strip()[:140]
    if not clean_title:
        clean_title = "Corporate Financial Update | MarketMunafa"

    labels = ["MARKET NEWS", "SHARE ORDER"] if doc_type == "order" else ["MARKET NEWS", "STOCK RESULT"]

    payload = {
        "title": clean_title,
        "content": content_html,
        "labels": labels
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


def publish_item(item, doc_type, ai_data, screener_info, item_id, comp_key, file_key):
    source_url = item["link"]
    raw_title = ai_data.get("seo_title") or f"{item['title']} Corporate Update & Valuation | MarketMunafa"
    blog_html, yoy_chart_url = build_post_html(ai_data, doc_type, screener_info, source_url)

    blog_url = ""
    if POST_TO_BLOGGER:
        blog_url = blogger_publish(raw_title, blog_html, doc_type)
        mark_posted(item_id, comp_key, file_key, item["title"], source_url)
        log.info("Blogger post successful (%s): %s", doc_type, blog_url)

    send_telegram_gujarati(ai_data, doc_type, screener_info, blog_url, source_url, yoy_chart_url)


# ---------------- MAIN ----------------
def run():
    init_db()
    all_items = []

    direct_orders = fetch_bse_direct_order_announcements()
    log.info("Direct BSE Order API: %d items", len(direct_orders))
    all_items.extend(direct_orders)

    for kind, feed_url in FEEDS.items():
        try:
            items = parse_feed(feed_url, kind)
            log.info("%s feed (filtered): %d items", kind, len(items))
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

        comp_key = normalize_company_key(item["title"], doc_type)
        file_key = extract_file_key(item["link"])
        item_id = make_item_id(comp_key, file_key, item)

        if (
            (comp_key and comp_key in seen_comp_in_run)
            or (file_key and file_key in seen_file_in_run)
            or (comp_key and comp_key in live_comp_keys)
            or (file_key and file_key in live_file_keys)
            or was_posted_in_db(item_id, comp_key, file_key)
        ):
            log.info("Skipping duplicate (%s): %s", doc_type, item["title"])
            continue

        if comp_key:
            seen_comp_in_run.add(comp_key)
        if file_key:
            seen_file_in_run.add(file_key)

        log.info("Processing %s: %s", doc_type, item["title"])
        try:
            source_text = get_document_text(item)
            if len(source_text) < MIN_SOURCE_TEXT:
                log.warning("Skipped: insufficient filing text (%d chars): %s", len(source_text), item["title"])
                continue

            screener_info = get_screener_fundamentals(item["title"])
            ai_data = analyze_with_gemini_json(item["title"], doc_type, source_text, screener_info)
            publish_item(item, doc_type, ai_data, screener_info, item_id, comp_key, file_key)
            
            if comp_key:
                live_comp_keys.add(comp_key)
            if file_key:
                live_file_keys.add(file_key)
            time.sleep(5)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
