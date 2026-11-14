import os
import time
import re
import json
import html
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

try:
    import openai
except ImportError:
    openai = None

# ---------------- CONFIG & CREDENTIALS ----------------
BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip()
BLOG_BASE_URL = os.getenv("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()

# ---------------- 1. GEMINI 3.5 FLASH-LITE QUOTA (15 RPM, 250,000 TPM, 500 RPD) ----------------
GEMINI_API_KEYS = [
    "AQ.Ab8RN6" + "JWzU70V3RM1zMEx0VEYmUj4q" + "EvvqqKbH27t1oHIO3Xjg",
    "AQ.Ab8RN6" + "Iw_vLVx2SPdac2gbUhK5fiSy" + "ihpDMHASZdZzdd4SnFGQ",
]
for _ek in re.split(r"[,\s]+", os.getenv("GEMINI_API_KEY", "").strip()):
    if _ek and _ek not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_ek)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_MAX_REQ_PER_MIN = 15        # 15 RPM
GEMINI_MAX_TOKENS_PER_MIN = 250000 # 250,000 TPM
GEMINI_MAX_REQ_PER_DAY = 500       # 500 RPD

# ---------------- 2. OPENROUTER BACKUP KEYS (ONLY 2 KEYS: 5 RPM, 20 RPD) ----------------
OPENROUTER_KEYS = [
    "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",
    "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",
]
OPENROUTER_MAX_REQ_PER_MIN = 5
OPENROUTER_MAX_REQ_PER_DAY = 20

RATE_WINDOW_SECONDS = 60.0

# ---------------- 3. PENNY STOCK FILTER & BLOGGER RATE LIMIT PROTECTION ----------------
# સ્મોલ કેપ, મિડ કેપ અને લાર્જ કેપ લેવાશે; માત્ર પેની સ્ટોક્સ (Mkt Cap < ₹150 Cr અથવા Price < ₹15) બાકાત રહેશે
MIN_MARKET_CAP_CR = float(os.getenv("MIN_MARKET_CAP_CR", "150.0"))
MIN_STOCK_PRICE = float(os.getenv("MIN_STOCK_PRICE", "15.0"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "10"))  # Blogger 429 ટાળવા એક રનમાં મહત્તમ 10 પોસ્ટ

FEEDS = {
    "result": "https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx",
    "announcement": "https://www.bseindia.com/data/xml/announcements.aspx",
}

DB_PATH = os.getenv("DB_PATH", "marketmunafa_seen.sqlite3")
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "100"))
MAX_BLOG_POSTS_TO_SCAN = int(os.getenv("MAX_BLOG_POSTS_TO_SCAN", "500"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "12"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "15000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ---------------- MULTI-KEY ROTATOR CLASS (RPM + TPM + RPD PROTECTION) ----------------
class MultiKeyRotator:
    def __init__(self, name, keys, max_per_min, max_per_day, max_tpm=250000, window_sec=60.0):
        self.name = name
        self.keys = [k.strip() for k in keys if k and k.strip()]
        self.max_per_min = max_per_min
        self.max_per_day = max_per_day
        self.max_tpm = max_tpm
        self.window_sec = window_sec
        self.usage_history = {i: [] for i in range(len(self.keys))}
        self.daily_count = {i: 0 for i in range(len(self.keys))}
        self.cooldown_until = {i: 0.0 for i in range(len(self.keys))}
        self.exhausted_today = set()
        self.next_idx = 0

    def acquire_key(self, estimated_tokens=4000):
        num_keys = len(self.keys)
        if num_keys == 0:
            return None, None

        for _ in range(2):
            if len(self.exhausted_today) >= num_keys:
                log.warning("All %s keys have reached their daily quota (%d/day).", self.name, self.max_per_day)
                return None, None

            now = time.time()
            earliest_wait = None

            for offset in range(num_keys):
                idx = (self.next_idx + offset) % num_keys
                if idx in self.exhausted_today:
                    continue

                if self.daily_count[idx] >= self.max_per_day:
                    self.exhausted_today.add(idx)
                    continue

                cutoff = now - self.window_sec
                self.usage_history[idx] = [(t, tok) for (t, tok) in self.usage_history[idx] if t > cutoff]

                if now < self.cooldown_until[idx]:
                    w = self.cooldown_until[idx] - now
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)
                    continue

                curr_rpm = len(self.usage_history[idx])
                curr_tpm = sum(tok for (_, tok) in self.usage_history[idx])

                if curr_rpm < self.max_per_min and (curr_tpm + estimated_tokens) <= self.max_tpm:
                    self.usage_history[idx].append((now, estimated_tokens))
                    self.daily_count[idx] += 1
                    self.next_idx = (idx + 1) % num_keys
                    log.info(
                        "Using %s Key #%d/%d | RPM: %d/%d | TPM: %d/%d | RPD: %d/%d",
                        self.name,
                        idx + 1,
                        num_keys,
                        curr_rpm + 1,
                        self.max_per_min,
                        curr_tpm + estimated_tokens,
                        self.max_tpm,
                        self.daily_count[idx],
                        self.max_per_day,
                    )
                    return idx, self.keys[idx]
                else:
                    w = (self.usage_history[idx][0][0] + self.window_sec) - now + 1.0
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)

            if len(self.exhausted_today) >= num_keys:
                return None, None

            sleep_s = max(1.0, min(earliest_wait or 5.0, 15.0))
            time.sleep(sleep_s)

        return None, None

    def mark_key_rate_limited(self, idx, err_msg="", cooldown_sec=65.0):
        if idx is None:
            return
        e_low = str(err_msg).lower()
        if any(w in e_low for w in ("depleted", "prepayment", "daily", "per_day", "invalid", "api_key_invalid", "unauthorized", "401")):
            self.exhausted_today.add(idx)
        else:
            self.cooldown_until[idx] = time.time() + cooldown_sec


GEMINI_ROTATOR = MultiKeyRotator(
    "Gemini (gemini-3.5-flash-lite)",
    GEMINI_API_KEYS,
    max_per_min=GEMINI_MAX_REQ_PER_MIN,
    max_per_day=GEMINI_MAX_REQ_PER_DAY,
    max_tpm=GEMINI_MAX_TOKENS_PER_MIN,
    window_sec=RATE_WINDOW_SECONDS,
)

OPENROUTER_ROTATOR = MultiKeyRotator(
    "OpenRouter Backup",
    OPENROUTER_KEYS,
    max_per_min=OPENROUTER_MAX_REQ_PER_MIN,
    max_per_day=OPENROUTER_MAX_REQ_PER_DAY,
    max_tpm=250000,
    window_sec=RATE_WINDOW_SECONDS,
)

# ---------------- STRICT DUPLICATE PROTECTION & COMPANY KEY NORMALIZATION ----------------
STOP_WORDS_REGEX = re.compile(
    r"\b(q1|q2|q3|q4|fy\d+|fy\d{2}-\d{2}|h1|h2|9m|results|result|order|orders|contract|contracts|"
    r"consolidated|standalone|pat|net|profit|revenue|sales|income|ebitda|growth|yoy|qoq|"
    r"jumps|surges|rises|soars|falls|drops|declines|slips|reports|posts|posted|announces|declared|"
    r"bags|wins|secures|receives|received|awarded|award|worth|rs|inr|cr|crore|crores|lakh|lakhs|"
    r"analysis|update|updates|valuation|share|shares|stock|price|target|marketmunafa|"
    r"ltd|limited|india|indian|industries|industry|pvt|private|corp|corporation|company|co|"
    r"enterprises|enterprise|international|global|solutions|technologies|technology|tech|"
    r"finance|financial|services|projects|infra|infrastructure|power|energy|pharma|pharmaceuticals|"
    r"chemicals|chemical|steel|motors|motor|bank|holding|holdings|group|commercial|trading|exports)\b",
    flags=re.I,
)


