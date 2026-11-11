import os
import sys
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

# ---------------- IMMEDIATE LOGGING SETUP (NO SILENT 0s EXIT) ----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("marketmunafa")

# ---------------- CONFIG ----------------
BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip()
BLOG_BASE_URL = os.getenv("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip()

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()


# ---------------- SECURE MULTI-KEY LOADER FROM GITHUB SECRETS ----------------
def load_gemini_keys_from_env():
    """
    GitHub Secrets માંથી GEMINI_API_KEY (કોમાથી જોડેલી 5 કી) અથવા
    GEMINI_API_KEY_1 થી GEMINI_API_KEY_5 સુરક્ષિત રીતે લોડ કરે છે.
    કોડમાં કી લખેલી ન હોવાથી ગૂગલ ક્યારેય કી બ્લોક નહીં કરે.
    """
    raw_chunks = []
    for env_name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEYS",
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
        "GEMINI_API_KEY_5",
    ):
        val = os.getenv(env_name, "").strip()
        if val:
            raw_chunks.append(val)

    combined = ",".join(raw_chunks)
    keys = []
    for part in re.split(r"[,;\s\n\r]+", combined):
        clean_k = part.strip().strip('"').strip("'")
        if len(clean_k) > 15 and clean_k not in keys:
            keys.append(clean_k)
    return keys


