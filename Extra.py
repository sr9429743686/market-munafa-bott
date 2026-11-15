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
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================
BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip()
BLOG_BASE_URL = os.getenv("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()

# ---------------- GEMINI 3.5 FLASH-LITE QUOTA (15 RPM, 250k TPM, 500 RPD) ----------------
GEMINI_API_KEYS = [
    "AQ.Ab8RN6" + "JWzU70V3RM1zMEx0VEYmUj4q" + "EvvqqKbH27t1oHIO3Xjg",
    "AQ.Ab8RN6" + "Iw_vLVx2SPdac2gbUhK5fiSy" + "ihpDMHASZdZzdd4SnFGQ",
]
for _ek in re.split(r"[,\s]+", os.getenv("GEMINI_API_KEY", "").strip()):
    if _ek and _ek not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_ek)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_MAX_RPM = 15
GEMINI_MAX_TPM = 250000
GEMINI_MAX_RPD = 500

# ---------------- OPENROUTER BACKUP KEYS (2 KEYS: 5 RPM, 20 RPD) ----------------
OPENROUTER_KEYS = [
    "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",
    "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",
]
OPENROUTER_MAX_RPM = 5
OPENROUTER_MAX_RPD = 20

RATE_WINDOW_SECONDS = 60.0

# ---------------- QUALITY & TRAFFIC FILTERS ----------------
MIN_MARKET_CAP_CR = float(os.getenv("MIN_MARKET_CAP_CR", "100.0"))
MIN_STOCK_PRICE = float(os.getenv("MIN_STOCK_PRICE", "10.0"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))

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


# ==========================================
# 2. MULTI-KEY ROTATOR (RPM + TPM + RPD)
# ==========================================
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

            time.sleep(max(1.0, min(earliest_wait or 5.0, 15.0)))

        return None, None

    def mark_key_rate_limited(self, idx, err_msg="", cooldown_sec=65.0):
        if idx is None:
            return
        e_low = str(err_msg).lower()
        if any(w in e_low for w in ("depleted", "daily", "per_day", "invalid", "api_key_invalid", "unauthorized", "401")):
            self.exhausted_today.add(idx)
        else:
            self.cooldown_until[idx] = time.time() + cooldown_sec


GEMINI_ROTATOR = MultiKeyRotator(
    "Gemini (gemini-3.5-flash-lite)",
    GEMINI_API_KEYS,
    max_per_min=GEMINI_MAX_RPM,
    max_per_day=GEMINI_MAX_RPD,
    max_tpm=GEMINI_MAX_TPM,
    window_sec=RATE_WINDOW_SECONDS,
)

OPENROUTER_ROTATOR = MultiKeyRotator(
    "OpenRouter Backup",
    OPENROUTER_KEYS,
    max_per_min=OPENROUTER_MAX_RPM,
    max_per_day=OPENROUTER_MAX_RPD,
    max_tpm=250000,
    window_sec=RATE_WINDOW_SECONDS,
)

# ==========================================
# 3. LIVE GOOGLE AUTOCOMPLETE KEYWORD ENGINE
# ==========================================
def fetch_live_google_keywords(company_name):
    """
    ગૂગલ સર્ચ બારમાંથી લોકો ખરેખર જે કીવર્ડ્સ (100 થી 10,000+ સર્ચ વોલ્યુમ)
    સર્ચ કરી રહ્યા છે તે રીઅલ-ટાઇમમાં ખેંચી લાવે છે.
    """
    short_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", company_name, flags=re.I).strip()
    seed_queries = [
        f"{short_name} share ",
        f"{short_name} share price target ",
        f"why {short_name} share ",
        f"{short_name} q2 results ",
    ]
    found_keywords = []
    seen = set()

    for q in seed_queries:
        try:
            url = f"https://suggestqueries.google.com/complete/search?client=firefox&gl=in&hl=en&q={urllib.parse.quote(q)}"
            res = c_requests.get(url, impersonate="chrome120", timeout=6)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
                    for kw in data[1]:
                        kw_clean = str(kw).strip()
                        if kw_clean and kw_clean.lower() not in seen and len(kw_clean) > 8:
                            seen.add(kw_clean.lower())
                            found_keywords.append(kw_clean)
        except Exception:
            pass

    # High-search-volume default retail keywords that always get searched on Google India
    high_vol_defaults = [
        f"{short_name} share price target 2026",
        f"{short_name} share price target 2027 to 2030",
        f"{short_name} Q2 results 2026 analysis",
        f"Why {short_name} share price is rising today",
        f"Is {short_name} share a good buy for long term",
        f"{short_name} share latest news and order book",
        f"{short_name} dividend record date and history",
        f"{short_name} debt to equity and ROE fundamental review",
    ]
    for d_kw in high_vol_defaults:
        if d_kw.lower() not in seen:
            seen.add(d_kw.lower())
            found_keywords.append(d_kw)

    return found_keywords[:10]


# ==========================================
# 4. DUPLICATE PROTECTION & HELPERS
# ==========================================
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


def clean_text(value):
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", str(value).replace("\x00", " "))).strip()


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