def extract_scrip_code(text):
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
    return fname if fname.endswith((".pdf", ".xml", ".xbrl")) else ""


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
            """SELECT 1 FROM posted WHERE item_id = ? OR (file_key != '' AND file_key = ?)
               OR (company_key != '' AND company_key = ?) OR (scrip_key != '' AND scrip_key = ?)""",
            (item_id, file_key, comp_key, scrip_key),
        ).fetchone()
        return row is not None


def mark_posted(item_id, comp_key, scrip_key, file_key, title, source_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted(item_id, company_key, scrip_key, file_key, title, source_url, posted_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (item_id, comp_key, scrip_key, file_key, title, source_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_url(url, extra_headers=None):
    profiles = ["chrome120", "chrome110", "edge101", "chrome104"]
    last_err = None
    for attempt in range(3):
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
            response = c_requests.get(url, impersonate=profile, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


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


# ---------------- SCREENER FUNDAMENTALS (EXACT SCRIP CODE VERIFICATION) ----------------
def get_screener_fundamentals(comp_title, scrip_code=""):
    clean_name = re.sub(r"\(\d+\)", "", comp_title).split("-")[0].strip()
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", clean_name, flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]

    # સૌથી પહેલાં BSE Scrip Code થી જ સર્ચ કરો જેથી ખોટી કંપની મેચ ન થાય
    queries = []
    if scrip_code:
        queries.append(str(scrip_code).strip())
    if clean_name:
        queries.append(clean_name)
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))

    info = {
        "screener_company_name": "", "price_str": "Live on BSE", "price_num": 0.0,
        "market_cap": "N/A", "market_cap_num": 0.0,
        "is_eligible_stock": False, "pe_ratio": "N/A", "pb_ratio": "N/A", "industry_pe": "N/A",
        "roe": "N/A", "eps": "N/A", "div_yield": "N/A", "book_value": "N/A", "total_debt": "₹0 Cr",
        "curr_debt_num": 0, "prev_debt_num": 0, "debt_to_equity": "0.00", "face_value": "N/A",
        "quarters_text": "", "q_all_labels": [], "q_all_rev": [], "q_all_gp": [], "q_all_np": [],
        "q_labels": [], "q_rev": [], "q_gp": [], "q_np": []
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
                first_hit = res.json()[0]
                hit_name = str(first_hit.get("name") or "").strip()

                # જો નામથી સર્ચ કર્યું હોય તો પહેલો શબ્દ કંપનીના નામ સાથે મેચ થવો જ જોઈએ
                if q != scrip_code and words:
                    if words[0].lower() not in hit_name.lower():
                        continue

                c_res = c_requests.get("https://www.screener.in" + first_hit["url"], impersonate="chrome120", timeout=15)
                if c_res.status_code != 200:
                    continue
                page_html = c_res.text

                # જો BSE Scrip Code ઉપલબ્ધ હોય અને પેજમાં બીજો કોઈ BSE કોડ હોય તો ખોટી મેચ ગણીને છોડી દો
                if scrip_code and f"/{scrip_code}/" not in page_html and f"BSE: {scrip_code}" not in page_html:
                    if q != scrip_code:
                        continue

                info["screener_company_name"] = hit_name
                soup = BeautifulSoup(c_res.content, "html.parser")

                top_ratios = soup.find("ul", id="top-ratios")
                if top_ratios:
                    for li in top_ratios.find_all("li"):
                        t = " ".join(li.get_text(" ", strip=True).split())
                        num_span = li.find("span", class_="number")
                        num_txt = num_span.get_text(strip=True) if num_span else ""

                        if "Current Price" in t and num_txt:
                            info["price_str"] = f"₹{num_txt}"
                            info["price_num"] = to_float(num_txt)
                        elif "Market Cap" in t and num_txt:
                            info["market_cap"] = f"₹{num_txt} Cr"
                            info["market_cap_num"] = to_float(num_txt)
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

                # માત્ર પેની સ્ટોક્સ (Mkt Cap < ₹150 Cr અથવા Share Price < ₹15) બાકાત રહેશે
                if info["market_cap_num"] >= MIN_MARKET_CAP_CR and (info["price_num"] == 0 or info["price_num"] >= MIN_STOCK_PRICE):
                    info["is_eligible_stock"] = True

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

                q_sec = soup.find("section", id="quarters")
                if q_sec:
                    q_lines, headers = [], []
                    for row in q_sec.find_all("tr"):
                        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]
                        if not cols:
                            continue
                        if not headers and len(cols) > 3:
                            headers = cols[1:]
                            info["q_all_labels"] = headers
                            info["q_labels"] = headers[-6:]
                        r_title = cols[0].lower()
                        if any(k in r_title for k in ["sales", "revenue", "operating profit", "ebitda", "net profit", "eps"]):
                            q_lines.append(" | ".join(cols))
                            all_nums = [to_float(c_val) for c_val in cols[1:]]
                            nums = all_nums[-6:]
                            if "sales" in r_title or "revenue" in r_title:
                                info["q_all_rev"], info["q_rev"] = all_nums, nums
                            elif "operating profit" in r_title or "ebitda" in r_title:
                                info["q_all_gp"], info["q_gp"] = all_nums, nums
                            elif "net profit" in r_title:
                                info["q_all_np"], info["q_np"] = all_nums, nums
                    if headers:
                        info["quarters_text"] = "Quarters Header: " + " | ".join(headers) + "\n" + "\n".join(q_lines)
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
    output = []

    for kind, cat in [("result", "Result"), ("announcement", "Company+Update")]:
        api_url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
            f"?pageno=1&strCat={cat}&strPrevDate={prev_str}&strScrip=&strSearch=P&strToDate={today_str}&strType=C&subcategory=-1"
        )
        try:
            res = get_url(api_url)
            rows = res.json().get("Table", []) if isinstance(res.json(), dict) else []
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
    output = []
    for node in [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]:
        title = child_text(node, {"title"})
        description = child_text(node, {"description", "summary", "content", "encoded"})
        scrip = child_text(node, {"scripcode"}) or extract_scrip_code(title)
        links = []
        for n in node.iter():
            if local_name(n.tag) in ("link", "enclosure") and (n.attrib.get("href") or n.attrib.get("url")):
                links.append((n.attrib.get("href") or n.attrib.get("url")).strip())
            elif local_name(n.tag) == "link" and n.text and str(n.text).strip().startswith("http"):
                links.append(str(n.text).strip())
        if not title or is_mutual_fund_nav_spam(title, description, scrip):
            continue
        direct = [u for u in links if urlparse(u).path.lower().endswith((".pdf", ".xml", ".xbrl", ".html", ".htm"))]
        link = direct[0] if direct else (links[0] if links else "")
        clean_t = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip()
        output.append({
            "kind": feed_kind,
            "title": clean_t,
            "company_raw": clean_t.split("-")[0].split(":")[0].strip(),
            "scrip_code": str(scrip).strip(),
            "description": html.unescape(re.sub(r"<[^>]+>", " ", description)).strip(),
            "link": urljoin(feed_url, link) if link else "",
            "guid": child_text(node, {"guid", "id"}) or link or title,
        })
        if len(output) >= MAX_ITEMS_PER_FEED:
            break
    return output


# ---------------- DOCUMENT EXTRACTION ----------------
def clean_text(value):
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value.replace("\x00", " "))).strip()


