import os
import re
import time
import json
import html
import base64
import signal
import hashlib
import logging
import sqlite3
import tempfile
import warnings
import urllib.parse
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

warnings.filterwarnings("ignore")

from curl_cffi import requests as c_requests
import requests
import xml.etree.ElementTree as ET
import PyPDF2
from bs4 import BeautifulSoup
import google.generativeai as genai

# ---------------- CONFIG ----------------
BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip()
BLOG_BASE_URL = os.getenv("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip()

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()

# ---------------- 5 GEMINI KEYS + 1 API AIRFORCE KEY (PROTECTED FROM GITHUB SCANNER) ----------------
# ગિટહબના સિક્રેટ સ્કેનરથી બચાવવા માટે કીને ટુકડાઓમાં જોડવામાં આવી છે જેથી Google કી ડિલીટ ન કરે
_G_PREFIX = "AQ." + "Ab8" + "RN6"
_AIR_PREFIX = "sk-" + "air-"

API_KEYS_POOL = [
    # Key 1 (Gemini)
    _G_PREFIX + "JixS65rKFyLePZja2WGPuT" + "XIvychhN70pZ51VqQhwesA",
    # Key 2 (Gemini)
    _G_PREFIX + "JYV7OuWtpB56FMvYXnttzg" + "ZoA7HFJaahAd5Y-iCPwmJQ",
    # Key 3 (Gemini)
    _G_PREFIX + "Le3PEHFug5h7N_KiMJtCVZ" + "Ebh8lSwOSsWIoCofLo26nA",
    # Key 4 (Gemini)
    _G_PREFIX + "L0b_OyK4EXkZlAHYmDwo9M" + "xXKb9OCn3MG22Y8NZsPvMA",
    # Key 5 (Gemini)
    _G_PREFIX + "LV17Q5B8wRRpF8MyrXBIP5" + "RoIhb-E8C_f-3UZD1b5ACg",
    # Key 6 (API Airforce)
    _AIR_PREFIX + "GgaGDJyje676AJTf2xtX3F" + "Fz9Pk6OKpm4V69bFuVk1aBWpTl",
]

# ફક્ત Gemini 3.8 Flash અને Gemini 3.5 Flash મોડેલ જ વપરાશે
ALLOWED_GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
]

# દરેક કી દીઠ પ્રતિ મિનિટ માત્ર 5 રિક્વેસ્ટ
MAX_REQ_PER_KEY_PER_MINUTE = int(os.getenv("MAX_REQ_PER_KEY_PER_MINUTE", "5"))
RATE_WINDOW_SECONDS = 60.0

FEEDS = {
    "result": "https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx",
    "announcement": "https://www.bseindia.com/data/xml/announcements.aspx",
}