GEMINI_API_KEYS = load_gemini_keys_from_env()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
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
GEMINI_HTTP_TIMEOUT = int(os.getenv("GEMINI_HTTP_TIMEOUT", "35"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"


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


# ---------------- MULTI-KEY ROTATOR CLASS (5 REQ/MIN PER KEY) ----------------
class GeminiMultiKeyRotator:
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
            raise RuntimeError(
                "GEMINI_API_KEY is empty! Please add your comma-separated keys in GitHub Secrets -> GEMINI_API_KEY."
            )

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
                    log.info(
                        "Using Gemini API Key #%d/%d (Request %d/%d in current 60s window)",
                        idx + 1,
                        num_keys,
                        used_count,
                        self.max_per_min,
                    )
                    sys.stdout.flush()
                    return idx, self.keys[idx]
                else:
                    oldest_req_time = self.usage_history[idx][0]
                    wait_needed = (oldest_req_time + self.window_sec) - now + 0.5
                    if earliest_wait is None or wait_needed < earliest_wait:
                        earliest_wait = wait_needed

            sleep_sec = max(2.0, min(earliest_wait or 5.0, 62.0))
            log.info(
                "All %d API keys reached %d req/min limit. Waiting %.1f seconds for next slot...",
                num_keys,
                self.max_per_min,
                sleep_sec,
            )
            sys.stdout.flush()
            time.sleep(sleep_sec)

    def mark_key_rate_limited(self, idx, cooldown_sec=65.0):
        self.cooldown_until[idx] = time.time() + cooldown_sec
        log.warning("API Key #%d placed on %.0fs cooldown (429/Quota).", idx + 1, cooldown_sec)
        sys.stdout.flush()


KEY_ROTATOR = GeminiMultiKeyRotator(
    GEMINI_API_KEYS,
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
    diff_str = f"{diff_sign}{fmt_num(diff)} Cr"
    if p != 0:
        g_pct = ((c - p) / abs(p)) * 100.0
        g_sign = "+" if g_pct > 0 else ""
        g_str = f"{g_sign}{g_pct:.1f}%"
    else:
        g_str = "0.0%"
    return {
        "curr": fmt_num(c),
        "prev": fmt_num(p),
        "difference": diff_str,
        "growth": g_str,
    }


def ensure_valid_result_numbers(ai_data, screener_info):
    rev = ai_data.get("revenue") or {}
    gp = ai_data.get("gross_profit") or {}
    np_d = ai_data.get("net_profit") or {}

    q_rev_all = screener_info.get("q_all_rev", [])
    q_gp_all = screener_info.get("q_all_gp", [])
    q_np_all = screener_info.get("q_all_np", [])
    q_lbl_all = screener_info.get("q_all_labels", [])

    def pick_curr_prev(arr):
        if len(arr) >= 5:
            return arr[-1], arr[-5]
        elif len(arr) >= 2:
            return arr[-1], arr[-2]
        elif len(arr) == 1:
            return arr[0], arr[0]
        return 0, 0

    if len(q_lbl_all) >= 5:
        if str(ai_data.get("curr_period_label", "")).upper() in ("", "N/A", "CURRENT"):
            ai_data["curr_period_label"] = q_lbl_all[-1]
        if str(ai_data.get("prev_period_label", "")).upper() in ("", "N/A", "PREVIOUS"):
            ai_data["prev_period_label"] = q_lbl_all[-5]

    if to_float(rev.get("curr")) == 0 and to_float(rev.get("prev")) == 0 and q_rev_all:
        c_val, p_val = pick_curr_prev(q_rev_all)
        ai_data["revenue"] = compute_metric_dict(c_val, p_val)
    else:
        ai_data["revenue"] = compute_metric_dict(rev.get("curr"), rev.get("prev"))

    if to_float(gp.get("curr")) == 0 and to_float(gp.get("prev")) == 0 and q_gp_all:
        c_val, p_val = pick_curr_prev(q_gp_all)
        ai_data["gross_profit"] = compute_metric_dict(c_val, p_val)
    else:
        ai_data["gross_profit"] = compute_metric_dict(gp.get("curr"), gp.get("prev"))

    if to_float(np_d.get("curr")) == 0 and to_float(np_d.get("prev")) == 0 and q_np_all:
        c_val, p_val = pick_curr_prev(q_np_all)
        ai_data["net_profit"] = compute_metric_dict(c_val, p_val)
    else:
        ai_data["net_profit"] = compute_metric_dict(np_d.get("curr"), np_d.get("prev"))

    return ai_data


# ---------------- DIRECT GEMINI REST API CALLER ----------------
_CACHED_WORKING_MODEL = None


def get_candidate_gemini_models():
    candidates = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-flash-latest",
        GEMINI_MODEL,
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def call_gemini_rest_with_fallback(prompt_text, pdf_bytes=None):
    global _CACHED_WORKING_MODEL
    models_to_try = get_candidate_gemini_models()
    if _CACHED_WORKING_MODEL and _CACHED_WORKING_MODEL in models_to_try:
        models_to_try.remove(_CACHED_WORKING_MODEL)
        models_to_try.insert(0, _CACHED_WORKING_MODEL)

    parts = [{"text": prompt_text}]
    if pdf_bytes:
        b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        parts.append({
            "inline_data": {
                "mime_type": "application/pdf",
                "data": b64_pdf,
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }

    last_err = None
    max_key_attempts = max(1, len(KEY_ROTATOR.keys))

    for _ in range(max_key_attempts):
        key_idx, active_key = KEY_ROTATOR.acquire_key()
        key_quota_error = False

        for m_name in models_to_try:
            api_url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
                f"?key={urllib.parse.quote(active_key)}"
            )
            try:
                res = requests.post(
                    api_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=GEMINI_HTTP_TIMEOUT,
                )
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        content_parts = candidates[0].get("content", {}).get("parts", [])
                        out_text = "".join(p.get("text", "") for p in content_parts).strip()
                        if out_text:
                            if _CACHED_WORKING_MODEL != m_name:
                                log.info("Active Gemini REST model: %s (Key #%d)", m_name, key_idx + 1)
                                _CACHED_WORKING_MODEL = m_name
                            return out_text

                err_body = res.text[:300]
                last_err = f"HTTP {res.status_code}: {err_body}"
                err_low = err_body.lower()

                if res.status_code in (400, 401, 403, 429) and any(
                    w in err_low for w in ("quota", "depleted", "prepayment", "resource_exhausted", "rate", "api_key_invalid", "key not valid")
                ):
                    log.warning("Key #%d limit/auth notice on %s (HTTP %d): %s", key_idx + 1, m_name, res.status_code, err_body[:160])
                    KEY_ROTATOR.mark_key_rate_limited(key_idx, cooldown_sec=65.0)
                    key_quota_error = True
                    break
                else:
                    log.warning("Model %s returned HTTP %d on Key #%d, trying next model...", m_name, res.status_code, key_idx + 1)

            except requests.Timeout:
                last_err = f"Timeout ({GEMINI_HTTP_TIMEOUT}s) on model {m_name}"
                log.warning("Gemini REST timeout (%ds) on %s (Key #%d)", GEMINI_HTTP_TIMEOUT, m_name, key_idx + 1)
            except Exception as exc:
                last_err = str(exc)
                log.warning("Gemini REST error on %s (Key #%d): %s", m_name, key_idx + 1, exc)

        if not key_quota_error:
            break

    raise RuntimeError(f"All Gemini API keys/models failed: {last_err}")


def analyze_with_gemini_json(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info):
    prompt = f"""
You are the Chief Equity Research Analyst and SEO Editor for 'MarketMunafa' (https://marketmunafa.blogspot.com).
Analyze the attached BSE corporate filing ({doc_type.upper()}) and Screener fundamentals data.

STRICT RULES FOR 100% COPYRIGHT-FREE SEO CONTENT & CONSOLIDATED YOY DATA:
1. 100% ORIGINAL & COPYRIGHT-FREE WRITING: Write all analysis in your own original equity-research voice. Never copy sentences verbatim from the filing. Transform raw numbers into insightful financial commentary that passes Google's Helpful Content and E-E-A-T guidelines.
2. EXCLUDE STANDALONE: Completely ignore 'Standalone' figures if 'Consolidated' figures exist in the PDF. Always prioritize 'Consolidated Financial Results'. Only use Standalone if the company has no subsidiaries.
3. FOCUS ON CONSOLIDATED PAT & YOY GROWTH: For 'net_profit', strictly extract Consolidated Profit After Tax (PAT). Compare the latest quarter ('curr') against the SAME QUARTER LAST YEAR ('prev' - Year-on-Year / YoY).
4. UNIT CONVERSION: If the PDF table reports in "(Rs. in Lakhs)" or "(Rs. in Lacs)", divide every number by 100 to convert into Rs. Crores (e.g., 3,450 Lakhs = 34.50 Cr). If "(Rs. in Millions)", divide by 10.
5. NEVER return "N/A" for Revenue, Gross Profit, or Net Profit in a 'result' filing. If Gross Profit is not explicitly labeled, compute (Revenue minus Cost of Materials/Operating Expenses) or use EBITDA / PBT. If the PDF is unreadable, use the "Screener Historical Quarters" below.
6. All English fields MUST be 100% in English. Only 'gujarati_summary' and 'gujarati_intraday_view' must be in natural Gujarati.

Company Headline: {company_title}
BSE Scrip Code: {scrip_code or 'Listed on BSE'}
Document Type: {doc_type}
Screener Fundamentals:
- Market Cap: {screener_info['market_cap']}
- Current Price: {screener_info['price_str']}
- P/E Ratio (TTM): {screener_info['pe_ratio']}
- P/B Ratio: {screener_info['pb_ratio']}
- Industry P/E: {screener_info['industry_pe']}
- ROE: {screener_info['roe']}
- EPS (TTM): {screener_info['eps']}
- Div Yield: {screener_info['div_yield']}
- Book Value: {screener_info['book_value']}
- Total Debt: {screener_info['curr_debt_num']} Cr (Previous: {screener_info['prev_debt_num']} Cr)
- Debt-to-Equity: {screener_info['debt_to_equity']}
- Face Value: {screener_info['face_value']}

Screener Historical Quarters (in Rs. Cr.):
{screener_info['quarters_text']}

Screener Annual YoY Data (in Rs. Cr.):
{screener_info['annual_text']}

Return ONLY a valid JSON object with this exact structure:
{{
  "seo_title": "High-CTR Google Search & Discover Title in 100% English starting with Company Name + Share Price/Order/Q2 Consolidated PAT YoY Growth (70 to 105 chars)",
  "seo_lead_summary": "A 45-60 word SEO meta summary paragraph in 100% English starting with Company Name (BSE: {scrip_code}) that summarizes the exact Consolidated PAT/Revenue YoY growth OR New Order value, current share price ({screener_info['price_str']}), and valuation impact.",
  "company_short_name": "Clean Company Name in English",
  "result_date": "Date of filing in English (e.g. November 11, 2026)",
  "price_display": "{screener_info['price_str']}",
  "quarter_label": "e.g. Q2 FY26-27",
  "curr_period_label": "e.g. Sep 26",
  "prev_period_label": "e.g. Sep 25",
  "result_type": "Consolidated",
  "revenue": {{"curr": "36.76", "prev": "27.15", "growth": "+35.4%", "difference": "+9.61 Cr"}},
  "gross_profit": {{"curr": "14.24", "prev": "7.75", "growth": "+83.7%", "difference": "+6.49 Cr"}},
  "net_profit": {{"curr": "11.27", "prev": "6.89", "growth": "+63.6%", "difference": "+4.38 Cr"}},
  "debt_comparison": {{"curr": "{screener_info['curr_debt_num']}", "prev": "{screener_info['prev_debt_num']}"}},
  "order_details": {{
    "client_or_awarder": "Client or Authority Name (for order filings)",
    "order_value": "Order / Contract Value in Rs. Cr.",
    "order_to_mcap_ratio": "Calculate Order Value as % of Market Cap ({screener_info['market_cap']}) in English",
    "scope_of_work": "Clear description of project scope in English",
    "execution_timeline": "Execution timeline in English"
  }},
  "market_cap": "{screener_info['market_cap']}",
  "pe_ratio": "{screener_info['pe_ratio']}",
  "pb_ratio": "{screener_info['pb_ratio']}",
  "industry_pe": "{screener_info['industry_pe']}",
  "roe": "{screener_info['roe']}",
  "eps": "{screener_info['eps']}",
  "div_yield": "{screener_info['div_yield']}",
  "book_value": "{screener_info['book_value']}",
  "total_debt": "{screener_info['total_debt']}",
  "debt_to_equity": "{screener_info['debt_to_equity']}",
  "face_value": "{screener_info['face_value']}",
  "fundamental_impact_note": "1-2 sentences in 100% English explaining what changed in the fundamentals.",
  "estimated_stock_return": "Estimated % return / outlook in 100% English (e.g. +8.0% to +12.5% Bullish Momentum)",
  "intraday_signal": "1-2 sentences in 100% English explaining technical/positional sentiment.",
  "quarter_chart_labels": ["Jun 25", "Sep 25", "Dec 25", "Mar 26", "Jun 26", "Sep 26"],
  "quarter_chart_revenue": [24.1, 27.15, 26.04, 28.31, 30.8, 36.76],
  "quarter_chart_gross_profit": [7.1, 7.75, 7.9, 8.6, 10.2, 14.24],
  "quarter_chart_net_profit": [5.45, 6.89, 5.83, 7.51, 9.02, 11.27],
  "key_highlights": [
    "Bullet 1 in 100% English: Core top-line revenue or order book milestone with exact figures.",
    "Bullet 2 in 100% English: Operating margin and Consolidated PAT expansion/contraction.",
    "Bullet 3 in 100% English: Balance sheet strength, Total Debt ({screener_info['total_debt']}), and Debt-to-Equity ({screener_info['debt_to_equity']}).",
    "Bullet 4 in 100% English: Valuation comparison between Stock P/E ({screener_info['pe_ratio']}) and Industry P/E ({screener_info['industry_pe']})."
  ],
  "seo_article_sections": [
    {{
      "heading": "Operational Performance & YoY Earnings Breakdown (or Order Execution Visibility)",
      "body": "130-150 words of 100% original, copyright-free financial journalism in English analyzing the exact numbers from the filing, YoY trajectory, and operational efficiency."
    }},
    {{
      "heading": "Fundamental Valuation, ROE & Balance Sheet Health",
      "body": "130-150 words in 100% English analyzing Market Cap, Stock P/E vs Industry P/E, ROE, EPS, Book Value, Debt-to-Equity, and whether the stock looks undervalued or richly priced."
    }},
    {{
      "heading": "Market Outlook & Key Triggers for Investors",
      "body": "100-120 words in 100% English discussing medium-term growth catalysts, execution risks, and what retail investors should track next."
    }}
  ],
  "faqs": [
    {{
      "q": "What are the latest {doc_type} highlights of [Company Name]?",
      "a": "Direct 2-sentence answer in 100% English with exact Rs. Cr. numbers and YoY growth."
    }},
    {{
      "q": "What is the current P/E ratio, ROE, and Book Value of [Company Name] share?",
      "a": "Direct 2-sentence answer in 100% English citing Stock P/E, Industry P/E, ROE, and Book Value."
    }},
    {{
      "q": "Is [Company Name] a debt-free or low-debt company?",
      "a": "Direct 2-sentence answer in 100% English citing Total Debt and Debt-to-Equity ratio."
    }}
  ],
  "gujarati_summary": "2-3 sentences in natural Gujarati summarizing the Consolidated result (YoY) or new order.",
  "gujarati_intraday_view": "1-2 sentences in natural Gujarati explaining debt, valuation, and expected return."
}}

Extracted Text from Filing:
{source_text[:MAX_TEXT_CHARS]}
"""
    raw_text = None
    send_pdf_inline = (
        pdf_bytes is not None
        and len(pdf_bytes) <= 4 * 1024 * 1024
        and len(source_text) < 600
    )

    if send_pdf_inline:
        try:
            raw_text = call_gemini_rest_with_fallback(prompt, pdf_bytes=pdf_bytes)
        except Exception as exc:
            log.warning("PDF inline OCR fallback to text prompt: %s", exc)

    if not raw_text:
        raw_text = call_gemini_rest_with_fallback(prompt, pdf_bytes=None)

    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
    if not match:
        raise RuntimeError("Gemini did not return valid JSON")
    parsed = json.loads(match.group(0))

    if doc_type == "result":
        parsed = ensure_valid_result_numbers(parsed, screener_info)
    return parsed


# ---------------- VISUAL CHARTS BUILDER ----------------
def build_quarterly_column_chart_url(comp_name, labels, rev_list, gp_list, np_list):
    datasets = [
        {
            "label": "Revenue",
            "backgroundColor": "#1b5e20",
            "data": [to_float(x) for x in rev_list],
        },
        {
            "label": "Net Profit",
            "backgroundColor": "#2563eb",
            "data": [to_float(x) for x in np_list],
        },
    ]
    if gp_list and any(to_float(x) != 0 for x in gp_list):
        datasets.insert(
            1,
            {
                "label": "Gross Profit",
                "backgroundColor": "#90a4ae",
                "data": [to_float(x) for x in gp_list],
            },
        )

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": datasets,
        },
        "options": {
            "layout": {"padding": {"top": 26, "right": 12, "left": 8, "bottom": 8}},
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
                "text": f"{comp_name} - {qtr_lbl} YoY (Rs.Cr.)",
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


# ---------------- SEO + COPYRIGHT-FREE HTML BUILDER ----------------
def build_post_html(data, doc_type, screener_info, source_url, comp_key="", scrip_key="", file_key="", scrip_code=""):
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

    raw_lead = str(data.get("seo_lead_summary") or "").strip()
    if not raw_lead:
        if doc_type == "order":
            ord_val = (data.get("order_details") or {}).get("order_value", "a new contract")
            raw_lead = (
                f"{data.get('company_short_name', 'The company')} (BSE: {scrip_code or 'Listed'}) has announced the receipt "
                f"of a new order worth {ord_val}. Trading near {screener_info['price_str']} with a Market Cap of "
                f"{screener_info['market_cap']} and ROE of {screener_info['roe']}, here is the complete order book and fundamental analysis."
            )
        else:
            np_info = data.get("net_profit") or {}
            rev_info = data.get("revenue") or {}
            raw_lead = (
                f"{data.get('company_short_name', 'The company')} (BSE: {scrip_code or 'Listed'}) declared its {data.get('quarter_label', 'quarterly')} "
                f"Consolidated financial results, reporting a Net Profit (PAT) of Rs. {np_info.get('curr', '0')} Cr ({np_info.get('growth', '0%')} YoY) "
                f"and Revenue of Rs. {rev_info.get('curr', '0')} Cr ({rev_info.get('growth', '0%')} YoY)."
            )
    lead_html = html.escape(raw_lead)

    seed_int = int(hashlib.md5((comp_name + res_date).encode("utf-8")).hexdigest()[:6], 16)
    thumb_prompt = (
        f"modern 3d financial stock market analytics chart infographic upward trend corporate building india {comp_name}"
    )
    thumb_url = (
        f"https://image.pollinations.ai/prompt/{urllib.parse.quote(thumb_prompt)}"
        f"?width=1200&height=675&seed={seed_int}&nologo=true"
    )

    content_blocks = []

    if doc_type == "order":
        ord_info = data.get("order_details", {}) or {}
        client_str = html.escape(str(ord_info.get("client_or_awarder", "Disclosed in Filing")))
        val_str = html.escape(str(ord_info.get("order_value", "As per BSE Filing")))
        mcap_pct_str = html.escape(str(ord_info.get("order_to_mcap_ratio", "Significant Revenue Visibility")))
        scope_str = html.escape(str(ord_info.get("scope_of_work", "Execution of commercial order/contract")))
        time_str = html.escape(str(ord_info.get("execution_timeline", "As per contract terms")))

        order_card = f"""
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 16px; padding: 22px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #1e3a8a;">🏆 {comp_name} New Order &amp; Contract Details</h2>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Company Name:</strong> {comp_name} ({price_disp})</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Client / Awarding Authority:</strong> {client_str}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Order / Contract Value:</strong> <span style="color:#16a34a; font-weight:700;">{val_str}</span></p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Order Size vs Market Cap:</strong> {mcap_pct_str}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Nature of Work / Scope:</strong> {scope_str}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Execution Timeline:</strong> {time_str}</p>
        </div>
        """
        content_blocks.append(order_card)
        yoy_chart_url = ""
    else:
        rev = data.get("revenue", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        gp = data.get("gross_profit", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        np_d = data.get("net_profit", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        debt_comp = data.get("debt_comparison") or {"curr": screener_info["curr_debt_num"], "prev": screener_info["prev_debt_num"]}

        q_labels = data.get("quarter_chart_labels") or screener_info["q_labels"] or [prev_lbl, curr_lbl]
        q_rev = data.get("quarter_chart_revenue") or screener_info["q_rev"] or [to_float(rev.get("prev")), to_float(rev.get("curr"))]
        q_gp = data.get("quarter_chart_gross_profit") or screener_info["q_gp"] or [to_float(gp.get("prev")), to_float(gp.get("curr"))]
        q_np = data.get("quarter_chart_net_profit") or screener_info["q_np"] or [to_float(np_d.get("prev")), to_float(np_d.get("curr"))]

        quarter_chart_url = build_quarterly_column_chart_url(comp_name, q_labels, q_rev, q_gp, q_np)
        yoy_chart_url = build_yoy_comparison_chart_url(comp_name, qtr_lbl, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp)

        def growth_color(growth_str):
            return "#dc2626" if "-" in str(growth_str) else "#16a34a"

        result_card = f"""
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px; overflow-x: auto;">
          <div style="display: flex; justify-content: space-between; color: #64748b; font-size: 14px; margin-bottom: 10px;">
            <span>{res_date}</span>
            <span>Rs.Cr.</span>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px;">
            <strong style="font-size: 21px; color: #0f172a;">{comp_name}</strong>
            <span style="font-size: 19px; font-weight: 600; color: #0f172a;">{price_disp}</span>
          </div>
          <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <thead>
              <tr style="border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #64748b; font-weight: 600;">
                <th style="text-align: left; padding: 10px 4px; white-space: nowrap;">{qtr_lbl}</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">{curr_lbl}</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">{prev_lbl}</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">Diff</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">YoY Growth</th>
              </tr>
            </thead>
            <tbody>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500; white-space: nowrap;">Revenue</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">{html.escape(str(rev.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">{html.escape(str(rev.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(rev.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap; color: {growth_color(rev.get('growth', ''))};">{html.escape(str(rev.get('growth', '0%')))}</td>
              </tr>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500; white-space: nowrap;">Gross Profit</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">{html.escape(str(gp.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">{html.escape(str(gp.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(gp.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap; color: {growth_color(gp.get('growth', ''))};">{html.escape(str(gp.get('growth', '0%')))}</td>
              </tr>
              <tr>
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 500; white-space: nowrap;">Net Profit (PAT)</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">{html.escape(str(np_d.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">{html.escape(str(np_d.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(np_d.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap; color: {growth_color(np_d.get('growth', ''))};">{html.escape(str(np_d.get('growth', '0%')))}</td>
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
          <h2 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📈 {comp_name} YoY Financial &amp; Debt Comparison Chart</h2>
          <img src="{yoy_chart_url}" alt="{comp_name} {qtr_lbl} YoY Revenue Profit Chart" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center; box-shadow: 0 4px 14px rgba(0,0,0,0.04);">
          <h2 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📊 {comp_name} Multi-Quarter Performance Trend</h2>
          <img src="{quarter_chart_url}" alt="{comp_name} Multi Quarter Revenue Net Profit Trend" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        """
        content_blocks.append(result_card)

    highlights = data.get("key_highlights") or []
    if highlights and isinstance(highlights, list):
        li_items = "".join(
            f"<li style='margin-bottom: 8px; color: #1e293b; line-height: 1.6;'>{html.escape(str(h))}</li>"
            for h in highlights if h
        )
        highlights_card = f"""
        <div style="background: #f8fafc; border-left: 4px solid #2563eb; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">📌 Key Financial Highlights for Investors</h2>
          <ul style="margin: 0; padding-left: 20px; font-size: 15px;">
            {li_items}
          </ul>
        </div>
        """
        content_blocks.append(highlights_card)

    fundamentals_card = f"""
    <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
      <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #0f172a;">🔍 {comp_name} Fundamentals &amp; Valuation Ratios</h2>
      <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <tbody>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Mkt Cap</td>
            <td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{mkt_cap}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">ROE</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{roe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">P/E Ratio(TTM)</td>
            <td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pe_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">EPS(TTM)</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{eps}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">P/B Ratio</td>
            <td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pb_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Div Yield</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{div_yield}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Industry P/E</td>
            <td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{ind_pe}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Book Value</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{book_value}</td>
          </tr>
          <tr>
            <td style="padding: 10px 4px; color: #64748b;">Debt to Equity</td>
            <td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{de_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Face Value</td>
            <td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{face_val}</td>
          </tr>
        </tbody>
      </table>

      <p style="margin: 12px 0 6px 0; font-size: 14px; color: #475569;">
        <strong>Total Company Debt:</strong> {total_debt} | <strong>Fundamental Impact:</strong> {fund_impact}
      </p>

      <div style="background: #d1fae5; border: 1px solid #10b981; padding: 12px 14px; border-radius: 8px; font-size: 16px; margin-top: 12px;">
        🎯 <strong>Estimated Stock Return / Outlook:</strong> <span style="color: #065f46; font-weight: 700;">{est_return}</span>
      </div>
      <p style="margin: 10px 0 0 0; font-size: 14px; color: #334155;"><strong>Technical &amp; Positional View:</strong> {intraday_sig}</p>
    </div>
    """
    content_blocks.append(fundamentals_card)

    sections = data.get("seo_article_sections") or []
    article_html_parts = []
    if sections and isinstance(sections, list):
        for sec in sections:
            if isinstance(sec, dict):
                h_txt = html.escape(str(sec.get("heading", "Financial & Valuation Analysis")))
                b_txt = html.escape(str(sec.get("body", "")))
                if b_txt:
                    article_html_parts.append(
                        f"<h2 style='font-size: 19px; color: #0f172a; margin: 22px 0 10px 0;'>📝 {h_txt}</h2>"
                        f"<p style='color:#334155; font-size:16px; line-height:1.75; margin-bottom:16px;'>{b_txt}</p>"
                    )
    else:
        paragraphs = data.get("seo_article_paragraphs", [])
        article_html_parts.append(
            "<h2 style='font-size: 19px; color: #0f172a; margin: 22px 0 10px 0;'>📝 Detailed Corporate &amp; Valuation Analysis</h2>"
        )
        for p in paragraphs:
            article_html_parts.append(
                f"<p style='color:#334155; font-size:16px; line-height:1.75; margin-bottom:14px;'>{html.escape(str(p))}</p>"
            )

    faqs = data.get("faqs") or []
    if not faqs or not isinstance(faqs, list):
        faqs = [
            {
                "q": f"What is the current share price and Market Cap of {data.get('company_short_name', 'the company')}?",
                "a": f"{data.get('company_short_name', 'The company')} currently trades near {screener_info['price_str']} on the BSE with a total Market Capitalization of {screener_info['market_cap']}.",
            },
            {
                "q": f"What is the P/E ratio and ROE of {data.get('company_short_name', 'the company')}?",
                "a": f"The stock trades at a TTM P/E multiple of {screener_info['pe_ratio']} compared to the Industry P/E of {screener_info['industry_pe']}, with a Return on Equity (ROE) of {screener_info['roe']}.",
            },
            {
                "q": f"How much debt does {data.get('company_short_name', 'the company')} have?",
                "a": f"The company reports total borrowings of {screener_info['total_debt']} with a Debt-to-Equity ratio of {screener_info['debt_to_equity']}.",
            },
        ]

    faq_html_items = []
    faq_schema_entities = []
    for f_item in faqs:
        if isinstance(f_item, dict) and f_item.get("q") and f_item.get("a"):
            q_clean = str(f_item["q"]).strip()
            a_clean = str(f_item["a"]).strip()
            faq_html_items.append(
                f"<details style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:12px 16px; margin-bottom:10px;'>"
                f"<summary style='font-weight:600; color:#0f172a; cursor:pointer; font-size:15px;'>{html.escape(q_clean)}</summary>"
                f"<p style='margin:10px 0 0 0; color:#334155; font-size:15px; line-height:1.6;'>{html.escape(a_clean)}</p>"
                f"</details>"
            )
            faq_schema_entities.append({
                "@type": "Question",
                "name": q_clean,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": a_clean,
                },
            })

    faq_block_html = ""
    if faq_html_items:
        faq_block_html = (
            f"<div style='margin: 26px 0;'>"
            f"<h2 style='font-size: 19px; color: #0f172a; margin-bottom: 14px;'>❓ Frequently Asked Questions (FAQs)</h2>"
            f"{''.join(faq_html_items)}"
            f"</div>"
        )

    faq_schema_json = json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": faq_schema_entities,
    })

    internal_links_html = f"""
    <div style="background: #eff6ff; border: 1px dashed #60a5fa; border-radius: 10px; padding: 14px 18px; margin: 22px 0; font-size: 14px; color: #1e3a8a;">
      <strong>🔗 Explore More on MarketMunafa:</strong>
      <ul style="margin: 8px 0 0 0; padding-left: 18px; line-height: 1.8;">
        <li><a href="{BLOG_BASE_URL}/search/label/STOCK%20RESULT" style="color:#1d4ed8; font-weight:600; text-decoration:none;">📊 Latest Q2 &amp; Quarterly Stock Results Analysis</a></li>
        <li><a href="{BLOG_BASE_URL}/search/label/SHARE%20ORDER" style="color:#1d4ed8; font-weight:600; text-decoration:none;">🏆 New Corporate Order Wins &amp; Contract Updates</a></li>
        <li><a href="{BLOG_BASE_URL}/search/label/MARKET%20NEWS" style="color:#1d4ed8; font-weight:600; text-decoration:none;">📰 Daily Indian Stock Market Fundamental Picks</a></li>
      </ul>
    </div>
    """

    source_link_html = (
        f'<p style="margin-top:16px;"><a href="{html.escape(source_url, quote=True)}" '
        f'target="_blank" rel="nofollow noopener" style="color:#2563eb; font-weight:600; text-decoration:none;">'
        f'📄 Verify Official BSE Exchange Filing PDF</a></p>'
        if source_url else ""
    )

    meta_comment = (
        f"<!-- MM_META:type={doc_type}|comp_key={comp_key}|scrip={scrip_key}|file={file_key}|comp_name={comp_name} -->"
    )

    html_body = f"""
    {meta_comment}
    <article style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #1e293b;">
      <p style="font-size: 16px; line-height: 1.75; color: #0f172a; background: #f8fafc; padding: 14px 16px; border-radius: 10px; border: 1px solid #e2e8f0; margin-bottom: 18px;">
        <strong>Executive Summary:</strong> {lead_html}
      </p>

      <figure style="margin: 0 0 22px 0; text-align: center;">
        <img src="{thumb_url}" alt="{comp_name} Share Price Target and {qtr_lbl} Financial Analysis" width="1200" height="675" fetchpriority="high" style="width: 100%; height: auto; aspect-ratio: 16/9; object-fit: cover; border-radius: 12px;" />
        <figcaption style="font-size: 13px; color: #64748b; margin-top: 6px;">{comp_name} (BSE: {html.escape(str(scrip_code or 'Listed'))}) Equity Research | MarketMunafa</figcaption>
      </figure>
      <!--more-->

      {"".join(content_blocks)}

      {"".join(article_html_parts)}

      {faq_block_html}

      {internal_links_html}

      {source_link_html}

      <script type="application/ld+json">{faq_schema_json}</script>

      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 22px 0;" />
      <p style="font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Disclaimer:</strong> This independent equity research article on MarketMunafa is originally authored using publicly disclosed BSE corporate filings and fundamental ratios for educational purposes only. We are not SEBI-registered investment advisors. Please consult a certified financial planner before making any investment decisions.
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
        header_title = f"📊 <b>{comp_name} - {qtr_lbl} Consolidated રિઝલ્ટ અપડેટ</b>"
        rev = data.get("revenue", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        gp = data.get("gross_profit", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        np_d = data.get("net_profit", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        dynamic_block_gu = (
            f"📈 <b>નાણાકીય કામગીરી - YoY ({curr_lbl} vs {prev_lbl}):</b>\n"
            f"• <b>આવક (Revenue):</b> ₹{html.escape(str(rev.get('curr', '0')))} Cr (તફાવત: {html.escape(str(rev.get('difference', '0')))}) | YoY ગ્રોથ: <b>{html.escape(str(rev.get('growth', '0%')))}</b>\n"
            f"• <b>કુલ નફો (Gross Profit):</b> ₹{html.escape(str(gp.get('curr', '0')))} Cr (તફાવત: {html.escape(str(gp.get('difference', '0')))}) | YoY ગ્રોથ: <b>{html.escape(str(gp.get('growth', '0%')))}</b>\n"
            f"• <b>ચોખ્ખો નફો (Consolidated PAT):</b> ₹{html.escape(str(np_d.get('curr', '0')))} Cr (તફાવત: {html.escape(str(np_d.get('difference', '0')))}) | YoY ગ્રોથ: <b>{html.escape(str(np_d.get('growth', '0%')))}</b>\n\n"
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
                    "caption": f"📊 {comp_name} - Consolidated YoY પરિણામ અને ગ્રોથ ચાર્ટ",
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


# ---------------- BLOGGER SCANNER & PUBLISHER (WITH 429 RETRY LOGIC) ----------------
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


def _parse_single_blog_post_for_dedup(title_str, content_html, labels_list, state):
    t_low = title_str.lower()
    lbl_low = " ".join(str(x).lower() for x in (labels_list or []))

    if "share order" in lbl_low or any(w in t_low for w in ("order", "contract", "award", "loa", "work order")):
        p_type = "order"
    else:
        p_type = "result"

    meta_match = re.search(r"<!--\s*MM_META:([^>]+?)\s*-->", content_html)
    if meta_match:
        meta_parts = dict(
            kv.split("=", 1) for kv in meta_match.group(1).split("|") if "=" in kv
        )
        m_type = meta_parts.get("type") or p_type
        if meta_parts.get("comp_key"):
            state["comp_keys"].add(meta_parts["comp_key"].strip())
        if meta_parts.get("scrip"):
            state["scrip_keys"].add(meta_parts["scrip"].strip())
        if meta_parts.get("file"):
            state["file_keys"].add(meta_parts["file"].strip().lower())
        if meta_parts.get("comp_name"):
            ck = normalize_company_key(meta_parts["comp_name"], m_type)
            if ck:
                state["comp_keys"].add(ck)

    fig_match = re.search(r"<figcaption[^>]*>\s*([^<|(]+?)\s+(?:Corporate Filing|\(BSE)", content_html, flags=re.I)
    if fig_match:
        comp_from_fig = html.unescape(fig_match.group(1)).strip()
        ck = normalize_company_key(comp_from_fig, p_type)
        if ck:
            state["comp_keys"].add(ck)

    strong_match = re.search(r"<strong[^>]*font-size:\s*21px[^>]*>\s*([^<]+?)\s*</strong>", content_html, flags=re.I)
    if strong_match:
        comp_from_strong = html.unescape(strong_match.group(1)).strip()
        ck = normalize_company_key(comp_from_strong, p_type)
        if ck:
            state["comp_keys"].add(ck)

    ord_comp_match = re.search(r"<strong>\s*Company Name:\s*</strong>\s*([^<(]+)", content_html, flags=re.I)
    if ord_comp_match:
        comp_from_ord =