def get_document_payload(item):
    parts = [f"COMPANY TITLE: {item['title']}"]
    pdf_bytes = None
    extracted_pdf_text = ""
    if item["description"]:
        parts.append("FEED DESCRIPTION:\n" + item["description"])
    url, attach_name = item["link"], item.get("attachment_name", "")
    if url and urlparse(url).path.lower().endswith(".pdf"):
        try:
            try:
                response = get_url(url)
            except Exception:
                if attach_name and "AttachLive" in url:
                    url = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attach_name}"
                    response = get_url(url)
                    item["link"] = url
                else:
                    raise
            pdf_bytes = response.content
            text_parts = []
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                tmp.write(pdf_bytes)
                tmp.flush()
                with open(tmp.name, "rb") as f:
                    for page in PyPDF2.PdfReader(f).pages[:MAX_PDF_PAGES]:
                        text_parts.append(page.extract_text() or "")
            extracted_pdf_text = clean_text("\n".join(text_parts))[:MAX_TEXT_CHARS]
            if extracted_pdf_text:
                parts.append("EXTRACTED PDF TEXT:\n" + extracted_pdf_text)
        except Exception as exc:
            log.warning("Attachment download warning: %s", exc)
    return clean_text("\n\n".join(parts))[:MAX_TEXT_CHARS], pdf_bytes, len(extracted_pdf_text)


def classify_item(item):
    td = (item["title"] + " " + item["description"]).lower()
    order_words = (
        "award_of_order", "receipt_of_order", "award of order", "receipt of order",
        "receiving of orders", "received an order", "work order", "purchase order",
        "letter of acceptance", "letter of award", "loa from", "contract worth",
        "order worth", "orders worth", "bagged an order", "secured an order",
        "orders / contracts", "order / contract",
    )
    ignore_words = (
        "investor meet", "earning call", "earnings call", "conference call",
        "audio recording", "transcript of", "prior intimation", "newspaper publication",
        "trading window", "loss of share", "postal ballot", "scrutinizer report",
    )
    result_words = (
        "financial result", "unaudited financial", "audited financial",
        "quarterly result", "half year result", "annual result", "financial results",
        "standalone result", "consolidated result",
    )
    if any(w in td for w in order_words):
        return "order"
    if any(w in td for w in ignore_words):
        return ""
    if item["kind"] == "result" or any(w in td for w in result_words):
        return "result"
    return ""


def make_item_id(comp_key, file_key, item):
    return hashlib.sha256((file_key or comp_key or item["guid"] or item["title"]).encode("utf-8")).hexdigest()


def compute_metric_dict(curr_val, prev_val):
    c, p = to_float(curr_val), to_float(prev_val)
    diff = round(c - p, 2)
    diff_str = f"{'+' if diff > 0 else ''}{fmt_num(diff)} Cr"
    g_str = f"{'+' if ((c - p) / abs(p)) * 100 > 0 else ''}{((c - p) / abs(p)) * 100:.1f}%" if p != 0 else "0.0%"
    return {"curr": fmt_num(c), "prev": fmt_num(p), "difference": diff_str, "growth": g_str}


def get_screener_yoy_defaults(screener_info):
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

    curr_lbl = q_lbl_all[-1] if len(q_lbl_all) >= 1 else "Current"
    prev_lbl = q_lbl_all[-5] if len(q_lbl_all) >= 5 else (q_lbl_all[-2] if len(q_lbl_all) >= 2 else "Previous YoY")

    r_c, r_p = pick_curr_prev(q_rev_all)
    g_c, g_p = pick_curr_prev(q_gp_all)
    n_c, n_p = pick_curr_prev(q_np_all)

    return {
        "curr_lbl": curr_lbl,
        "prev_lbl": prev_lbl,
        "revenue": compute_metric_dict(r_c, r_p),
        "gross_profit": compute_metric_dict(g_c, g_p),
        "net_profit": compute_metric_dict(n_c, n_p),
    }


def ensure_valid_result_numbers(ai_data, screener_info, pdf_text_len=0):
    defaults = get_screener_yoy_defaults(screener_info)
    rev = ai_data.get("revenue") or {}
    gp = ai_data.get("gross_profit") or {}
    np_d = ai_data.get("net_profit") or {}

    use_screener_strict = (pdf_text_len < 250 and len(screener_info.get("q_all_rev", [])) >= 2)

    if use_screener_strict or str(ai_data.get("curr_period_label", "")).upper() in ("", "N/A", "CURRENT"):
        ai_data["curr_period_label"] = defaults["curr_lbl"]
    if use_screener_strict or str(ai_data.get("prev_period_label", "")).upper() in ("", "N/A", "PREVIOUS"):
        ai_data["prev_period_label"] = defaults["prev_lbl"]

    if use_screener_strict or (to_float(rev.get("curr")) == 0 and to_float(rev.get("prev")) == 0):
        ai_data["revenue"] = defaults["revenue"]
    else:
        ai_data["revenue"] = compute_metric_dict(rev.get("curr"), rev.get("prev"))

    if use_screener_strict or (to_float(gp.get("curr")) == 0 and to_float(gp.get("prev")) == 0):
        ai_data["gross_profit"] = defaults["gross_profit"]
    else:
        ai_data["gross_profit"] = compute_metric_dict(gp.get("curr"), gp.get("prev"))

    if use_screener_strict or (to_float(np_d.get("curr")) == 0 and to_float(np_d.get("prev")) == 0):
        ai_data["net_profit"] = defaults["net_profit"]
    else:
        ai_data["net_profit"] = compute_metric_dict(np_d.get("curr"), np_d.get("prev"))

    if use_screener_strict or not ai_data.get("quarter_chart_revenue"):
        ai_data["quarter_chart_labels"] = screener_info.get("q_labels", [])
        ai_data["quarter_chart_revenue"] = screener_info.get("q_rev", [])
        ai_data["quarter_chart_gross_profit"] = screener_info.get("q_gp", [])
        ai_data["quarter_chart_net_profit"] = screener_info.get("q_np", [])

    return ai_data


class DummyResponse:
    def __init__(self, text):
        self.text = text


def call_ai_api(prompt_text, pdf_bytes=None):
    est_tokens = max(2500, int(len(prompt_text) / 3.5) + (3000 if pdf_bytes else 0))

    # 1. Primary: Google Gemini API (gemini-3.5-flash-lite) with 15 RPM, 250k TPM & 500 RPD per key
    if GEMINI_API_KEYS:
        gemini_models = [GEMINI_MODEL, "gemini-3.5-flash-lite", "gemini-2.0-flash"]
        seen_m = []
        for m in gemini_models:
            if m and m not in seen_m:
                seen_m.append(m)

        for _ in range(len(GEMINI_ROTATOR.keys)):
            key_idx, active_key = GEMINI_ROTATOR.acquire_key(estimated_tokens=est_tokens)
            if active_key is None:
                break
            try:
                genai.configure(api_key=active_key)
            except Exception:
                pass

            key_error = False
            for m_name in seen_m:
                try:
                    model = genai.GenerativeModel(m_name)
                    if pdf_bytes and len(pdf_bytes) < 18 * 1024 * 1024:
                        resp = model.generate_content([prompt_text, {"mime_type": "application/pdf", "data": pdf_bytes}])
                    else:
                        resp = model.generate_content(prompt_text)
                    if resp and getattr(resp, "text", None):
                        log.info("Gemini success using model: %s", m_name)
                        return resp
                except Exception as exc:
                    err_str = str(exc).lower()
                    if any(w in err_str for w in ("429", "quota", "rate", "depleted", "exhausted", "invalid", "api_key_invalid", "400", "403")):
                        GEMINI_ROTATOR.mark_key_rate_limited(key_idx, str(exc), 65.0)
                        key_error = True
                        break
            if not key_error:
                break

    # 2. Backup: OpenRouter (2 Keys, 5 RPM & 20 RPD per key)
    if openai is not None and OPENROUTER_KEYS:
        or_models = [
            "google/gemini-3.5-flash-lite",
            "google/gemini-2.0-flash-lite-preview-02-05:free",
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ]
        for _ in range(len(OPENROUTER_ROTATOR.keys)):
            or_idx, or_key = OPENROUTER_ROTATOR.acquire_key(estimated_tokens=est_tokens)
            if or_key is None:
                break
            for or_model in or_models:
                try:
                    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
                    completion = client.chat.completions.create(
                        model=or_model,
                        messages=[{"role": "user", "content": prompt_text}],
                        temperature=0.3,
                        max_tokens=4000,
                    )
                    txt = completion.choices[0].message.content
                    if txt and "{" in txt:
                        log.info("OpenRouter backup success using model: %s", or_model)
                        return DummyResponse(txt)
                except Exception as exc:
                    err_s = str(exc).lower()
                    if any(w in err_s for w in ("401", "402", "403", "429", "rate", "quota", "credits", "invalid")):
                        OPENROUTER_ROTATOR.mark_key_rate_limited(or_idx, str(exc), 65.0)
                        break
                    continue

    return None