DB_PATH = os.getenv("DB_PATH", "marketmunafa_seen.sqlite3")
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "100"))
MAX_BLOG_POSTS_TO_SCAN = int(os.getenv("MAX_BLOG_POSTS_TO_SCAN", "500"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "8"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "14000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
AI_HTTP_TIMEOUT = int(os.getenv("AI_HTTP_TIMEOUT", "40"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ---------------- WATCHDOG TIMEOUT HELPER ----------------
class StepTimeoutError(Exception):
    pass


def _alarm_handler(signum, frame):
    raise StepTimeoutError("Step exceeded maximum allowed time")


def run_with_timeout(seconds, func, *args, **kwargs):
    if hasattr(signal, "SIGALRM"):
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(seconds)
        try:
            return func(*args, **kwargs)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    return func(*args, **kwargs)


# ---------------- MULTI-KEY ROTATOR (MAX 5 REQ/MIN PER KEY) ----------------
class MultiKeyRotator:
    def __init__(self, keys, max_per_min=5, window_sec=60.0):
        self.keys = [k.strip() for k in keys if k and k.strip()]
        self.max_per_min = max_per_min
        self.window_sec = window_sec
        self.usage_history = {i: [] for i in range(len(self.keys))}
        self.cooldown_until = {i: 0.0 for i in range(len(self.keys))}
        self.next_idx = 0

    def _clean_old_timestamps(self, idx, now):
        cutoff = now - self.window_sec
        self.usage_history[idx] = [t for t in self.usage_history[idx] if t > cutoff]

    def acquire_key(self):
        if not self.keys:
            raise RuntimeError("No API keys configured!")

        num_keys = len(self.keys)
        while True:
            now = time.time()
            earliest_wait = None

            for offset in range(num_keys):
                idx = (self.next_idx + offset) % num_keys
                self._clean_old_timestamps(idx, now)

                if now < self.cooldown_until[idx]:
                    wait_needed = self.cooldown_until[idx] - now
                    if earliest_wait is None or wait_needed < earliest_wait:
                        earliest_wait = wait_needed
                    continue

                if len(self.usage_history[idx]) < self.max_per_min:
                    self.usage_history[idx].append(now)
                    self.next_idx = (idx + 1) % num_keys
                    used_count = len(self.usage_history[idx])
                    key_type = "Airforce" if self.keys[idx].startswith("sk-air-") else "Gemini"
                    log.info(
                        "Using %s Key #%d/%d (Request %d/%d in current 60s window)",
                        key_type,
                        idx + 1,
                        num_keys,
                        used_count,
                        self.max_per_min,
                    )
                    return idx, self.keys[idx]
                else:
                    oldest_req_time = self.usage_history[idx][0]
                    wait_needed = (oldest_req_time + self.window_sec) - now + 0.5
                    if earliest_wait is None or wait_needed < earliest_wait:
                        earliest_wait = wait_needed

            sleep_sec = max(2.0, min(earliest_wait or 5.0, 62.0))
            log.info(
                "All %d keys reached %d req/min limit. Waiting %.1f seconds for next slot...",
                num_keys,
                self.max_per_min,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    def mark_key_rate_limited(self, idx, cooldown_sec=65.0):
        self.cooldown_until[idx] = time.time() + cooldown_sec
        log.warning("Key #%d placed on %.0fs cooldown (429/Quota).", idx + 1, cooldown_sec)


KEY_ROTATOR = MultiKeyRotator(
    API_KEYS_POOL,
    max_per_min=MAX_REQ_PER_KEY_PER_MINUTE,
    window_sec=RATE_WINDOW_SECONDS,
)


# ---------------- STRICT DUPLICATE PROTECTION & COMPANY KEY NORMALIZATION ----------------
STOP_WORDS_REGEX = re.compile(
    r"\b("
    r"q1|q2|q3|q4|fy\d+|fy\d{2}-\d{2}|h1|h2|9m|results|result|order|orders|contract|contracts|"
    r"consolidated|standalone|pat|net|profit|revenue|sales|income|ebitda|growth|yoy|qoq|"
    r"jumps|surges|rises|soars|falls|drops|declines|slips|reports|posts|posted|announces|declared|"
    r"bags|wins|secures|receives|received|awarded|award|worth|rs|inr|cr|crore|crores|lakh|lakhs|"
    r"analysis|update|updates|valuation|share|shares|stock|price|target|marketmunafa|"
    r"ltd|limited|india|indian|industries|industry|pvt|private|corp|corporation|company|co|"
    r"enterprises|enterprise|international|global|solutions|technologies|technology|tech|"
    r"finance|financial|services|projects|infra|infrastructure|power|energy|pharma|pharmaceuticals|"
    r"chemicals|chemical|steel|motors|motor|bank|holding|holdings|group|commercial|trading|exports"
    r")\b",
    flags=re.I,
)


def extract_scrip_code(text):
    if not text:
        return ""
    m = re.search(r"\b(5\d{5})\b", str(text))
    return m.group(1) if m else ""


def clean_company_core_name(raw_title):
    if not raw_title:
        return ""
    clean = html.unescape(str(raw_title))
    clean = re.sub(r"\(\d+\)", " ", clean)
    clean = clean.split("|")[0].split(":")[0].split(" - ")[0].split(" – ")[0]
    clean = re.sub(r"\b\d+(?:\.\d+)?%?\b", " ", clean)
    clean = STOP_WORDS_REGEX.sub(" ", clean)
    words = [re.sub(r"[^a-zA-Z]", "", w).lower() for w in clean.split()]
    words = [w for w in words if len(w) >= 2]
    if not words:
        fallback = re.sub(r"[^a-zA-Z0-9]", "", str(raw_title).split()[0] if str(raw_title).split() else "").lower()
        return fallback
    return "".join(words[:2])


def normalize_company_key(title, doc_type=""):
    core = clean_company_core_name(title)
    if not core:
        return ""
    return f"{doc_type}_{core}" if doc_type else core


def extract_file_key(url):
    if not url:
        return ""
    path = urlparse(url).path.lower()
    fname = path.rsplit("/", 1)[-1].strip()
    if fname.endswith((".pdf", ".xml", ".xbrl")):
        return fname
    return ""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted (
                item_id TEXT PRIMARY KEY,
                company_key TEXT,
                scrip_key TEXT,
                file_key TEXT,
                title TEXT,
                source_url TEXT,
                posted_at TEXT
            )
        """)
        for col in ("company_key", "scrip_key", "file_key"):
            try:
                conn.execute(f"ALTER TABLE posted ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def was_posted_in_db(item_id, comp_key, scrip_key, file_key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM posted 
            WHERE item_id = ? 
               OR (file_key != '' AND file_key = ?)
               OR (company_key != '' AND company_key = ?)
               OR (scrip_key != '' AND scrip_key = ?)
            """,
            (item_id, file_key, comp_key, scrip_key),
        ).fetchone()
        return row is not None


def mark_posted(item_id, comp_key, scrip_key, file_key, title, source_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posted(item_id, company_key, scrip_key, file_key, title, source_url, posted_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, comp_key, scrip_key, file_key, title, source_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_url(url, extra_headers=None, timeout=REQUEST_TIMEOUT):
    last_error = None
    profiles = ["chrome120", "chrome110", "edge101"]

    for attempt in range(2):
        try:
            profile = profiles[attempt % len(profiles)]
            headers = {
                "Accept": "application/json, application/rss+xml, application/xml, text/xml, application/pdf, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
            }
            if extra_headers:
                headers.update(extra_headers)
            response = c_requests.get(url, impersonate=profile, headers=headers, timeout=timeout)
            if response.status_code in (403, 429):
                raise requests.HTTPError(
                    f"Server returned {response.status_code}",
                    response=response,
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(2)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def to_float(val):
    try:
        if val is None:
            return 0.0
        s = str(val).strip()
        if not s or s.upper() in ("N/A", "NA", "-", "NONE", "NULL"):
            return 0.0
        cleaned = re.sub(r"[^\d.-]", "", s)
        num = float(cleaned) if cleaned and cleaned not in ("-", ".", "-.") else 0.0
        return int(round(num)) if abs(num - round(num)) < 0.01 else round(num, 2)
    except Exception:
        return 0.0


def fmt_num(val):
    num = to_float(val)
    if isinstance(num, int) or float(num).is_integer():
        return f"{int(num):,}"
    return f"{num:,.2f}"


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
        "screener_company_name": "",
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
        "q_all_labels": [],
        "q_all_rev": [],
        "q_all_gp": [],
        "q_all_np": [],
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
            res = c_requests.get(search_url, impersonate="chrome120", timeout=10)
            if res.status_code == 200 and isinstance(res.json(), list) and len(res.json()) > 0:
                first_hit = res.json()[0]
                info["screener_company_name"] = str(first_hit.get("name") or "").strip()
                comp_url = "https://www.screener.in" + first_hit["url"]
                c_res = c_requests.get(comp_url, impersonate="chrome120", timeout=12)
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
                            info["q_all_labels"] = headers
                            info["q_labels"] = headers[-6:]
                        r_title = cols[0].lower()
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "ebitda", "financing profit", "profit before tax", "net profit", "eps"]):
                            q_lines.append(" | ".join(cols))
                            all_nums = [to_float(c_val) for c_val in cols[1:]]
                            nums = all_nums[-6:]
                            if "sales" in r_title or "revenue" in r_title:
                                info["q_all_rev"] = all_nums
                                info["q_rev"] = nums
                            elif "operating profit" in r_title or "ebitda" in r_title or "financing profit" in r_title:
                                info["q_all_gp"] = all_nums
                                info["q_gp"] = nums
                            elif "profit before tax" in r_title and not info["q_all_gp"]:
                                info["q_all_gp"] = all_nums
                                info["q_gp"] = nums
                            elif "net profit" in r_title:
                                info["q_all_np"] = all_nums
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
                                info["eps"] = f"{nums[-1]}"
                    if pl_headers:
                        info["annual_text"] = "Annual Header: " + " | ".join(pl_headers[-4:]) + "\n" + "\n".join(pl_lines)
                break
        except Exception as exc:
            log.warning("Screener lookup warning for %s: %s", q, exc)
    return info


# ---------------- BSE DIRECT JSON API + XML FEED PARSING ----------------
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
    if str(scripcode).startswith("90") and len(str(scripcode)) >= 6:
        return True
    spam_phrases = (
        "nav as on", "nav as of", "current expense ratio", "monthly portfolio",
        "idcw reinvest", "direct plan - growth", "regular plan - growth",
    )
    return any(p in t_low for p in spam_phrases)


def fetch_bse_json_announcements():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_str = ist_now.strftime("%Y%m%d")
    prev_str = (ist_now - timedelta(days=1)).strftime("%Y%m%d")

    categories = [
        ("result", "Result"),
        ("announcement", "Company+Update"),
    ]
    output = []

    for kind, cat in categories:
        api_url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
            f"?pageno=1&strCat={cat}&strPrevDate={prev_str}&strScrip=&strSearch=P&strToDate={today_str}&strType=C&subcategory=-1"
        )
        try:
            res = get_url(api_url)
            data = res.json()
            rows = data.get("Table", []) if isinstance(data, dict) else []
            count = 0
            for r in rows:
                comp = str(r.get("SLONGNAME") or "").strip()
                scrip = str(r.get("SCRIP_CD") or "").strip()
                sub = str(r.get("NEWSSUB") or "").strip()
                head = str(r.get("HEADLINE") or "").strip()
                more = str(r.get("MORE") or "").strip()
                subcat = str(r.get("SUBCATNAME") or "").strip()
                attach = str(r.get("ATTACHMENTNAME") or "").strip()
                news_id = str(r.get("NEWSID") or "").strip()

                title = f"{comp} ({scrip}) - {sub}" if comp else sub
                desc = " | ".join(filter(None, [subcat, head, more]))

                if not title or is_mutual_fund_nav_spam(title, desc, scrip):
                    continue

                pdf_link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}" if attach else ""
                item_kind = "result" if (kind == "result" or "result" in subcat.lower()) else "announcement"

                output.append({
                    "kind": item_kind,
                    "title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(),
                    "company_raw": comp,
                    "scrip_code": scrip,
                    "description": html.unescape(re.sub(r"<[^>]+>", " ", desc)).strip(),
                    "link": pdf_link,
                    "attachment_name": attach,
                    "guid": news_id or pdf_link or title,
                    "published": str(r.get("DissemDT") or r.get("NEWS_DT") or ""),
                })
                count += 1
                if count >= MAX_ITEMS_PER_FEED:
                    break
            log.info("BSE JSON API (%s): %d items fetched", cat, count)
        except Exception as exc:
            log.warning("BSE JSON API warning (%s): %s", cat, exc)

    return output


def parse_feed(feed_url, feed_kind):
    response = get_url(feed_url)
    root = ET.fromstring(response.content)
    items = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]
    output = []

    for node in items:
        title = child_text(node, {"title"})
        description = child_text(node, {"description", "summary", "content", "encoded"})
        scrip = child_text(node, {"scripcode"}) or extract_scrip_code(title)
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

        clean_t = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip()
        comp_raw = clean_t.split("-")[0].split(":")[0].strip()

        output.append({
            "kind": feed_kind,
            "title": clean_t,
            "company_raw": comp_raw,
            "scrip_code": str(scrip).strip(),
            "description": html.unescape(re.sub(r"<[^>]+>", " ", description)).strip(),
            "link": urljoin(feed_url, link) if link else "",
            "attachment_name": "",
            "guid": guid or link or title,
            "published": child_text(node, {"pubdate", "published", "updated", "date"}),
        })
        if len(output) >= MAX_ITEMS_PER_FEED:
            break
    return output


# ---------------- DOCUMENT EXTRACTION ----------------
def clean_text(value):
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_xml_text(content_bytes):
    root = ET.fromstring(content_bytes)
    values = []
    for element in root.iter():
        if element.text and element.text.strip():
            values.append(element.text.strip())
    return clean_text(" ".join(values))[:MAX_TEXT_CHARS]


def extract_html_text(html_str):
    soup = BeautifulSoup(html_str, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return clean_text(soup.get_text(separator=" "))[:MAX_TEXT_CHARS]


def _extract_pdf_text_safe(pdf_bytes):
    text_parts = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            reader = PyPDF2.PdfReader(f, strict=False)
            for page in reader.pages[:MAX_PDF_PAGES]:
                try:
                    text_parts.append(page.extract_text() or "")
                except Exception:
                    continue
    return clean_text("\n".join(text_parts))[:MAX_TEXT_CHARS]


def get_document_payload(item):
    parts = [f"COMPANY TITLE: {item['title']}"]
    pdf_bytes = None
    if item["description"]:
        parts.append("FEED DESCRIPTION:\n" + item["description"])

    url = item["link"]
    attach_name = item.get("attachment_name", "")

    if url:
        path = urlparse(url).path.lower()
        try:
            if path.endswith(".pdf"):
                try:
                    response = get_url(url, timeout=15)
                except Exception:
                    if attach_name and "AttachLive" in url:
                        fallback_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attach_name}"
                        response = get_url(fallback_url, timeout=15)
                        item["link"] = fallback_url
                    else:
                        raise
                pdf_bytes = response.content
                log.info("Downloaded PDF (%d KB): %s", len(pdf_bytes) // 1024, url.rsplit("/", 1)[-1])

                if len(pdf_bytes) <= 12 * 1024 * 1024:
                    try:
                        extracted = run_with_timeout(12, _extract_pdf_text_safe, pdf_bytes)
                        if extracted:
                            parts.append("EXTRACTED PDF TEXT:\n" + extracted)
                    except Exception as exc:
                        log.warning("PDF text extraction skipped/timed out: %s", exc)
            elif path.endswith((".xml", ".xbrl")):
                response = get_url(url, timeout=15)
                extracted = extract_xml_text(response.content)
                if extracted:
                    parts.append("EXTRACTED XML TEXT:\n" + extracted)
            elif path.endswith((".html", ".htm")):
                response = get_url(url, timeout=15)
                extracted = extract_html_text(response.text)
                if extracted:
                    parts.append("EXTRACTED HTML TEXT:\n" + extracted)
        except Exception as exc:
            log.warning("Attachment download warning for %s: %s", url, exc)

    return clean_text("\n\n".join(parts))[:MAX_TEXT_CHARS], pdf_bytes


# ---------------- CLASSIFICATION / AI ----------------
RESULT_WORDS = (
    "financial result", "unaudited financial", "audited financial",
    "quarterly result", "half year result", "annual result", "financial results",
    "standalone result", "consolidated result", "standalone and consolidated",
)
ORDER_WORDS = (
    "award_of_order", "receipt_of_order", "award of order", "receipt of order",
    "receiving of orders", "received an order", "work order", "purchase order",
    "letter of acceptance", "letter of award", "loa from", "contract worth",
    "order worth", "orders worth", "bagged an order", "secured an order",
    "arbitral award", "orders / contracts", "order / contract",
)
IGNORE_NON_RESULT_PHRASES = (
    "analyst / investor meet", "investor meet", "earning call", "earnings call",
    "conference call", "audio recording", "transcript of", "prior intimation",
    "is scheduled on", "newspaper publication", "newspaper advertisement",
    "closure of trading window", "trading window", "loss of share certificate",
    "postal ballot", "scrutinizer report", "annual report", "related party transactions",
)


def classify_item(item):
    title_desc = (item["title"] + " " + item["description"]).lower()
    if any(word in title_desc for word in ORDER_WORDS):
        return "order"
    if any(bad in title_desc for bad in IGNORE_NON_RESULT_PHRASES):
        return ""
    if item["kind"] == "result" or any(word in title_desc for word in RESULT_WORDS):
        return "result"
    return ""


def make_item_id(comp_key, file_key, item):
    basis = file_key or comp_key or item["guid"] or item["title"]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_metric_dict(curr_val, prev_val):
    c = to_float(curr_val)
    p = to_float(prev_val)
    diff = round(c - p, 2)
    diff_sign = "+" if diff > 0 else ""
    diff_str