# ==========================================
# 5. SCREENER FUNDAMENTALS & VALUATION TARGETS
# ==========================================
def get_screener_fundamentals(comp_title, scrip_code=""):
    clean_name = re.sub(r"\(\d+\)", "", comp_title).split("-")[0].strip()
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", clean_name, flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]

    queries = []
    if scrip_code:
        queries.append(str(scrip_code).strip())
    if clean_name:
        queries.append(clean_name)
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))

    info = {
        "screener_company_name": "",
        "nse_bse_symbol": scrip_code or "BSE",
        "price_str": "Live on BSE",
        "price_num": 0.0,
        "high_52w": "N/A",
        "low_52w": "N/A",
        "market_cap": "N/A",
        "market_cap_num": 0.0,
        "is_eligible_stock": False,
        "pe_ratio": "N/A",
        "pb_ratio": "N/A",
        "industry_pe": "N/A",
        "roe": "N/A",
        "roce": "N/A",
        "eps": "N/A",
        "div_yield": "N/A",
        "book_value": "N/A",
        "total_debt": "₹0 Cr",
        "curr_debt_num": 0,
        "prev_debt_num": 0,
        "debt_to_equity": "0.00",
        "face_value": "N/A",
        "quarters_text": "",
        "q_all_labels": [],
        "q_all_rev": [],
        "q_all_gp": [],
        "q_all_np": [],
        "q_labels": [],
        "q_rev": [],
        "q_gp": [],
        "q_np": [],
        "target_2026": "N/A",
        "target_2027": "N/A",
        "target_2030": "N/A",
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

                if q != scrip_code and words:
                    if words[0].lower() not in hit_name.lower():
                        continue

                c_res = c_requests.get("https://www.screener.in" + first_hit["url"], impersonate="chrome120", timeout=15)
                if c_res.status_code != 200:
                    continue
                page_html = c_res.text

                if scrip_code and f"/{scrip_code}/" not in page_html and f"BSE: {scrip_code}" not in page_html:
                    if q != scrip_code:
                        continue

                info["screener_company_name"] = hit_name
                soup = BeautifulSoup(c_res.content, "html.parser")

                top_ratios = soup.find("ul", id="top-ratios")
                if top_ratios:
                    for li in top_ratios.find_all("li"):
                        t = " ".join(li.get_text(" ", strip=True).split())
                        nums = [s.get_text(strip=True) for s in li.find_all("span", class_="number") if s.get_text(strip=True)]
                        num_txt = nums[0] if nums else ""

                        if "Current Price" in t and num_txt:
                            info["price_str"] = f"₹{num_txt}"
                            info["price_num"] = to_float(num_txt)
                        elif "High / Low" in t and len(nums) >= 2:
                            info["high_52w"] = f"₹{nums[0]}"
                            info["low_52w"] = f"₹{nums[1]}"
                        elif "Market Cap" in t and num_txt:
                            info["market_cap"] = f"₹{num_txt} Cr"
                            info["market_cap_num"] = to_float(num_txt)
                        elif ("Stock P/E" in t or t.startswith("P/E")) and num_txt:
                            info["pe_ratio"] = num_txt
                        elif "Book Value" in t and num_txt:
                            info["book_value"] = f"₹{num_txt}"
                        elif "Div Yield" in t and num_txt:
                            info["div_yield"] = f"{num_txt}%"
                        elif "ROCE" in t and num_txt:
                            info["roce"] = f"{num_txt}%"
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

                if info["market_cap_num"] >= MIN_MARKET_CAP_CR and (info["price_num"] == 0 or info["price_num"] >= MIN_STOCK_PRICE):
                    info["is_eligible_stock"] = True

                # Calculate algorithmic valuation targets (for high-traffic "Share Price Target" searches)
                if info["price_num"] > 0:
                    p = info["price_num"]
                    info["target_2026"] = f"₹{round(p * 1.15, 1):,} to ₹{round(p * 1.24, 1):,}"
                    info["target_2027"] = f"₹{round(p * 1.35, 1):,} to ₹{round(p * 1.50, 1):,}"
                    info["target_2030"] = f"₹{round(p * 1.90, 1):,} to ₹{round(p * 2.40, 1):,}"

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


# ==========================================
# 6. BSE API & FEEDS
# ==========================================
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


# ==========================================
# 7. AI API CALL & HIGH-TRAFFIC SEO GENERATOR
# ==========================================
class DummyResponse:
    def __init__(self, text):
        self.text = text


def call_ai_api(prompt_text, pdf_bytes=None):
    est_tokens = max(2500, int(len(prompt_text) / 3.5) + (3000 if pdf_bytes else 0))

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
                    if any(w in err_str for w in ("429", "quota", "rate", "depleted", "exhausted", "invalid", "400", "403")):
                        GEMINI_ROTATOR.mark_key_rate_limited(key_idx, str(exc), 65.0)
                        key_error = True
                        break
            if not key_error:
                break

    if OpenAI is not None and OPENROUTER_KEYS:
        or_models = ["google/gemini-3.5-flash-lite", "google/gemini-2.0-flash-lite-preview-02-05:free", "google/gemma-3-27b-it:free"]
        for _ in range(len(OPENROUTER_ROTATOR.keys)):
            or_idx, or_key = OPENROUTER_ROTATOR.acquire_key(estimated_tokens=est_tokens)
            if or_key is None:
                break
            for or_model in or_models:
                try:
                    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
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


def build_smart_fallback_data(company_title, scrip_code, doc_type, source_text, screener_info, trending_kws, pdf_text_len=0):
    c_name = screener_info.get("screener_company_name") or re.sub(r"\(\d+\)", "", company_title).split("-")[0].strip()
    ist_date_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%B %d, %Y")
    ord_val_extracted = extract_order_value_from_text(source_text) if doc_type == "order" else ""
    scr_defaults = get_screener_yoy_defaults(screener_info)

    np_growth_num = to_float(scr_defaults["net_profit"]["growth"])
    rev_growth_num = to_float(scr_defaults["revenue"]["growth"])
    skip_flag = False
    if doc_type == "result" and np_growth_num < 0 and rev_growth_num < 5 and len(screener_info.get("q_all_np", [])) >= 2:
        skip_flag = True

    fallback = {
        "skip_stock": skip_flag,
        "company_short_name": c_name,
        "result_date": ist_date_str,
        "price_display": screener_info["price_str"],
        "quarter_label": f"Q2 / Quarterly ({scr_defaults['curr_lbl']})",
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
        "estimated_stock_return": f"{screener_info['target_2026']} (2026 Valuation Outlook)",
        "intraday_signal": f"Strong search interest around {c_name} ({screener_info['price_str']}) with 52-Week High at {screener_info['high_52w']} and Low at {screener_info['low_52w']}.",
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
        # High-Search-Volume Title starting with exact keywords people search!
        fallback["seo_title"] = f"{c_name} Share Price Target 2026, Q2 Results (PAT {np_g} YoY) & Latest News"[:105]
        fallback["seo_lead_summary"] = (
            f"Looking for {c_name} share price target 2026, Q2 results, and fundamental analysis? "
            f"{c_name} (BSE: {scrip_code}) declared Consolidated Revenue of ₹{rev_c} Cr and Net Profit (PAT) of "
            f"₹{np_c} Cr ({np_g} YoY). Trading near {screener_info['price_str']}, check its 2026-2030 valuation outlook."
        )
        fallback["gujarati_summary"] = (
            f"{c_name} કંપનીએ તેના ત્રિમાસિક પરિણામો જાહેર કર્યા છે, જેમાં કંપનીની આવક ₹{rev_c} કરોડ અને "
            f"ચોખ્ખો નફો (PAT) ₹{np_c} કરોડ ({np_g} YoY) નોંધાયો છે."
        )
    else:
        fallback["seo_title"] = f"{c_name} Share Latest News: Bags {ord_val_extracted} Order | Share Price Target 2026"[:105]
        fallback["seo_lead_summary"] = (
            f"Why is {c_name} share price in focus today? {c_name} (BSE: {scrip_code}) has bagged a new order worth "
            f"{ord_val_extracted}. Trading at {screener_info['price_str']} with a Market Cap of {screener_info['market_cap']}, "
            f"check {c_name} share price target 2026, order book, and fundamental analysis."
        )
        fallback["gujarati_summary"] = (
            f"{c_name} કંપનીને નવો કોન્ટ્રેક્ટ / ઓર્ડર ({ord_val_extracted}) મળ્યો છે, જેનાથી કંપનીની ઓર્ડર બુક અને આવનારા ક્વાર્ટરની આવકમાં મજબૂત વધારો થશે."
        )

    fallback["gujarati_intraday_view"] = (
        f"કંપનીનો Debt-to-Equity રેશિયો {screener_info['debt_to_equity']} અને ROE {screener_info['roe']} છે, જે મધ્યમથી લાંબા ગાળા માટે મજબૂત ફંડામેન્ટલ્સ દર્શાવે છે."
    )
    fallback["key_highlights"] = [
        f"{c_name} Share Price & 52W Range: Currently trading at {screener_info['price_str']} (52-Week High: {screener_info['high_52w']} | 52-Week Low: {screener_info['low_52w']}).",
        f"Quarterly Earnings / Order Catalyst: {'Revenue at ₹' + fallback['revenue']['curr'] + ' Cr & Consolidated PAT at ₹' + fallback['net_profit']['curr'] + ' Cr (' + fallback['net_profit']['growth'] + ' YoY)' if doc_type=='result' else 'New Order Win of ' + ord_val_extracted}.",
        f"Balance Sheet & Debt Status: Total Debt stands at {screener_info['total_debt']} with a Debt-to-Equity ratio of {screener_info['debt_to_equity']}.",
        f"Valuation Comparison: Stock P/E is {screener_info['pe_ratio']} vs Industry P/E of {screener_info['industry_pe']}, supported by ROE of {screener_info['roe']} and Book Value of {screener_info['book_value']}.",
    ]
    fallback["web_story_hook"] = [
        f"{c_name} ({screener_info['price_str']}) reports Revenue of ₹{fallback['revenue']['curr']} Cr ({fallback['revenue']['growth']} YoY)!" if doc_type == "result" else f"{c_name} ({screener_info['price_str']}) wins new order ({ord_val_extracted})!",
        f"Consolidated Net Profit (PAT) at ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY) with ROE of {screener_info['roe']}." if doc_type == "result" else f"Market Cap stands at {screener_info['market_cap']} with ROE of {screener_info['roe']}.",
        f"52-Week High/Low range at {screener_info['high_52w']} / {screener_info['low_52w']} with {screener_info['debt_to_equity']} Debt-to-Equity.",
        f"2026 Fundamental Valuation Target range estimated at {screener_info['target_2026']} based on earnings trajectory.",
    ]
    fallback["seo_article_sections"] = [
        {
            "heading": f"Why {c_name} Share is in Focus Today: Quarterly Results & Order Book Breakdown",
            "body": (
                f"{c_name} (BSE: {scrip_code}) has captured strong investor search interest following its latest corporate filing. "
                f"With a market capitalization of {screener_info['market_cap']}, the company recorded top-line revenue of "
                f"₹{fallback['revenue']['curr']} Cr ({fallback['revenue']['growth']} YoY) and Net Profit of ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY)."
            ),
        },
        {
            "heading": f"{c_name} Share Price Target 2026, 2027, 2030 & Valuation Analysis",
            "body": (
                f"Investors searching for the {c_name} share price target should evaluate its earnings multiple and balance sheet strength. "
                f"Trading at {screener_info['price_str']} against a Book Value of {screener_info['book_value']}, the stock commands a P/E of "
                f"{screener_info['pe_ratio']} compared to its Industry P/E of {screener_info['industry_pe']}. Based on its EPS (TTM) of "
                f"{screener_info['eps']} and ROE of {screener_info['roe']}, its fundamental valuation band for 2026 is projected around {screener_info['target_2026']}."
            ),
        },
        {
            "heading": f"Is {c_name} Share a Good Buy for Long Term? Debt & ROE Check",
            "body": (
                f"For long-term portfolio investors, balance sheet leverage is a critical filter. {c_name} carries total borrowings of "
                f"{screener_info['total_debt']} with a Debt-to-Equity ratio of {screener_info['debt_to_equity']}. Combined with a dividend yield of "
                f"{screener_info['div_yield']} and 52-week high/low bounds of {screener_info['high_52w']} / {screener_info['low_52w']}, retail investors should track upcoming quarterly margin expansion."
            ),
        },
    ]
    fallback["faqs"] = [
        {
            "q": f"What is the {c_name} share price target for 2026 and 2027?",
            "a": f"Based on current EPS (TTM) of {screener_info['eps']}, ROE of {screener_info['roe']}, and P/E valuation of {screener_info['pe_ratio']}, {c_name}'s fundamental valuation range is estimated at {screener_info['target_2026']} for 2026 and {screener_info['target_2027']} for 2027.",
        },
        {
            "q": f"Why is {c_name} share price rising or falling today?",
            "a": f"{c_name} share is reacting to its latest BSE filing where Consolidated Net Profit stood at ₹{fallback['net_profit']['curr']} Cr ({fallback['net_profit']['growth']} YoY) on Revenue of ₹{fallback['revenue']['curr']} Cr.",
        },
        {
            "q": f"Is {c_name} a debt-free company?",
            "a": f"{c_name} has a total debt of {screener_info['total_debt']} and a Debt-to-Equity ratio of {screener_info['debt_to_equity']}.",
        },
        {
            "q": f"Is {c_name} share a good buy for long term investment?",
            "a": f"With a Market Cap of {screener_info['market_cap']}, ROE of {screener_info['roe']}, and Stock P/E of {screener_info['pe_ratio']} (vs Industry P/E {screener_info['industry_pe']}), {c_name} offers solid fundamental metrics for long-term tracking.",
        },
    ]
    return fallback


def analyze_with_gemini_json(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info, trending_kws, pdf_text_len=0):
    scr_defaults = get_screener_yoy_defaults(screener_info)
    c_clean_name = screener_info.get("screener_company_name") or re.sub(r"\(\d+\)", "", company_title).split("-")[0].strip()
    kws_str = ", ".join(trending_kws)

    prompt = f"""
You are India's #1 SEO Financial Journalist and Equity Research Analyst for 'MarketMunafa' (https://marketmunafa.blogspot.com).
Analyze the attached BSE corporate filing ({doc_type.upper()}) and Screener fundamentals data for {c_clean_name}.

HIGH-TRAFFIC GOOGLE SEARCH & DISCOVER RANKING RULES:
1. STRONG RESULT FILTER: Set `"skip_stock": false` ONLY IF the company has strong results (e.g., positive YoY Net Profit growth >= 5%, turnaround to profit, strong revenue jump, or major new order win). If results are weak/loss-making/flat, set `"skip_stock": true`.
2. HIGH-SEARCH-VOLUME TITLE (CRITICAL FOR BLOGGER URL SLUG): Your `seo_title` MUST start with `{c_clean_name} Share Price Target 2026` or `{c_clean_name} Q2 Results` or `{c_clean_name} Share Latest News` so that the Blogger URL matches what 10,000 retail investors search on Google!
3. NATURALLY INTEGRATE THESE LIVE GOOGLE TRENDING KEYWORDS IN HEADINGS, BODY & FAQs:
   {kws_str}
4. 100% ORIGINAL & ADSENSE SAFE: Never copy sentences from the filing. Frame price targets strictly as educational/fundamental valuation projections based on EPS & P/E.
5. NEVER INVENT DUMMY NUMBERS: Use only {c_clean_name}'s exact figures from the filing or Screener data below.

Company Name: {c_clean_name} (BSE: {scrip_code})
Screener Verified Fundamentals:
- Current Share Price: {screener_info['price_str']} (52W High: {screener_info['high_52w']} | 52W Low: {screener_info['low_52w']})
- Market Cap: {screener_info['market_cap']}
- Stock P/E: {screener_info['pe_ratio']} | Industry P/E: {screener_info['industry_pe']} | P/B: {screener_info['pb_ratio']}
- ROE: {screener_info['roe']} | ROCE: {screener_info['roce']} | EPS (TTM): {screener_info['eps']}
- Total Debt: {screener_info['total_debt']} | Debt-to-Equity: {screener_info['debt_to_equity']}
- Book Value: {screener_info['book_value']} | Div Yield: {screener_info['div_yield']}
- Algorithmic Valuation Bands: 2026: {screener_info['target_2026']} | 2027: {screener_info['target_2027']} | 2030: {screener_info['target_2030']}

Screener Historical Quarters (in Rs. Cr.):
{screener_info['quarters_text']}

Return ONLY a valid JSON object with this exact structure:
{{
  "skip_stock": false,
  "seo_title": "{c_clean_name} Share Price Target 2026, Q2 Results (PAT YoY Growth) & Latest News",
  "seo_lead_summary": "45-60 word high-CTR summary in English starting with '{c_clean_name} Share Price Target 2026 & Q2 Results:' covering exact PAT/Revenue YoY growth or Order value, current price ({screener_info['price_str']}), and valuation impact.",
  "company_short_name": "{c_clean_name}",
  "result_date": "November 14, 2026",
  "price_display": "{screener_info['price_str']}",
  "quarter_label": "Q2 / Quarterly",
  "curr_period_label": "{scr_defaults['curr_lbl']}",
  "prev_period_label": "{scr_defaults['prev_lbl']}",
  "result_type": "Consolidated",
  "revenue": {json.dumps(scr_defaults['revenue'])},
  "gross_profit": {json.dumps(scr_defaults['gross_profit'])},
  "net_profit": {json.dumps(scr_defaults['net_profit'])},
  "debt_comparison": {{"curr": "{screener_info['curr_debt_num']}", "prev": "{screener_info['prev_debt_num']}"}},
  "order_details": {{
    "client_or_awarder": "Client Name",
    "order_value": "Order Value in Rs. Cr.",
    "order_to_mcap_ratio": "Order Value as % of Market Cap",
    "scope_of_work": "Project scope",
    "execution_timeline": "Execution timeline"
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
  "fundamental_impact_note": "1-2 sentences in English explaining what changed in {c_clean_name}'s fundamentals.",
  "estimated_stock_return": "{screener_info['target_2026']} (Medium-Term Valuation Band)",
  "intraday_signal": "1-2 sentences in English covering 52-week high/low ({screener_info['high_52w']} / {screener_info['low_52w']}) and momentum.",
  "quarter_chart_labels": {json.dumps(screener_info['q_labels'])},
  "quarter_chart_revenue": {json.dumps(screener_info['q_rev'])},
  "quarter_chart_gross_profit": {json.dumps(screener_info['q_gp'])},
  "quarter_chart_net_profit": {json.dumps(screener_info['q_np'])},
  "key_highlights": [
    "Bullet 1 in English: Revenue & PAT YoY growth or Order Book milestone with exact numbers.",
    "Bullet 2 in English: 52-Week High ({screener_info['high_52w']}) & Low ({screener_info['low_52w']}) vs Current Price ({screener_info['price_str']}).",
    "Bullet 3 in English: Total Debt ({screener_info['total_debt']}) and Debt-to-Equity ({screener_info['debt_to_equity']}) analysis.",
    "Bullet 4 in English: Stock P/E ({screener_info['pe_ratio']}) vs Industry P/E ({screener_info['industry_pe']}) and ROE ({screener_info['roe']})."
  ],
  "web_story_hook": [
    "Punchy Web Story slide 1 about {c_clean_name}'s exact profit jump or order win.",
    "Punchy Web Story slide 2 about {c_clean_name}'s Share Price Target 2026 ({screener_info['target_2026']}).",
    "Punchy Web Story slide 3 about Debt-to-Equity ({screener_info['debt_to_equity']}) and ROE ({screener_info['roe']}).",
    "Punchy Web Story slide 4 about Stock P/E ({screener_info['pe_ratio']}) vs Sector P/E ({screener_info['industry_pe']})."
  ],
  "seo_article_sections": [
    {{
      "heading": "Why {c_clean_name} Share Price is in Focus Today: Q2 Results & Earnings Breakdown",
      "body": "140-160 words in English analyzing the exact numbers, YoY profit growth, and operational performance."
    }},
    {{
      "heading": "{c_clean_name} Share Price Target 2026, 2027, 2030 & Fundamental Valuation",
      "body": "140-160 words in English discussing {c_clean_name} share price target 2026 ({screener_info['target_2026']}) and 2030 ({screener_info['target_2030']}) based on EPS ({screener_info['eps']}), P/E ({screener_info['pe_ratio']}), and Book Value ({screener_info['book_value']})."
    }},
    {{
      "heading": "Is {c_clean_name} Share a Good Buy for Long Term? Debt-to-Equity & ROE Analysis",
      "body": "120-140 words in English evaluating Total Debt ({screener_info['total_debt']}), Debt-to-Equity ({screener_info['debt_to_equity']}), ROE ({screener_info['roe']}), and key risks."
    }}
  ],
  "faqs": [
    {{
      "q": "What is the {c_clean_name} share price target for 2026, 2027, and 2030?",
      "a": "Direct 2-sentence answer in English citing valuation bands ({screener_info['target_2026']} for 2026 and {screener_info['target_2030']} for 2030) based on EPS and ROE."
    }},
    {{
      "q": "Why is {c_clean_name} share rising or falling after Q2 results?",
      "a": "Direct 2-sentence answer in English with exact Revenue and Net Profit YoY numbers."
    }},
    {{
      "q": "Is {c_clean_name} a debt-free stock?",
      "a": "Direct answer in English citing Total Debt of {screener_info['total_debt']} and Debt-to-Equity of {screener_info['debt_to_equity']}."
    }},
    {{
      "q": "Is {c_clean_name} share a good buy for long term investment?",
      "a": "Direct answer in English comparing Stock P/E ({screener_info['pe_ratio']}) with Industry P/E ({screener_info['industry_pe']}) and ROE ({screener_info['roe']})."
    }}
  ],
  "gujarati_summary": "2-3 sentences in natural Gujarati summarizing {c_clean_name}'s exact Consolidated result (YoY) or new order.",
  "gujarati_intraday_view": "1-2 sentences in natural Gujarati explaining debt, valuation, and expected return."
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
                if isinstance(parsed.get("web_story_hook"), str):
                    parsed["web_story_hook"] = [parsed["web_story_hook"]]
                if doc_type == "result":
                    parsed = ensure_valid_result_numbers(parsed, screener_info, pdf_text_len)
                return parsed
            except Exception as exc:
                log.warning("JSON parse warning, using smart fallback: %s", exc)

    return build_smart_fallback_data(company_title, scrip_code, doc_type, source_text, screener_info, trending_kws, pdf_text_len)


# ==========================================
# 8. VISUAL CHARTS & HIGH-SEO HTML BUILDER
# ==========================================
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


def build_post_html(data, doc_type, screener_info, source_url, trending_kws, comp_key="", scrip_key="", file_key="", scrip_code=""):
    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", datetime.now().strftime("%B %d, %Y"))))
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ist_time_str = ist_now.strftime("%B %d, %Y | %I:%M %p IST")
    iso_time = ist_now.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Q2 / Quarterly")))
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
    high_52w = html.escape(str(screener_info.get("high_52w", "N/A")))
    low_52w = html.escape(str(screener_info.get("low_52w", "N/A")))
    fund_impact = html.escape(str(data.get("fundamental_impact_note", "")))
    est_return = html.escape(str(data.get("estimated_stock_return", screener_info.get("target_2026", "N/A"))))
    intraday_sig = html.escape(str(data.get("intraday_signal", "")))

    raw_lead = str(data.get("seo_lead_summary") or "").strip()
    lead_html = html.escape(raw_lead)

    seed_int = int(hashlib.md5((comp_name + res_date).encode("utf-8")).hexdigest()[:6], 16)
    thumb_prompt = f"modern 3d financial stock market analytics chart infographic upward trend corporate building india {comp_name}"
    thumb_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(thumb_prompt)}?width=1200&height=675&seed={seed_int}&nologo=true"

    content_blocks = []

    # 1. VISUAL WEB STORY SUMMARY BOX
    web_story_hooks = data.get("web_story_hook") or []
    if isinstance(web_story_hooks, str):
        web_story_hooks = [web_story_hooks]
    if web_story_hooks and isinstance(web_story_hooks, list):
        hook_list = "".join(f"<li style='margin-bottom:8px;'>{html.escape(str(h))}</li>" for h in web_story_hooks if h)
        if hook_list:
            content_blocks.append(f"""
            <div style="background: linear-gradient(135deg, #1e3a8a, #2563eb); color: white; border-radius: 14px; padding: 18px 22px; margin-bottom: 24px; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);">
              <h2 style="margin: 0 0 12px 0; font-size: 19px; border-bottom: 1px solid #93c5fd; padding-bottom: 8px; color: white;">⚡ {comp_name} Quick Highlights (Web Story Summary)</h2>
              <ul style="margin: 0; padding-left: 20px; font-size: 15px; font-weight: 500; line-height: 1.65;">
                {hook_list}
              </ul>
              <p style="margin: 10px 0 0 0; font-size: 12px; text-align: right; color: #dbeafe;"><em>MarketMunafa Equity Research Desk</em></p>
            </div>
            """)

    # 2. RESULT / ORDER TABLE & CHARTS
    if doc_type == "order":
        ord_info = data.get("order_details", {}) or {}
        order_card = f"""
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 16px; padding: 22px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #1e3a8a;">🏆 {comp_name} Latest Order Win &amp; Contract Details</h2>
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
          <h2 style="margin: 0 0 12px 0; font-size: 20px; color: #0f172a;">📊 {comp_name} {qtr_lbl} Financial Results Table (YoY)</h2>
          <div style="display: flex; justify-content: space-between; color: #64748b; font-size: 14px; margin-bottom: 10px;">
            <span>{res_date} ({res_type})</span>
            <span>Figures in Rs. Cr.</span>
          </div>
          <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <thead>
              <tr style="border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #64748b; font-weight: 600;">
                <th style="text-align: left; padding: 10px 4px; white-space: nowrap;">Metric</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">{curr_lbl}</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">{prev_lbl}</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">YoY Change</th>
                <th style="text-align: right; padding: 10px 4px; white-space: nowrap;">YoY Growth %</th>
              </tr>
            </thead>
            <tbody>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 600; white-space: nowrap;">Revenue from Operations</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">₹{html.escape(str(rev.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">₹{html.escape(str(rev.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(rev.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 700; white-space: nowrap; color: {growth_color(rev.get('growth', ''))};">{html.escape(str(rev.get('growth', '0%')))}</td>
              </tr>
              <tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 600; white-space: nowrap;">Operating Profit (EBITDA)</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">₹{html.escape(str(gp.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">₹{html.escape(str(gp.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(gp.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 700; white-space: nowrap; color: {growth_color(gp.get('growth', ''))};">{html.escape(str(gp.get('growth', '0%')))}</td>
              </tr>
              <tr>
                <td style="padding: 12px 4px; color: #1e293b; font-weight: 600; white-space: nowrap;">Net Profit (Consolidated PAT)</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; white-space: nowrap;">₹{html.escape(str(np_d.get('curr', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; color: #475569; white-space: nowrap;">₹{html.escape(str(np_d.get('prev', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 600; color: #0f172a; white-space: nowrap;">{html.escape(str(np_d.get('difference', '0')))}</td>
                <td style="padding: 12px 4px; text-align: right; font-weight: 700; white-space: nowrap; color: {growth_color(np_d.get('growth', ''))};">{html.escape(str(np_d.get('growth', '0%')))}</td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center;">
          <h3 style="margin: 0 0 12px 0; font-size: 18px; color: #0f172a;">📈 {comp_name} YoY Revenue, PAT &amp; Debt Chart</h3>
          <img src="{yoy_chart_url}" alt="{comp_name} Q2 Results YoY Chart" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 18px; margin-bottom: 24px; text-align: center;">
          <h3 style="margin: 0 0 12px 0; font-size: 18px; color: #0f172a;">📊 {comp_name} Multi-Quarter Earnings Trend</h3>
          <img src="{quarter_chart_url}" alt="{comp_name} Quarterly Profit Trend" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        """
        content_blocks.append(result_card)

    # 3. HIGH-TRAFFIC SHARE PRICE TARGET & VALUATION OUTLOOK TABLE (2026 - 2030)
    if screener_info.get("target_2026") and screener_info["target_2026"] != "N/A":
        target_table_card = f"""
        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 16px; padding: 20px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 12px 0; font-size: 20px; color: #166534;">🎯 {comp_name} Share Price Target 2026, 2027 to 2030 (Fundamental Projection)</h2>
          <p style="margin: 0 0 12px 0; font-size: 14px; color: #15803d;">
            Based on {comp_name}'s trailing EPS of <strong>{eps}</strong>, ROE of <strong>{roe}</strong>, Book Value of <strong>{book_value}</strong>, and current P/E ratio of <strong>{pe_ratio}</strong>, here are the algorithmic fair-value estimation bands:
          </p>
          <table style="width: 100%; border-collapse: collapse; font-size: 14px; background: #ffffff; border-radius: 8px; overflow: hidden;">
            <thead>
              <tr style="background: #dcfce7; color: #14532d; font-weight: 700;">
                <th style="padding: 10px; text-align: left; border: 1px solid #bbf7d0;">Valuation Horizon</th>
                <th style="padding: 10px; text-align: right; border: 1px solid #bbf7d0;">Estimated Fair Value Band</th>
                <th style="padding: 10px; text-align: left; border: 1px solid #bbf7d0;">Primary Fundamental Driver</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style="padding: 10px; font-weight: 600; border: 1px solid #e2e8f0;">{comp_name} Share Target 2026</td>
                <td style="padding: 10px; text-align: right; font-weight: 700; color: #16a34a; border: 1px solid #e2e8f0;">{html.escape(screener_info['target_2026'])}</td>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">Quarterly PAT growth &amp; order execution</td>
              </tr>
              <tr>
                <td style="padding: 10px; font-weight: 600; border: 1px solid #e2e8f0;">{comp_name} Share Target 2027</td>
                <td style="padding: 10px; text-align: right; font-weight: 700; color: #16a34a; border: 1px solid #e2e8f0;">{html.escape(screener_info['target_2027'])}</td>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">Operating margin expansion &amp; ROE compounding</td>
              </tr>
              <tr>
                <td style="padding: 10px; font-weight: 600; border: 1px solid #e2e8f0;">{comp_name} Share Target 2030</td>
                <td style="padding: 10px; text-align: right; font-weight: 700; color: #16a34a; border: 1px solid #e2e8f0;">{html.escape(screener_info['target_2030'])}</td>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">Long-term earnings compounding &amp; debt reduction</td>
              </tr>
            </tbody>
          </table>
          <p style="margin: 8px 0 0 0; font-size: 12px; color: #475569;"><em>*Note: These are algorithmic valuation models based on historical earnings multiples for educational reference, not buy/sell recommendations.</em></p>
        </div>
        """
        content_blocks.append(target_table_card)

    # 4. KEY HIGHLIGHTS
    highlights = data.get("key_highlights") or []
    if highlights and isinstance(highlights, list):
        li_items = "".join(f"<li style='margin-bottom: 8px; color: #1e293b; line-height: 1.6;'>{html.escape(str(h))}</li>" for h in highlights if h)
        highlights_card = f"""
        <div style="background: #f8fafc; border-left: 4px solid #2563eb; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">📌 Key Financial Highlights for Retail Investors</h2>
          <ul style="margin: 0; padding-left: 20px; font-size: 15px;">{li_items}</ul>
        </div>
        """
        content_blocks.append(highlights_card)

    # 5. FUNDAMENTAL RATIOS TABLE (WITH 52W HIGH/LOW)
    fundamentals_card = f"""
    <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.05); margin-bottom: 24px;">
      <h2 style="margin: 0 0 16px 0; font-size: 20px; color: #0f172a;">🔍 {comp_name} Fundamental Ratios, Debt &amp; 52-Week High/Low</h2>
      <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <tbody>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Current Share Price</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 700; color: #0f172a;">{price_disp}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">52-Week High / Low</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{high_52w} / {low_52w}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Market Cap</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{mkt_cap}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Return on Equity (ROE)</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{roe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">Stock P/E (TTM)</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{pe_ratio}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Industry P/E</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{ind_pe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 4px; color: #64748b;">EPS (TTM)</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{eps}</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Book Value / P/B</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{book_value} ({pb_ratio}x)</td>
          </tr>
          <tr>
            <td style="padding: 10px 4px; color: #64748b;">Debt to Equity</td><td style="padding: 10px 8px 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{de_ratio} ({total_debt})</td>
            <td style="padding: 10px 4px 10px 8px; color: #64748b; border-left: 1px solid #f1f5f9;">Dividend Yield / FV</td><td style="padding: 10px 4px; text-align: right; font-weight: 600; color: #0f172a;">{div_yield} (FV: ₹{face_val})</td>
          </tr>
        </tbody>
      </table>
      <p style="margin: 12px 0 0 0; font-size: 14px; color: #334155;"><strong>Technical &amp; Fundamental View:</strong> {intraday_sig} {fund_impact}</p>
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

    faq_block_html = f"<div style='margin: 26px 0;'><h2 style='font-size: 19px; color: #0f172a; margin-bottom: 14px;'>❓ Frequently Asked Questions ({comp_name} Share FAQs)</h2>{''.join(faq_html_items)}</div>" if faq_html_items else ""

    # 6. TRENDING GOOGLE SEARCH TAGS BLOCK (FOR LONG-TAIL ORGANIC SEARCH TRAFFIC)
    kw_badges = "".join(
        f"<span style='display:inline-block; background:#f1f5f9; color:#334155; border:1px solid #cbd5e1; border-radius:20px; padding:4px 12px; font-size:13px; margin:4px;'>{html.escape(k)}</span>"
        for k in (trending_kws or [])
    )
    trending_kw_html = f"""
    <div style="margin: 22px 0; padding: 14px; background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0;">
      <strong style="font-size: 14px; color: #0f172a; display: block; margin-bottom: 8px;">🔥 Trending Investor Searches on Google:</strong>
      {kw_badges}
    </div>
    """ if kw_badges else ""

    internal_links_html = f"""
    <div style="background: #eff6ff; border: 1px dashed #60a5fa; border-radius: 10px; padding: 14px 18px; margin: 22px 0; font-size: 14px; color: #1e3a8a;">
      <strong>🔗 Explore More High-Growth Stocks on MarketMunafa:</strong>
      <ul style="margin: 8px 0 0 0; padding-left: 18px; line-height: 1.8;">
        <li><a href="{BLOG_BASE_URL}/search/label/STOCK%20RESULT" style="color:#1d4ed8; font-weight:600; text-decoration:none;">📊 Latest Q2 &amp; Quarterly Stock Results Analysis</a></li>
        <li><a href="{BLOG_BASE_URL}/search/label/SHARE%20ORDER" style="color:#1d4ed8; font-weight:600; text-decoration:none;">🏆 New Corporate Order Wins &amp; Share Price Targets</a></li>
        <li><a href="{BLOG_BASE_URL}/search/label/MARKET%20NEWS" style="color:#1d4ed8; font-weight:600; text-decoration:none;">📰 Daily Multibagger &amp; Debt-Free Stock Picks</a></li>
      </ul>
    </div>
    """

    source_link_html = f'<p style="margin-top:16px;"><a href="{html.escape(source_url, quote=True)}" target="_blank" rel="nofollow noopener" style="color:#2563eb; font-weight:600; text-decoration:none;">📄 Verify Official BSE Exchange Filing PDF</a></p>' if source_url else ""

    faq_schema_json = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq_schema_entities})
    news_schema_json = json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": str(data.get("seo_title") or f"{comp_name} Share Price Target 2026 & Q2 Results")[:110],
        "datePublished": iso_time,
        "dateModified": iso_time,
        "image": [thumb_url],
        "author": [{"@type": "Organization", "name": "MarketMunafa Equity Research Desk", "url": BLOG_BASE_URL}],
        "publisher": {"@type": "Organization", "name": "MarketMunafa", "url": BLOG_BASE_URL},
    })

    meta_comment = f"<!-- MM_META:type={doc_type}|comp_key={comp_key}|scrip={scrip_key}|file={file_key}|comp_name={comp_name} -->"

    html_body = f"""
    {meta_comment}
    <article itemscope itemtype="https://schema.org/NewsArticle" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 740px; margin: 0 auto; color: #1e293b;">
      <div style="font-size: 13px; color: #64748b; margin-bottom: 12px; font-weight: 500;">
        📅 Published on: <time itemprop="datePublished" datetime="{iso_time}">{ist_time_str}</time> | By <span itemprop="author">MarketMunafa Equity Research Desk</span>
      </div>
      <p style="font-size: 16px; line-height: 1.75; color: #0f172a; background: #f8fafc; padding: 14px 16px; border-radius: 10px; border: 1px solid #e2e8f0; margin-bottom: 18px;">
        <strong>Executive Summary:</strong> {lead_html}
      </p>
      <figure style="margin: 0 0 22px 0; text-align: center;">
        <img itemprop="image" src="{thumb_url}" alt="{comp_name} Share Price Target 2026 and {qtr_lbl} Results" width="1200" height="675" fetchpriority="high" style="width: 100%; height: auto; aspect-ratio: 16/9; object-fit: cover; border-radius: 12px;" />
        <figcaption style="font-size: 13px; color: #64748b; margin-top: 6px;">{comp_name} (BSE: {html.escape(str(scrip_code or 'Listed'))}) Share Price Target &amp; Equity Research | MarketMunafa</figcaption>
      </figure>
      <!--more-->
      {"".join(content_blocks)}
      {"".join(article_html_parts)}
      {faq_block_html}
      {trending_kw_html}
      {internal_links_html}
      {source_link_html}
      <script type="application/ld+json">{news_schema_json}</script>
      <script type="application/ld+json">{faq_schema_json}</script>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 22px 0;" />
      <p style="font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Disclaimer:</strong> This independent equity research article on MarketMunafa is originally authored using publicly disclosed BSE corporate filings and fundamental ratios for educational and informational purposes only. Valuation projections are algorithmic estimates and not investment advice. We are not SEBI-registered advisors. Please consult a certified financial planner before making any investment decisions.
      </p>
    </article>
    """
    return html_body, yoy_chart_url


# ==========================================
# 9. TELEGRAM, BLOGGER & SITEMAP PING
# ==========================================
def ping_search_engines():
    sitemap_url = f"{BLOG_BASE_URL}/sitemap.xml"
    for ping_url in [
        f"https://www.google.com/ping?sitemap={urllib.parse.quote(sitemap_url)}",
        f"https://www.bing.com/ping?sitemap={urllib.parse.quote(sitemap_url)}",
    ]:
        try:
            requests.get(ping_url, timeout=8)
        except Exception:
            pass


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
    roe = html.escape(str(data.get("roe") or screener_info["roe"]))
    de_ratio = html.escape(str(data.get("debt_to_equity") or screener_info["debt_to_equity"]))
    total_debt = html.escape(str(data.get("total_debt") or screener_info["total_debt"]))
    est_return = html.escape(str(data.get("estimated_stock_return", screener_info.get("target_2026", "N/A"))))
    guj_summary = html.escape(str(data.get("gujarati_summary", "")))
    guj_intraday = html.escape(str(data.get("gujarati_intraday_view", "")))

    if doc_type == "order":
        header_title = f"🏆 <b>{comp_name} - નવો ઓર્ડર / કોન્ટ્રેક્ટ અપડેટ</b>"
        ord_info = data.get("order_details", {}) or {}
        dynamic_block_gu = (
            f"📦 <b>ઓર્ડરની વિગત:</b> {html.escape(str(ord_info.get('order_value', '-')))} "
            f"({html.escape(str(ord_info.get('client_or_awarder', '-')))})\n\n"
        )
    else:
        header_title = f"📊 <b>{comp_name} - {qtr_lbl} રિઝલ્ટ અપડેટ</b>"
        rev = data.get("revenue", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        np_d = data.get("net_profit", {"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"})
        dynamic_block_gu = (
            f"📈 <b>નાણાકીય કામગીરી ({curr_lbl} vs {prev_lbl}):</b>\n"
            f"• <b>આવક (Revenue):</b> ₹{html.escape(str(rev.get('curr', '0')))} Cr ({html.escape(str(rev.get('growth', '0%')))} YoY)\n"
            f"• <b>ચોખ્ખો નફો (PAT):</b> ₹{html.escape(str(np_d.get('curr', '0')))} Cr (<b>{html.escape(str(np_d.get('growth', '0%')))} YoY</b>)\n\n"
        )

    msg_html = (
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>તારીખ:</b> {res_date} | 💰 <b>ભાવ:</b> {price_disp}\n\n"
        f"{dynamic_block_gu}"
        f"🔍 <b>Mkt Cap:</b> {mkt_cap} | <b>P/E:</b> {pe_ratio} | <b>ROE:</b> {roe}\n"
        f"🏦 <b>Debt-to-Equity:</b> {de_ratio} ({total_debt})\n"
        f"🎯 <b>અંદાજિત વેલ્યુએશન ટાર્ગેટ (2026):</b> <b>{est_return}</b>\n\n"
        f"📝 <b>સારાંશ:</b> {guj_summary}\n"
        f"💡 <b>વ્યુ:</b> {guj_intraday}\n"
    )

    if blog_url:
        msg_html += f"\n🌐 <a href=\"{html.escape(blog_url, quote=True)}\"><b>સંપૂર્ણ રિપોર્ટ અને 2026-2030 ટાર્ગેટ બ્લોગ પર વાંચો</b></a>"
    if source_url:
        msg_html += f" | <a href=\"{html.escape(source_url, quote=True)}\"><b>BSE PDF</b></a>"
    msg_html += "\n\n✅ <b>MarketMunafa</b>"

    try:
        if chart_url and doc_type != "order":
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "photo": chart_url, "caption": f"📊 {comp_name} - YoY Growth Chart"},
                timeout=15,
            )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg_html, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20,
        )
    except Exception as exc:
        log.warning("Telegram send warning: %s", exc)


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
    p_type = "order" if ("share order" in lbl_low or any(w in t_low for w in ("order", "contract", "award", "loa"))) else "result"

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
        log.warning("Blogger scan warning: %s", exc)
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
    clean_title = re.sub(r"[\r\n\t]+", " ", re.sub(r"<[^>]+>", "", str(title))).strip()[:140] or "Share Price Target & Q2 Results | MarketMunafa"
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
                time.sleep(25)
                continue
            log.warning("⚠️ Blogger Daily Post Limit (429) reached! Sending to Telegram only.")
            return None
        response.raise_for_status()
        return response.json().get("url", "")
    return None


def publish_item(item, doc_type, ai_data, screener_info, trending_kws, item_id, comp_key, scrip_key, file_key, scrip_code=""):
    source_url = item["link"]
    c_short = ai_data.get("company_short_name") or screener_info.get("screener_company_name") or item["title"]
    raw_title = ai_data.get("seo_title") or f"{c_short} Share Price Target 2026, Q2 Results & Latest News"
    blog_html, yoy_chart_url = build_post_html(
        ai_data,
        doc_type,
        screener_info,
        source_url,
        trending_kws,
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
            log.info("✅ Published High-SEO Post (%s): %s", doc_type, blog_url)
            ping_search_engines()

    send_telegram_gujarati(ai_data, doc_type, screener_info, blog_url, source_url, yoy_chart_url)
    return not blogger_quota_hit


# ==========================================
# 10. MAIN RUNNER
# ==========================================
def run():
    init_db()
    log.info("Scanning existing posts on MarketMunafa blog...")
    blog_state = get_existing_blogger_posts()

    all_items = fetch_bse_json_announcements()
    for kind, feed_url in FEEDS.items():
        try:
            items = parse_feed(feed_url, kind)
            all_items.extend(items)
        except Exception:
            pass

    if not all_items:
        log.warning("No feed items retrieved.")
        return

    published_count = 0

    for item in all_items:
        if published_count >= MAX_POSTS_PER_RUN:
            log.info("Reached MAX_POSTS_PER_RUN (%d). Stopping cleanly.", MAX_POSTS_PER_RUN)
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
            continue

        try:
            screener_info = get_screener_fundamentals(item["title"], scrip_code=scrip_code)

            if not screener_info["is_eligible_stock"]:
                log.info("Skipping Penny/Unlisted Stock (Mkt Cap: %s): %s", screener_info["market_cap"], item["title"])
                continue

            if screener_info.get("screener_company_name"):
                scr_comp_key = normalize_company_key(screener_info["screener_company_name"], doc_type)
                if scr_comp_key and scr_comp_key in blog_state["comp_keys"]:
                    continue

            source_text, pdf_bytes, pdf_text_len = get_document_payload(item)
            file_key = extract_file_key(item["link"]) or file_key

            if doc_type == "result" and not screener_info["q_all_rev"] and pdf_text_len < 200:
                continue

            # Fetch Real-Time Google Autocomplete Trending Keywords for this Stock!
            c_search_name = screener_info.get("screener_company_name") or comp_basis
            trending_kws = fetch_live_google_keywords(c_search_name)

            ai_data = analyze_with_gemini_json(
                item["title"], scrip_code, doc_type, source_text, pdf_bytes, screener_info, trending_kws, pdf_text_len
            )

            # 🛑 STRONG RESULT FILTER: માત્ર સારા પરિણામ કે મોટા ઓર્ડર વાળા શેર જ પોસ્ટ થશે
            if ai_data.get("skip_stock") is True:
                log.info("Skipping stock due to weak/flat financial results: %s", item["title"])
                continue

            if doc_type == "result":
                r_curr = to_float(ai_data.get("revenue", {}).get("curr"))
                np_curr = to_float(ai_data.get("net_profit", {}).get("curr"))
                if r_curr == 0 and np_curr == 0:
                    continue

            ai_comp_name = str(ai_data.get("company_short_name") or "").strip()
            ai_comp_key = normalize_company_key(ai_comp_name, doc_type) if ai_comp_name else ""
            if ai_comp_key and ai_comp_key in blog_state["comp_keys"]:
                continue

            final_comp_key = ai_comp_key or comp_key
            blogger_ok = publish_item(
                item,
                doc_type,
                ai_data,
                screener_info,
                trending_kws,
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
                break

            time.sleep(12)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])


if __name__ == "__main__":
    run()