def extract_order_value_from_text(source_text):
    patterns = [
        r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:crore|crores|cr\.?)\b",
        r"([\d,]+(?:\.\d+)?)\s*(?:crore|crores|cr\.?)\b",
        r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:million|mn)\b",
    ]
    for pat in patterns:
        m = re.search(pat, source_text, flags=re.I)
        if m:
            val = m.group(1).replace(",", "")
            if "million" in pat or "mn" in pat:
                try:
                    return f"₹{float(val)/10.0:,.2f} Cr"
                except Exception:
                    pass
            return f"₹{val} Cr"
    return "Disclosed in BSE Filing"


def build_smart_fallback_data(company_title, scrip_code, doc_type, source_text, screener_info, pdf_text_len=0):
    c_name = screener_info.get("screener_company_name") or re.sub(r"\(\d+\)", "", company_title).split("-")[0].strip()
    ist_date_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%B %d, %Y")
    ord_val_extracted = extract_order_value_from_text(source_text) if doc_type == "order" else ""
    scr_defaults = get_screener_yoy_defaults(screener_info)

    fallback = {
        "company_short_name": c_name,
        "result_date": ist_date_str,
        "price_display": screener_info["price_str"],
        "quarter_label": f"Quarterly ({scr_defaults['curr_lbl']})",
        "curr_period_label": scr_defaults["curr_lbl"],
        "prev_period_label": scr_defaults["prev_lbl"],
        "result_type": "Consolidated",
        "revenue": scr_defaults["revenue"],
        "gross_profit": scr_defaults["gross_profit"],
        "net_profit": scr_defaults["net_profit"],
        "debt_comparison": {"curr": screener_info["curr_debt_num"], "prev": screener_info["prev_debt_num"]},
        "order_details": {
            "client_or_awarder": "Institutional / Corporate Client (As per BSE Filing)",
            "order_value": ord_val_extracted or "As per Official Exchange Filing",
            "order_to_mcap_ratio": f"Backed by Market Cap of {screener_info['market_cap']}",
            "scope_of_work": f"Commercial execution of contract as disclosed by {c_name} on BSE",
            "execution_timeline": "As per contract schedule",
        },
        "market_cap": screener_info["market_cap"],
        "pe_ratio": screener_info["pe_ratio"],
        "pb_ratio": screener_info["pb_ratio"],
        "industry_pe": screener_info["industry_pe"],
        "roe": screener_info["roe"],
        "eps": screener_info["eps"],
        "div_yield": screener_info["div_yield"],
        "book_value": screener_info["book_value"],
        "total_debt": screener_info["total_debt"],
        "debt_to_equity": screener_info["debt_to_equity"],
        "face_value": screener_info["face_value"],
        "fundamental_impact_note": f"{c_name} maintains a Debt-to-Equity ratio of {screener_info['debt_to_equity']} with ROE at {screener_info['roe']}.",
        "estimated_stock_return": "+8.0% to +15.0% Medium-Term Positional Outlook",
        "intraday_signal": f"Positive institutional sentiment around {c_name} ({screener_info['price_str']}) following the official BSE corporate filing.",
        "quarter_chart_labels": screener_info["q_labels"],
        "quarter_chart_revenue": screener_info["q_rev"],
        "quarter_chart_gross_profit": screener_info["q_gp"],
        "quarter_chart_net_profit": screener_info["q_np"],
    }

    if doc_type == "result":
        fallback = ensure_valid_result_numbers(fallback, screener_info, pdf_text_len)
        np_g = fallback["net_profit"]["growth"]
        rev_c = fallback["revenue"]["curr"]
        np_c = fallback["net_profit"]["curr"]
        fallback["seo_title"] = f"{c_name} Results: Consolidated PAT at ₹{np_c} Cr ({np_g} YoY) & Valuation Analysis"[:105]
        fallback["seo_lead_summary"] = (
            f"{c_name} (BSE: {scrip_code}) has declared its latest Consolidated financial results, posting Revenue of "
            f"₹{rev_c} Cr and Net Profit (PAT) of ₹{np_c} Cr ({np_g} YoY). Trading at {screener_info['price_str']} "
            f"with a Market Cap of {screener_info['market_cap']}, here is our complete fundamental review."
        )
        fallback["gujarati_summary"] = (
            f"{c_name} કંપનીએ તેના ત્રિમાસિક પરિણામો જાહેર કર્યા છે, જેમાં કંપનીની આવક ₹{rev_c} કરોડ અને "
            f"ચોખ્ખો નફો (PAT) ₹{np_c} કરોડ ({np_g} YoY) નોંધાયો છે."
        )
    else:
        fallback["seo_title"] = f"{c_name} Share News: Bags New Order ({ord_val_extracted}) | Fundamental Analysis"[:105]
        fallback["seo_lead_summary"] = (
            f"{c_name} (BSE: {scrip_code}) has officially announced a new order/contract update ({ord_val_extracted}) on BSE. "
            f"With a Market Cap of {screener_info['market_cap']}, ROE of {screener_info['roe']}, and Debt-to-Equity of "
            f"{screener_info['debt_to_equity']}, explore the revenue visibility and valuation outlook."
        )
        fallback["gujarati_summary"] = (
            f"{c_name} કંપનીને નવો કોન્ટ્રેક્ટ / ઓર્ડર ({ord_val_extracted}) મળ્યો છે, જેનાથી કંપનીની ઓર્ડર બુક અને આવનારા ક્વાર્ટરની આવકમાં મજબૂત વધારો થશે."
        )

    fallback["gujarati_intraday_view"] = (
        f"કંપનીનો Debt-to-Equity રેશિયો {screener_info['debt_to_equity']} અને ROE {screener_info['roe']} છે, જે મધ્યમથી લાંબા ગાળા માટે મજબૂત ફંડામેન્ટલ્સ દર્શાવે છે."
    )
    fallback["key_highlights"] = [
        f"Company & Market Cap: {c_name} (BSE: {scrip_code}) commands a market capitalization of {screener_info['market_cap']} at {screener_info['price_str']}.",
        f"Earnings / Order Catalyst: Official corporate filing on BSE highlights operational execution ({ord_val_extracted if doc_type=='order' else 'Revenue ₹' + fallback['revenue']['curr'] + ' Cr & PAT ₹' + fallback['net_profit']['curr'] + ' Cr'}).",
        f"Balance Sheet Leverage: Total borrowings stand at {screener_info['total_debt']} with a Debt-to-Equity ratio of {screener_info['debt_to_equity']}.",
        f"Valuation Multiples: Stock trades at a P/E of {screener_info['pe_ratio']} against Industry P/E of {screener_info['industry_pe']} with ROE of {screener_info['roe']}.",
    ]
    fallback["web_story_hook"] = [
        f"{c_name} ({screener_info['price_str']}) reports Revenue of ₹{fallback['revenue']['curr']} Cr ({fallback['revenue']['growth']} YoY)!" if doc_type == "result" else f"{c_name} ({screener_info['price_str']}) bags new order ({ord_val_extracted})!",
        f"Net Profit (PAT) stands at ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY) with ROE of {screener_info['roe']}." if doc_type == "result" else f"Market Cap stands at {screener_info['market_cap']} with Return on Equity (ROE) of {screener_info['roe']}.",
        f"Balance sheet discipline reflected in {screener_info['debt_to_equity']} Debt-to-Equity ratio ({screener_info['total_debt']} debt).",
        f"Valuation check: Stock P/E at {screener_info['pe_ratio']} vs Industry P/E at {screener_info['industry_pe']}.",
    ]
    fallback["seo_article_sections"] = [
        {
            "heading": "Operational Performance & Filing Breakdown",
            "body": (
                f"{c_name} (BSE: {scrip_code}) remains on the radar of institutional and retail investors following its latest exchange disclosure. "
                f"Operating with a market capitalization of {screener_info['market_cap']}, the business reported top-line revenue of "
                f"₹{fallback['revenue']['curr']} Cr ({fallback['revenue']['growth']} YoY) and Net Profit of ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY)."
            ),
        },
        {
            "heading": "Fundamental Valuation, ROE & Balance Sheet Health",
            "body": (
                f"From a fundamental lens, {c_name} trades at {screener_info['price_str']} with a trailing P/E multiple of {screener_info['pe_ratio']} "
                f"compared to the sector average P/E of {screener_info['industry_pe']}. Its Return on Equity (ROE) of {screener_info['roe']}, "
                f"Book Value of {screener_info['book_value']}, and Debt-to-Equity ratio of {screener_info['debt_to_equity']} highlight its capital allocation structure."
            ),
        },
        {
            "heading": "Market Outlook & Key Triggers for Investors",
            "body": (
                f"Going forward, market participants will closely track order execution, quarterly margin expansion, and working capital discipline. "
                f"With an EPS (TTM) of {screener_info['eps']} and dividend yield of {screener_info['div_yield']}, {c_name} offers clear fundamental benchmarks to monitor."
            ),
        },
    ]
    fallback["faqs"] = [
        {
            "q": f"What are the latest quarterly results of {c_name}?",
            "a": f"{c_name} posted Consolidated Revenue of ₹{fallback['revenue']['curr']} Cr ({fallback['revenue']['growth']} YoY) and Net Profit (PAT) of ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY).",
        },
        {
            "q": f"What is the Debt-to-Equity ratio and ROE of {c_name}?",
            "a": f"{c_name} has a Debt-to-Equity ratio of {screener_info['debt_to_equity']} (Total Debt: {screener_info['total_debt']}) and a Return on Equity (ROE) of {screener_info['roe']}.",
        },
        {
            "q": f"What is the current valuation P/E of {c_name} compared to its industry?",
            "a": f"Currently trading near {screener_info['price_str']}, {c_name} has a Stock P/E of {screener_info['pe_ratio']} compared to its Industry P/E of {screener_info['industry_pe']}.",
        },
    ]
    return fallback


def analyze_with_gemini_json(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len=0):
    scr_defaults = get_screener_yoy_defaults(screener_info)
    c_clean_name = screener_info.get("screener_company_name") or re.sub(r"\(\d+\)", "", company_title).split("-")[0].strip()

    prompt = f"""
You are the Chief Equity Research Analyst and SEO Editor for 'MarketMunafa' (https://marketmunafa.blogspot.com).
Analyze the attached BSE corporate filing ({doc_type.upper()}) and Screener fundamentals data for {c_clean_name}.

STRICT RULES FOR 100% ACCURATE COMPANY-SPECIFIC NUMBERS & ORIGINAL SEO CONTENT:
1. NEVER INVENT OR COPY DUMMY NUMBERS: Every number you output MUST belong specifically to {c_clean_name}.
2. CONSOLIDATED YOY PRIORITY: If the Extracted PDF Text below contains readable Consolidated financial result tables, convert Lakhs/Millions into Rs. Crores (1 Crore = 100 Lakhs = 10 Million) and extract Current Quarter vs Same Quarter Last Year (YoY).
3. FALLBACK TO SCREENER DATA: If the Extracted PDF Text does NOT contain readable financial tables (e.g. scanned PDF), you MUST use the exact Screener YoY figures for {c_clean_name} provided in the JSON template below.
4. All English fields MUST be 100% in English. Only 'gujarati_summary' and 'gujarati_intraday_view' must be in natural Gujarati.

Company Name: {c_clean_name}
Company Headline: {company_title}
BSE Scrip Code: {scrip_code or 'Listed on BSE'}
Document Type: {doc_type}
Screener Verified Fundamentals for {c_clean_name}:
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

Screener Historical Quarters for {c_clean_name} (in Rs. Cr.):
{screener_info['quarters_text']}

Return ONLY a valid JSON object with this exact structure (using {c_clean_name}'s real numbers):
{{
  "seo_title": "High-CTR Google Search & Discover Title in 100% English starting with {c_clean_name} + Share Price/Order/Consolidated PAT YoY Growth (70 to 105 chars)",
  "seo_lead_summary": "A 45-60 word SEO meta summary paragraph in 100% English starting with {c_clean_name} (BSE: {scrip_code}) summarizing the exact Consolidated PAT/Revenue YoY growth OR New Order value, current share price ({screener_info['price_str']}), and valuation impact.",
  "company_short_name": "{c_clean_name}",
  "result_date": "November 14, 2026",
  "price_display": "{screener_info['price_str']}",
  "quarter_label": "Quarterly Results",
  "curr_period_label": "{scr_defaults['curr_lbl']}",
  "prev_period_label": "{scr_defaults['prev_lbl']}",
  "result_type": "Consolidated",
  "revenue": {json.dumps(scr_defaults['revenue'])},
  "gross_profit": {json.dumps(scr_defaults['gross_profit'])},
  "net_profit": {json.dumps(scr_defaults['net_profit'])},
  "debt_comparison": {{"curr": "{screener_info['curr_debt_num']}", "prev": "{screener_info['prev_debt_num']}"}},
  "order_details": {{
    "client_or_awarder": "Client or Authority Name from filing",
    "order_value": "Order Value in Rs. Cr. from filing",
    "order_to_mcap_ratio": "Order Value as % of Market Cap ({screener_info['market_cap']}) in English",
    "scope_of_work": "Project scope in English",
    "execution_timeline": "Timeline in English"
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
  "fundamental_impact_note": "1-2 sentences in 100% English explaining what changed in {c_clean_name}'s fundamentals.",
  "estimated_stock_return": "Estimated % return / outlook in 100% English (e.g. +8.0% to +12.5% Positional Outlook)",
  "intraday_signal": "1-2 sentences in 100% English explaining technical/positional sentiment for {c_clean_name}.",
  "quarter_chart_labels": {json.dumps(screener_info['q_labels'])},
  "quarter_chart_revenue": {json.dumps(screener_info['q_rev'])},
  "quarter_chart_gross_profit": {json.dumps(screener_info['q_gp'])},
  "quarter_chart_net_profit": {json.dumps(screener_info['q_np'])},
  "key_highlights": [
    "Bullet 1 in 100% English: Core top-line revenue or order book milestone for {c_clean_name} with exact figures.",
    "Bullet 2 in 100% English: Operating margin and Consolidated PAT performance.",
    "Bullet 3 in 100% English: Balance sheet strength, Total Debt ({screener_info['total_debt']}) and Debt-to-Equity ({screener_info['debt_to_equity']}).",
    "Bullet 4 in 100% English: Valuation comparison between Stock P/E ({screener_info['pe_ratio']}) and Industry P/E ({screener_info['industry_pe']})."
  ],
  "web_story_hook": [
    "Unique punchy visual hook bullet 1 about {c_clean_name}'s exact revenue/profit or order win.",
    "Unique visual hook bullet 2 focusing on {c_clean_name}'s {screener_info['debt_to_equity']} debt-to-equity and {screener_info['roe']} ROE.",
    "Unique visual hook bullet 3 on {c_clean_name}'s valuation (P/E {screener_info['pe_ratio']} vs Industry P/E {screener_info['industry_pe']}).",
    "Unique visual hook bullet 4 on investor sentiment around {screener_info['price_str']}."
  ],
  "seo_article_sections": [
    {{
      "heading": "Operational Performance & YoY Earnings Breakdown",
      "body": "130-150 words of 100% original financial journalism in English analyzing {c_clean_name}'s exact numbers, YoY trajectory, and operational efficiency."
    }},
    {{
      "heading": "Fundamental Valuation, ROE & Balance Sheet Health",
      "body": "130-150 words in 100% English analyzing {c_clean_name}'s Market Cap ({screener_info['market_cap']}), Stock P/E ({screener_info['pe_ratio']}) vs Industry P/E ({screener_info['industry_pe']}), ROE ({screener_info['roe']}), Book Value ({screener_info['book_value']}), and Debt-to-Equity ({screener_info['debt_to_equity']})."
    }},
    {{
      "heading": "Market Outlook & Key Triggers for Investors",
      "body": "100-120 words in 100% English discussing medium-term growth catalysts, execution risks, and what retail investors should track next for {c_clean_name}."
    }}
  ],
  "faqs": [
    {{
      "q": "Why is {c_clean_name} share price in focus after latest quarterly results?",
      "a": "Direct answer in 100% English with {c_clean_name}'s exact numbers."
    }},
    {{
      "q": "What is the debt situation and Debt-to-Equity ratio of {c_clean_name}?",
      "a": "Direct answer in 100% English citing Total Debt of {screener_info['total_debt']} and Debt-to-Equity of {screener_info['debt_to_equity']}."
    }},
    {{
      "q": "Is {c_clean_name} share a good buy for long term based on its P/E ({screener_info['pe_ratio']}) and ROE ({screener_info['roe']})?",
      "a": "Direct answer in 100% English citing valuation and ROE."
    }}
  ],
  "gujarati_summary": "2-3 sentences in natural Gujarati summarizing {c_clean_name}'s exact Consolidated result (YoY) or new order.",
  "gujarati_intraday_view": "1-2 sentences in natural Gujarati explaining {c_clean_name}'s debt, valuation, and expected return."
}}

Extracted Text from Filing:
{source_text[:MAX_TEXT_CHARS]}
"""
    response = call_ai_api(prompt, pdf_bytes)

    if response and getattr(response, "text", None):
        raw = response.text.strip()
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if doc_type == "result":
                    parsed = ensure_valid_result_numbers(parsed, screener_info, pdf_text_len)
                return parsed
            except Exception as exc:
                log.warning("JSON parse warning, using smart fallback: %s", exc)

    log.info("Using Smart Auto-Fallback data for %s", company_title)
    return build_smart_fallback_data(company_title, scrip_code, doc_type, source_text, screener_info, pdf_text_len)


# ---------------- VISUAL CHARTS BUILDER ----------------
def build_quarterly_column_chart_url(comp_name, labels, rev_list, gp_list, np_list):
    datasets = [
        {"label": "Revenue", "backgroundColor": "#1b5e20", "data": [to_float(x) for x in rev_list]},
        {"label": "Net Profit", "backgroundColor": "#2563eb", "data": [to_float(x) for x in np_list]},
    ]
    if gp_list and any(to_float(x) != 0 for x in gp_list):
        datasets.insert(1, {"label": "Gross Profit", "backgroundColor": "#90a4ae", "data": [to_float(x) for x in gp_list]})

    chart_config = {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "layout": {"padding": {"top": 26, "right": 12, "left": 8, "bottom": 8}},
            "title": {"display": True, "text": f"{comp_name} - Multi-Quarter Trend (Rs.Cr.)", "fontSize": 16, "fontColor": "#334155", "padding": 14},
            "legend": {"display": True, "position": "top", "labels": {"fontSize": 12, "fontColor": "#334155"}},
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True, "padding": 6}}], "xAxes": [{"ticks": {"fontSize": 12, "fontColor": "#334155"}}]},
            "plugins": {"datalabels": {"display": True, "anchor": "end", "align": "top", "offset": 2, "clip": False, "color": "#000000", "font": {"weight": "bold", "size": 11}}},
        },
    }
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={urllib.parse.quote(json.dumps(chart_config))}"


def build_yoy_comparison_chart_url(comp_name, qtr_lbl, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp):
    labels = ["Revenue", "Gross Profit", "Net Profit", "Total Debt"]
    curr_data = [to_float(rev.get("curr")), to_float(gp.get("curr")), to_float(np_d.get("curr")), to_float(debt_comp.get("curr"))]
    prev_data = [to_float(rev.get("prev")), to_float(gp.get("prev")), to_float(np_d.get("prev")), to_float(debt_comp.get("prev"))]

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": str(curr_lbl), "backgroundColor": "#1b5e20", "data": curr_data},
                {"label": str(prev_lbl), "backgroundColor": "#90a4ae", "data": prev_data},
            ],
        },
        "options": {
            "layout": {"padding": {"top": 26, "right": 14, "left": 10, "bottom": 8}},
            "title": {"display": True, "text": f"{comp_name} - {qtr_lbl} YoY (Rs.Cr.)", "fontSize": 17, "fontColor": "#475569", "padding": 14},
            "legend": {"display": True, "position": "top", "labels": {"fontSize": 13, "fontColor": "#475569"}},
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True, "padding": 6}}], "xAxes": [{"ticks": {"fontSize": 13, "fontColor": "#475569"}}]},
            "plugins": {"datalabels": {"display": True, "anchor": "end", "align": "top", "offset": 2, "clip": False, "color": "#000000", "font": {"weight": "bold", "size": 12}}},
        },
    }
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={urllib.parse.quote(json.dumps(chart_config))}"


def build_post_html(data, doc_type, screener_info, source_url, comp_key="", scrip_key="", file_key="", scrip_code=""):
    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", datetime.now().strftime("%B %d, %Y"))))
    ist_time_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%B %d, %Y | %I:%M %p IST")

    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Quarterly")))
    curr_lbl = html.escape(str(data.get("curr_period_label", "Current")))
    prev_lbl = html.escape(str(data.get("prev_period_label", "Previous")))
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
            raw_lead = f"{data.get('company_short_name', 'The company')} (BSE: {scrip_code or 'Listed'}) has announced the receipt of a new order worth {ord_val}. Trading near {screener_info['price_str']} with a Market Cap of {screener_info['market_cap']} and ROE of {screener_info['roe']}, here is the complete order book and fundamental analysis."
        else:
            np_info = data.get("net_profit") or {}
            rev_info = data.get("revenue") or {}
            raw_lead = f"{data.get('company_short_name', 'The company')} (BSE: {scrip_code or 'Listed'}) declared its {data.get('quarter_label', 'quarterly')} Consolidated financial results, reporting a Net Profit (PAT) of Rs. {np_info.get('curr', '0')} Cr ({np_info.get('growth', '0%')} YoY) and Revenue of Rs. {rev_info.get('curr', '0')} Cr ({rev_info.get('growth', '0%')} YoY)."
    lead_html = html.escape(raw_lead)

    seed_int = int(hashlib.md5((comp_name + res_date).encode("utf-8")).hexdigest()[:6], 16)
    thumb_prompt = f"modern 3d financial stock market analytics chart infographic upward trend corporate building india {comp_name}"
    thumb_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(thumb_prompt)}?width=1200&height=675&seed={seed_int}&nologo=true"

    content_blocks = []

    web_story_hooks = data.get("web_story_hook") or []
    if web_story_hooks and isinstance(web_story_hooks, list):
        hook_list = "".join(f"<li style='margin-bottom:8px;'>{html.escape(str(h))}</li>" for h in web_story_hooks if h)
        if hook_list:
            content_blocks.append(f"""
            <div style="background: linear-gradient(135deg, #1e3a8a, #3b82f6); color: white; border-radius: 12px; padding: 18px 22px; margin-bottom: 24px; box-shadow: 0 4px 10px rgba(59, 130, 246, 0.3);">
              <h2 style="margin: 0 0 12px 0; font-size: 19px; border-bottom: 1px solid #93c5fd; padding-bottom: 8px; color: white;">⚡ Visual Web Story Summary (Top Picks)</h2>
              <ul style="margin: 0; padding-left: 20px; font-size: 15px; font-weight: 500; line-height: 1.6;">
                {hook_list}
              </ul>
              <p style="margin: 12px 0 0 0; font-size: 13px; text-align: right;"><em>MarketMunafa Rapid Analysis</em></p>
            </div>
            """)

    if doc_type == "order":
        ord_info = data.get("order_details", {}) or {}
        order_card = f"""
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 16px; padding: 22px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #1e3a8a;">🏆 {comp_name} New Order &amp; Contract Details</h2>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Company Name:</strong> {comp_name} ({price_disp})</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Client / Awarding Authority:</strong> {html.escape(str(ord_info.get("client_or_awarder", "Disclosed in Filing")))}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Order / Contract Value:</strong> <span style="color:#16a34a; font-weight:700;">{html.escape(str(ord_info.get("order_value", "As per BSE Filing")))}</span></p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Order Size vs Market Cap:</strong> {html.escape(str(ord_info.get("order_to_mcap_ratio", "Significant Revenue Visibility")))}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Nature of Work / Scope:</strong> {html.escape(str(ord_info.get("scope_of_work", "Execution of commercial order/contract")))}</p>
          <p style="margin: 8px 0; font-size: 16px;"><strong>Execution Timeline:</strong> {html.escape(str(ord_info.get("execution_timeline", "As per contract terms")))}</p>
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
            <span style="display: inline-block; border: 1px solid #cbd5e1; border-radius: 6px; padding: 4px 12px; font-size: 13px; color: #64748b;">{res_type}</span>
          </div>
        </div>
        
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center; box-shadow: 0 4px 14px rgba(0,0,0,0.04);">
          <h2 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📈 {comp_name} YoY Financial &amp; Debt Comparison Chart</h2>
          <img src="{yoy_chart_url}" alt="{comp_name} YoY Revenue Profit Chart" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center; box-shadow: 0 4px 14px rgba(0,0,0,0.04);">
          <h2 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📊 {comp_name} Multi-Quarter Performance Trend</h2>
          <img src="{quarter_chart_url}" alt="{comp_name} Multi Quarter Trend" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        """
        content_blocks.append(result_card)

    highlights = data.get("key_highlights") or []
    if highlights and isinstance(highlights, list):
        li_items = "".join(f"<li style='margin-bottom: 8px; color: #1e293b; line-height: 1.6;'>{html.escape(str(h))}</li>" for h in highlights if h)
        highlights_card = f"""
        <div style="background: #f8fafc; border-left: 4px solid #2563eb; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">📌 Key Financial Highlights for Investors</h2>
          <ul style="margin: 0; padding-left: 20px; font-size: 15px;">{li_items}</ul>
        </div>
        """
        content_blocks.append(highlights_card)

    fundamentals_card = f"""
    <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
      <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #0f172a;">🔍 {comp_name} Fundamentals &amp; Valuation Ratios</h2>
      <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <tbody>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Mkt Cap</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{mkt_cap}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">ROE</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{roe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">P/E Ratio(TTM)</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pe_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">EPS(TTM)</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{eps}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">P/B Ratio</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pb_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Div Yield</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{div_yield}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Industry P/E</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{ind_pe}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Book Value</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{book_value}</td>
          </tr>
          <tr>
            <td style="padding: 10px 4px; color: #64748b;">Debt to Equity</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{de_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Face Value</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{face_val}</td>
          </tr>
        </tbody>
      </table>
      <p style="margin: 12px 0 6px 0; font-size: 14px; color: #475569;"><strong>Total Company Debt:</strong> {total_debt} | <strong>Fundamental Impact:</strong> {fund_impact}</p>
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

    faqs = data.get("faqs") or []
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
            faq_schema_entities.append({"@type": "Question", "name": q_clean, "acceptedAnswer": {"@type": "Answer", "text": a_clean}})

    faq_block_html = ""
    if faq_html_items:
        faq_block_html = f"<div style='margin: 26px 0;'><h2 style='font-size: 19px; color: #0f172a; margin-bottom: 14px;'>❓ Frequently Asked Questions (FAQs)</h2>{''.join(faq_html_items)}</div>"

    faq_schema_json = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq_schema_entities})

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

    source_link_html = f'<p style="margin-top:16px;"><a href="{html.escape(source_url, quote=True)}" target="_blank" rel="nofollow noopener" style="color:#2563eb; font-weight:600; text-decoration:none;">📄 Verify Official BSE Exchange Filing PDF</a></p>' if source_url else ""
    meta_comment = f"<!-- MM_META:type={doc_type}|comp_key={comp_key}|scrip={scrip_key}|file={file_key}|comp_name={comp_name} -->"

    html_body = f"""
    {meta_comment}
    <article style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #1e293b;">
      <div style="font-size: 13px; color: #64748b; margin-bottom: 12px; font-weight: 500;">
        📅 Published on: <time datetime="{datetime.now(timezone.utc).isoformat()}">{ist_time_str}</time> | By MarketMunafa Equity Research Desk
      </div>
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
    qtr_lbl = html.escape(str(data.get("quarter_label", "Quarterly")))
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


# ---------------- BLOGGER SCANNER & DEDUPLICATION ----------------
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
        meta_parts = dict(kv.split("=", 1) for kv in meta_match.group(1).split("|") if "=" in kv)
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

    title_ck = normalize_company_key(title_str, p_type)
    if title_ck:
        state["comp_keys"].add(title_ck)

    for match in re.findall(r"[a-zA-Z0-9_-]{8,}\.(?:pdf|xml|xbrl|html)", content_html, flags=re.I):
        state["file_keys"].add(match.lower())

    for sc in re.findall(r"\b(5\d{5})\b", title_str + " " + content_html):
        state["scrip_keys"].add(f"{p_type}_{sc}")


def get_existing_blogger_posts():
    state = {"comp_keys": set(), "scrip_keys": set(), "file_keys": set()}
    if not BLOG_ID:
        return state

    total_scanned = 0
    try:
        token = blogger_access_token()
        page_token = None
        while total_scanned < MAX_BLOG_POSTS_TO_SCAN:
            params = {"maxResults": 100, "fetchBodies": "true"}
            if page_token:
                params["pageToken"] = page_token
            res = requests.get(
                f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=25,
            )
            if res.status_code != 200:
                break
            data = res.json()
            items = data.get("items", [])
            if not items:
                break
            for post in items:
                _parse_single_blog_post_for_dedup(
                    post.get("title", ""),
                    post.get("content", ""),
                    post.get("labels", []),
                    state,
                )
                total_scanned += 1
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        log.warning("Blogger API scan warning: %s", exc)

    log.info(
        "Blog Scan Complete: %d posts | %d company keys | %d file keys | %d scrip keys",
        total_scanned,
        len(state["comp_keys"]),
        len(state["file_keys"]),
        len(state["scrip_keys"]),
    )
    return state


def is_already_on_blog_or_db(item_id, comp_key, scrip_key, file_key, blog_state):
    if file_key and file_key in blog_state["file_keys"]:
        return True, f"file_key={file_key}"
    if scrip_key and scrip_key in blog_state["scrip_keys"]:
        return True, f"scrip_key={scrip_key}"
    if comp_key and comp_key in blog_state["comp_keys"]:
        return True, f"comp_key={comp_key}"
    if was_posted_in_db(item_id, comp_key, scrip_key, file_key):
        return True, "local_sqlite_db"
    return False, ""


def blogger_publish(title, content_html, doc_type):
    if not BLOG_ID:
        raise RuntimeError("BLOG_ID is missing")
    token = blogger_access_token()
    clean_title = re.sub(r"[\r\n\t]+", " ", re.sub(r"<[^>]+>", "", str(title))).strip()[:140] or "Corporate Financial Update | MarketMunafa"
    labels = ["MARKET NEWS", "SHARE ORDER"] if doc_type == "order" else ["MARKET NEWS", "STOCK RESULT"]

    for attempt in range(2):
        response = requests.post(
            f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"title": clean_title, "content": content_html, "labels": labels},
            timeout=25,
        )
        if response.status_code == 429:
            if attempt == 0:
                log.warning("Blogger rate limit (429) hit. Waiting 25s before retry...")
                time.sleep(25)
                continue
            log.warning("⚠️ Blogger Daily Post Limit (429 Too Many Requests) reached for today! Sending to Telegram only.")
            return None
        response.raise_for_status()
        return response.json().get("url", "")
    return None


def publish_item(item, doc_type, ai_data, screener_info, item_id, comp_key, scrip_key, file_key, scrip_code=""):
    source_url = item["link"]
    raw_title = ai_data.get("seo_title") or f"{item['title']} Consolidated Results & Valuation | MarketMunafa"
    blog_html, yoy_chart_url = build_post_html(
        ai_data,
        doc_type,
        screener_info,
        source_url,
        comp_key=comp_key,
        scrip_key=scrip_key,
        file_key=file_key,
        scrip_code=scrip_code,
    )

    blog_url = ""
    blogger_quota_hit = False
    if POST_TO_BLOGGER:
        res_url = blogger_publish(raw_title, blog_html, doc_type)
        if res_url is None:
            blogger_quota_hit = True
        else:
            blog_url = res_url
            mark_posted(item_id, comp_key, scrip_key, file_key, item["title"], source_url)
            log.info("✅ Blogger post published (%s): %s", doc_type, blog_url)

    send_telegram_gujarati(ai_data, doc_type, screener_info, blog_url, source_url, yoy_chart_url)
    return not blogger_quota_hit


# ---------------- MAIN RUNNER ----------------
def run():
    init_db()
    log.info("Scanning existing posts on MarketMunafa blog...")
    blog_state = get_existing_blogger_posts()

    all_items = fetch_bse_json_announcements()
    for kind, feed_url in FEEDS.items():
        try:
            items = parse_feed(feed_url, kind)
            log.info("%s XML feed: %d items", kind, len(items))
            all_items.extend(items)
        except Exception:
            log.exception("Feed failed: %s", feed_url)

    if not all_items:
        log.warning("No feed items retrieved.")
        return

    published_count = 0

    for item in all_items:
        if published_count >= MAX_POSTS_PER_RUN:
            log.info("Reached MAX_POSTS_PER_RUN (%d). Stopping run cleanly to protect Blogger quota.", MAX_POSTS_PER_RUN)
            break

        doc_type = classify_item(item)
        if not doc_type:
            continue

        comp_basis = item.get("company_raw") or item["title"]
        comp_key = normalize_company_key(comp_basis, doc_type)
        scrip_code = item.get("scrip_code") or extract_scrip_code(item["title"])
        scrip_key = f"{doc_type}_{scrip_code}" if scrip_code else ""
        file_key = extract_file_key(item["link"])
        item_id = make_item_id(comp_key, file_key, item)

        is_dup, dup_reason = is_already_on_blog_or_db(item_id, comp_key, scrip_key, file_key, blog_state)
        if is_dup:
            log.info("Skipping duplicate [%s] (%s): %s", dup_reason, doc_type, item["title"])
            continue

        log.info("Processing %s: %s", doc_type, item["title"])
        try:
            screener_info = get_screener_fundamentals(item["title"], scrip_code=scrip_code)

            # માત્ર પેની સ્ટોક્સ (Mkt Cap < ₹150 Cr અથવા Share Price < ₹15) બાકાત રહેશે
            if not screener_info["is_eligible_stock"]:
                log.info(
                    "Skipping Penny/Unlisted Stock (Market Cap: %s, Price: %s): %s",
                    screener_info["market_cap"],
                    screener_info["price_str"],
                    item["title"],
                )
                continue

            if screener_info.get("screener_company_name"):
                scr_comp_key = normalize_company_key(screener_info["screener_company_name"], doc_type)
                if scr_comp_key and scr_comp_key in blog_state["comp_keys"]:
                    log.info("Skipping duplicate [Screener Match: %s]: %s", scr_comp_key, item["title"])
                    continue

            source_text, pdf_bytes, pdf_text_len = get_document_payload(item)
            file_key = extract_file_key(item["link"]) or file_key

            if doc_type == "result" and not screener_info["q_all_rev"] and pdf_text_len < 200:
                log.warning("Skipped: no readable PDF numbers and no Screener quarterly data for %s", item["title"])
                continue

            ai_data = analyze_with_gemini_json(
                item["title"], scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len
            )

            ai_comp_name = str(ai_data.get("company_short_name") or "").strip()
            ai_comp_key = normalize_company_key(ai_comp_name, doc_type) if ai_comp_name else ""
            if ai_comp_key and ai_comp_key in blog_state["comp_keys"]:
                log.info("Skipping duplicate [AI Name Match: %s]: %s", ai_comp_key, item["title"])
                continue

            if doc_type == "result":
                r_curr = to_float(ai_data.get("revenue", {}).get("curr"))
                np_curr = to_float(ai_data.get("net_profit", {}).get("curr"))
                if r_curr == 0 and np_curr == 0:
                    log.warning("Skipping result post with 0 financial figures: %s", item["title"])
                    continue

            final_comp_key = ai_comp_key or comp_key
            blogger_ok = publish_item(
                item,
                doc_type,
                ai_data,
                screener_info,
                item_id,
                final_comp_key,
                scrip_key,
                file_key,
                scrip_code=scrip_code,
            )

            if comp_key:
                blog_state["comp_keys"].add(comp_key)
            if ai_comp_key:
                blog_state["comp_keys"].add(ai_comp_key)
            if scrip_key:
                blog_state["scrip_keys"].add(scrip_key)
            if file_key:
                blog_state["file_keys"].add(file_key)

            published_count += 1
            if not blogger_ok:
                log.info("Stopping further Blogger posts for this run as Blogger daily limit (429) is active.")
                break

            time.sleep(12)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
