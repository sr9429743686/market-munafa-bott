import os
import time
import re
import json
import html
import random
import hashlib
import logging
import sqlite3
import tempfile
import warnings
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

warnings.filterwarnings("ignore")

from curl_cffi import requests as c_requests
import requests
import xml.etree.ElementTree as ET
import PyPDF2
from bs4 import BeautifulSoup
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import google.generativeai as genai

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ==========================================
# 1. CONFIGURATION & CREDENTIALS (FROM ENV / GITHUB SECRETS)
# ==========================================
IST_TZ = timezone(timedelta(hours=5, minutes=30))

BLOG_ID = os.getenv("BLOG_ID", "6996150531679127159").strip('"\' ')
BLOG_BASE_URL = os.getenv("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").strip('"\' ').rstrip("/")
SITE_LOGO_URL = os.getenv(
    "SITE_LOGO_URL",
    "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgXG-GojgyTIZWy4ftRRsgpLsDMxrAEKCRb_BnZsfVTKT0_T9IELdKQYKgACOQkhus69xfx_xg-iVyLvP2-RAki8voVBwja88Dfsg_tQRQR7JvgegMGFfEQfm73oO00WeTo1vPUHghTXmXd_by6k0C-m3Y_5JhoYEjUF8Y8V3KKTbGnUn53dUY-6AN32_A/s1254/1001173067.png",
).strip('"\' ')

GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip('"\' ')
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip('"\' ')
GOOGLE_REFRESH_TOKEN = (os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("REFRESH_TOKEN") or "").strip('"\' ')

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip('"\' ')
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip('"\' ')

RAW_GEMINI = " ".join([
    os.getenv("GEMINI_API_KEY", ""),
    os.getenv("GEMINI_API_KEYS", ""),
    os.getenv("GEMINI_API_KEY_1", ""),
    os.getenv("GEMINI_API_KEY_2", ""),
])
GEMINI_API_KEYS = list(dict.fromkeys([
    k.strip('"\' ') for k in re.split(r"[,\s;]+", RAW_GEMINI) if len(k.strip('"\' ')) > 10
]))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip('"\' ')
GEMINI_MAX_RPM = 12
GEMINI_MAX_TPM = 240000
GEMINI_MAX_RPD = 500

OPENROUTER_KEYS = [
    k.strip('"\' ') for k in re.split(r"[,\s;]+", os.getenv("OPENROUTER_API_KEY", "")) if len(k.strip('"\' ')) > 10
]
OPENROUTER_MAX_RPM = 5
OPENROUTER_MAX_RPD = 20
RATE_WINDOW_SECONDS = 60.0

# GitHub & Thumbnail Settings
GITHUB_REPO = (os.getenv("GITHUB_REPO") or os.getenv("GITHUB_REPOSITORY") or "").strip('"\' ')
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip('"\' ')
THUMBNAIL_DIR = "thumbnails"
os.makedirs(THUMBNAIL_DIR, exist_ok=True)
AUTHOR_IMAGE_CANDIDATES = [
    "1001185905_2.png",
    "author.png",
    "1001185905.png",
    "author.jpg",
    "author.jpeg",
]

# Strict 8 Approved Blog Categories
APPROVED_LABELS = [
    "STOCKS RESULT",
    "MARKET NEWS",
    "IPO",
    "INTRADAY",
    "TOP 10 STOCKS",
    "DEBT FREE 10 STOCKS",
    "GOLD",
    "SILVER",
]

# Quality, Safety & 200/200 Gate Settings
MIN_MARKET_CAP_CR = float(os.getenv("MIN_MARKET_CAP_CR", "500.0"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "10"))
REQUIRED_TOTAL_SCORE = 200    # 100 Python Hard Gate + 100 Chief Editor Audit = 200/200 ફરજિયાત
MAX_AUDIT_FIX_ROUNDS = 3
MIN_ENGLISH_WORDS = 460

FEEDS = {
    "result": "https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx",
    "announcement": "https://www.bseindia.com/data/xml/announcements.aspx",
}

DB_PATH = os.getenv("DB_PATH", "marketmunafa_seen.sqlite3")
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "100"))
MAX_BLOG_POSTS_TO_SCAN = int(os.getenv("MAX_BLOG_POSTS_TO_SCAN", "500"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "20"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "35"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

# AI જેવા લાગતા રોબોટિક શબ્દો તથા AdSense/YMYL પ્રતિબંધિત શબ્દો
BANNED_AI_AND_POLICY_PHRASES = [
    "delve into", "tapestry", "in conclusion", "game-changer", "testament to",
    "ever-evolving", "rollercoaster", "pivotal moment", "underscores the importance",
    "double-edged sword", "landscape of", "in today's fast-paced", "it is worth noting",
    "all eyes are on", "only time will tell", "guaranteed return", "sure shot multibagger",
    "strong buy recommendation", "target price 2027", "target price 2028", "target price 2030",
    "विकास ऑर्डर", "यह ध्यान देने योग्य है", "निष्कर्ष के तौर पर",
    "અંતમાં કહીએ તો", "એ નોંધવું મહત્વપૂર્ણ છે",
]

# ભારતીય આંકડા (૦-૯, ०-९) ને ફરજિયાત અંગ્રેજી 0-9 માં ફેરવવા માટે
INDIC_DIGIT_TRANS = str.maketrans({
    chr(base + d): str(d)
    for base in [0x0966, 0x0AE6]
    for d in range(10)
})

# દરેક પોસ્ટ માટે અલગ-અલગ 6 પ્રીમિયમ એડિટોરિયલ થમ્બનેલ થીમ
THUMB_THEMES = [
    {"bg1": (10, 22, 46), "bg2": (24, 58, 108), "accent": (250, 204, 21), "accent_txt": (15, 23, 42), "badge": (16, 150, 72), "glow": (56, 189, 248), "chart": (34, 197, 94)},
    {"bg1": (8, 34, 34), "bg2": (16, 86, 80), "accent": (253, 224, 71), "accent_txt": (15, 23, 42), "badge": (234, 88, 12), "glow": (45, 212, 191), "chart": (74, 222, 128)},
    {"bg1": (26, 14, 48), "bg2": (68, 28, 118), "accent": (251, 191, 36), "accent_txt": (15, 23, 42), "badge": (225, 29, 72), "glow": (192, 132, 252), "chart": (56, 189, 248)},
    {"bg1": (20, 24, 34), "bg2": (48, 58, 82), "accent": (56, 189, 248), "accent_txt": (15, 23, 42), "badge": (22, 163, 74), "glow": (250, 204, 21), "chart": (52, 211, 153)},
    {"bg1": (36, 14, 20), "bg2": (98, 26, 40), "accent": (250, 204, 21), "accent_txt": (15, 23, 42), "badge": (14, 116, 144), "glow": (251, 146, 60), "chart": (250, 204, 21)},
    {"bg1": (12, 32, 22), "bg2": (24, 78, 52), "accent": (253, 224, 71), "accent_txt": (15, 23, 42), "badge": (37, 99, 235), "glow": (74, 222, 128), "chart": (134, 239, 172)},
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketmunafa")


# ==========================================
# 2. MULTI-KEY ROTATOR
# ==========================================
class MultiKeyRotator:
    def __init__(self, name, keys, max_per_min, max_per_day, max_tpm=240000, window_sec=60.0):
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

    def acquire_key(self, estimated_tokens=6000):
        num_keys = len(self.keys)
        if num_keys == 0:
            return None, None

        for _ in range(4):
            if len(self.exhausted_today) >= num_keys:
                log.warning("All %s keys have reached daily quota (%d/day).", self.name, self.max_per_day)
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
                        "Using %s Key #%d/%d | RPM: %d/%d | RPD: %d/%d",
                        self.name, idx + 1, num_keys, curr_rpm + 1, self.max_per_min,
                        self.daily_count[idx], self.max_per_day,
                    )
                    return idx, self.keys[idx]
                else:
                    w = (self.usage_history[idx][0][0] + self.window_sec) - now + 1.5
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)

            if len(self.exhausted_today) >= num_keys:
                return None, None

            wait_sec = max(3.0, min(earliest_wait or 10.0, 35.0))
            log.info("Waiting %.1fs for %s rate-limit window...", wait_sec, self.name)
            time.sleep(wait_sec)

        return None, None

    def mark_key_rate_limited(self, idx, err_msg="", cooldown_sec=65.0):
        if idx is None:
            return
        e_low = str(err_msg).lower()
        if any(w in e_low for w in ("depleted", "daily", "per_day", "invalid", "api_key_invalid", "unauthorized", "401")):
            self.exhausted_today.add(idx)
        else:
            self.cooldown_until[idx] = time.time() + cooldown_sec


GEMINI_ROTATOR = MultiKeyRotator("Gemini", GEMINI_API_KEYS, GEMINI_MAX_RPM, GEMINI_MAX_RPD, GEMINI_MAX_TPM, RATE_WINDOW_SECONDS)
OPENROUTER_ROTATOR = MultiKeyRotator("OpenRouter Backup", OPENROUTER_KEYS, OPENROUTER_MAX_RPM, OPENROUTER_MAX_RPD, 240000, RATE_WINDOW_SECONDS)


# ==========================================
# 3. GENERAL & DEDUPLICATION HELPERS
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


def get_ist_now():
    return datetime.now(IST_TZ)


def enforce_english_digits(obj):
    if isinstance(obj, str):
        return obj.translate(INDIC_DIGIT_TRANS)
    if isinstance(obj, list):
        return [enforce_english_digits(x) for x in obj]
    if isinstance(obj, dict):
        return {k: enforce_english_digits(v) for k, v in obj.items()}
    return obj


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


def classify_market_cap_label(mcap_cr):
    if mcap_cr >= 20000:
        return "Large Cap"
    if mcap_cr >= 5000:
        return "Mid Cap"
    if mcap_cr >= 500:
        return "Small Cap"
    return "Micro Cap"


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


def shorten_period_label(lbl):
    s = str(lbl or "").strip()
    if not s:
        return "Current"
    m_date = re.search(r"(?:30|31|28|29)?[\s./-]*(0?3|0?6|0?9|12)[\s./-]*(20\d{2}|\d{2})\b", s)
    if m_date:
        mm = int(m_date.group(1))
        yy = m_date.group(2)
        if len(yy) == 2:
            yy = "20" + yy
        month_map = {3: "Mar", 6: "Jun", 9: "Sep", 12: "Dec"}
        return f"{month_map.get(mm, 'Sep')} {yy}"

    m_word = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s,/-]*(\d{1,2}[\s,/-]*)?(20\d{2}|\d{2})\b", s, flags=re.I)
    if m_word:
        mon = m_word.group(1).capitalize()[:3]
        yy = m_word.group(3)
        if len(yy) == 2:
            yy = "20" + yy
        return f"{mon} {yy}"
    return s[:12]


# ==========================================
# 4. CUSTOM AUTO-THUMBNAIL GENERATOR (UNIQUE PER POST + AUTHOR PHOTO)
# ==========================================
def load_author_photo():
    for fname in AUTHOR_IMAGE_CANDIDATES:
        if os.path.exists(fname):
            log.info("Loaded author photo for thumbnail: %s", fname)
            return Image.open(fname).convert("RGBA")
    for fname in sorted(os.listdir(".")):
        if fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and not fname.startswith("thumb_"):
            log.info("Auto-detected author photo: %s", fname)
            return Image.open(fname).convert("RGBA")
    log.warning("Author photo (1001185905_2.png / author.png) not found in repo root.")
    return None


def remove_white_bg_numpy(img_rgba, threshold=236):
    data = np.array(img_rgba)
    r, g, b, a = data[:, :, 0], data[:, :, 1], data[:, :, 2], data[:, :, 3]
    white_mask = (r >= threshold) & (g >= threshold) & (b >= threshold)
    data[white_mask, 3] = 0
    near_white = (~white_mask) & (r >= threshold - 20) & (g >= threshold - 20) & (b >= threshold - 20) & (a > 0)
    avg = ((r[near_white].astype(np.int32) + g[near_white].astype(np.int32) + b[near_white].astype(np.int32)) // 3)
    data[near_white, 3] = np.clip((threshold - avg) * 12, 0, 255).astype(np.uint8)
    return Image.fromarray(data)


def load_thumb_font(size, bold=True):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "arialbd.ttf",
        "arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def wrap_lines_to_px(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text_box(draw, text, max_w, start_sz=54, min_sz=28, max_lines=2):
    for sz in range(start_sz, min_sz - 1, -2):
        fnt = load_thumb_font(sz, bold=True)
        lines = wrap_lines_to_px(draw, text, fnt, max_w)
        if len(lines) <= max_lines and all((draw.textbbox((0, 0), l, font=fnt)[2] - draw.textbbox((0, 0), l, font=fnt)[0]) <= max_w for l in lines):
            return fnt, lines
    fnt = load_thumb_font(min_sz, bold=True)
    return fnt, wrap_lines_to_px(draw, text, fnt, max_w)[:max_lines]


def draw_subtle_market_bg(canvas, th, seed_str):
    W, H = canvas.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    for x in range(0, W, 64):
        odraw.line([(x, 0), (x, H)], fill=(255, 255, 255, 13), width=1)
    for y in range(0, H, 64):
        odraw.line([(0, y), (W, y)], fill=(255, 255, 255, 13), width=1)

    rng = random.Random(seed_str)
    pts = []
    base_y = 560
    for i in range(18):
        cx = 45 + i * 44
        base_y = max(210, min(585, base_y + rng.randint(-36, 20)))
        body_h = rng.randint(24, 68)
        is_green = rng.random() > 0.32
        col = (34, 197, 94, 46) if is_green else (239, 68, 68, 38)
        odraw.line([(cx, max(150, base_y - body_h // 2 - 22)), (cx, min(635, base_y + body_h // 2 + 22))], fill=col, width=3)
        odraw.rounded_rectangle([cx - 8, base_y - body_h // 2, cx + 8, base_y + body_h // 2], radius=3, fill=col)
        pts.append((cx, base_y - 18))

    if len(pts) >= 2:
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).line(pts, fill=(*th["chart"], 130), width=7)
        glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
        overlay = Image.alpha_composite(overlay, glow)
        ImageDraw.Draw(overlay).line(pts, fill=(*th["chart"], 195), width=3)

    return Image.alpha_composite(canvas, overlay)


def create_and_push_unique_thumbnail(comp_short, thumb_headline, badge_text, cap_label, price_disp, slug_id, theme_index=0):
    """1280x720 દરેક પોસ્ટ માટે અલગ થીમ, ઓટો-ફિટ ટેક્સ્ટ અને તમારા ફોટા સાથેનું પ્રોફેશનલ થમ્બનેલ."""
    W, H = 1280, 720
    th = THUMB_THEMES[theme_index % len(THUMB_THEMES)]

    canvas = Image.new("RGBA", (W, H), (*th["bg1"], 255))
    draw = ImageDraw.Draw(canvas)
    for y in range(H):
        t = y / H
        r = int(th["bg1"][0] + (th["bg2"][0] - th["bg1"][0]) * t)
        g = int(th["bg1"][1] + (th["bg2"][1] - th["bg1"][1]) * t)
        b = int(th["bg1"][2] + (th["bg2"][2] - th["bg1"][2]) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

    canvas = draw_subtle_market_bg(canvas, th, slug_id)

    # સ્ટુડિયો લાઈટ ગ્લો અને ડાબી બાજુ રીડેબિલિટી વિગ્નેટ
    lights = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(lights)
    ldraw.ellipse([710, 30, 1310, 650], fill=(*th["glow"], 95))
    ldraw.ellipse([800, 110, 1220, 560], fill=(*th["accent"], 75))
    ldraw.ellipse([-120, -80, 780, 740], fill=(4, 10, 22, 145))
    lights = lights.filter(ImageFilter.GaussianBlur(radius=45))
    canvas = Image.alpha_composite(canvas, lights)

    # જમણી બાજુ તમારો ફોટો + સ્ટુડિયો રિમ-લાઈટ
    author = load_author_photo()
    if author is not None:
        try:
            w_a, h_a = author.size
            corners = [author.getpixel((2, 2)), author.getpixel((w_a - 3, 2)), author.getpixel((2, h_a - 3)), author.getpixel((w_a - 3, h_a - 3))]
            if sum(1 for r, g, b, a in corners if a > 220 and r > 230 and g > 230 and b > 230) >= 2:
                author = remove_white_bg_numpy(author)
            bbox = author.getbbox()
            if bbox:
                author = author.crop(bbox)

            rgb_p = author.convert("RGB")
            rgb_p = ImageEnhance.Contrast(rgb_p).enhance(1.07)
            rgb_p = ImageEnhance.Color(rgb_p).enhance(1.06)
            rgb_p = ImageEnhance.Sharpness(rgb_p).enhance(1.16)
            author_enh = rgb_p.convert("RGBA")
            author_enh.putalpha(author.getchannel("A"))
            author = author_enh

            target_h = 610
            ratio = target_h / author.height
            target_w = int(author.width * ratio)
            if target_w > 540:
                scale = 540 / target_w
                target_w = 540
                target_h = int(target_h * scale)

            author = author.resize((target_w, target_h), Image.Resampling.LANCZOS)
            paste_x = W - target_w - 16
            paste_y = H - target_h - 44

            rim_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            rim_fill = Image.new("RGBA", author.size, (*th["accent"], 210))
            rim_fill.putalpha(author.getchannel("A"))
            rim_layer.paste(rim_fill, (paste_x - 6, paste_y - 4))
            rim_layer = rim_layer.filter(ImageFilter.GaussianBlur(radius=18))
            canvas = Image.alpha_composite(canvas, rim_layer)
            canvas.paste(author, (paste_x, paste_y), author)
        except Exception as exc:
            log.warning("Thumbnail author photo warning: %s", exc)

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, W, 9], fill=(*th["accent"], 255))

    TEXT_X = 56
    MAX_W = 660

    # 1) ટોપ બેજ + કેપ કેટેગરી
    clean_badge = re.sub(r"[^A-Za-z0-9\s\-&|]", "", f"{badge_text} | {cap_label}").strip().upper()[:30]
    f_badge = load_thumb_font(24, bold=True)
    tb = draw.textbbox((0, 0), clean_badge, font=f_badge)
    bw = min(MAX_W, (tb[2] - tb[0]) + 56)
    draw.rounded_rectangle([TEXT_X, 46, TEXT_X + bw, 98], radius=10, fill=(*th["badge"], 255))
    draw.text((TEXT_X + 28, 57), clean_badge, font=f_badge, fill=(255, 255, 255, 255))

    # 2) કંપનીનું નામ (ગોલ્ડન હાઈલાઈટ બેનરમાં, ઓટો-ફિટ — ક્યારેય કપાશે નહીં)
    clean_comp = " ".join(re.sub(r"[^\x20-\x7E&]", "", str(comp_short)).split()).upper()[:26]
    f_comp, comp_lines = fit_text_box(draw, clean_comp, MAX_W - 28, start_sz=54, min_sz=34, max_lines=2)
    y_cur = 128
    for idx, line in enumerate(comp_lines):
        lb = draw.textbbox((0, 0), line, font=f_comp)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        if idx == 0:
            draw.rounded_rectangle([TEXT_X - 6, y_cur - 6, TEXT_X + lw + 26, y_cur + lh + 18], radius=10, fill=(*th["accent"], 255))
            draw.text((TEXT_X + 10, y_cur), line, font=f_comp, fill=(*th["accent_txt"], 255))
            y_cur += lh + 32
        else:
            draw.text((TEXT_X + 4, y_cur + 4), line, font=f_comp, fill=(0, 0, 0, 220))
            draw.text((TEXT_X, y_cur), line, font=f_comp, fill=(255, 255, 255, 255))
            y_cur += lh + 22

    # 3) મુખ્ય આંકડાકીય હેડલાઈન (ઓટો-ફિટ 3D શેડો સાથે)
    clean_hl = " ".join(re.sub(r"[^\x20-\x7E₹%+.-]", "", str(thumb_headline)).split()).upper()
    f_hl, hl_lines = fit_text_box(draw, clean_hl, MAX_W, start_sz=46, min_sz=30, max_lines=3)
    y_cur += 10
    for line in hl_lines:
        lb = draw.textbbox((0, 0), line, font=f_hl)
        lh = lb[3] - lb[1]
        draw.text((TEXT_X + 4, y_cur + 4), line, font=f_hl, fill=(0, 0, 0, 230))
        draw.text((TEXT_X, y_cur), line, font=f_hl, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 220))
        y_cur += lh + 20

    # 4) ભાવ અને BSE ટૅગ પિલ
    clean_price = re.sub(r"[^\x20-\x7E₹,.]", "", str(price_disp)).strip()
    if clean_price and "LIVE" not in clean_price.upper():
        f_sub = load_thumb_font(25, bold=True)
        sub_txt = f"SHARE PRICE: {clean_price}   |   OFFICIAL BSE FILING"
        draw.text((TEXT_X, min(H - 122, y_cur + 14)), sub_txt, font=f_sub, fill=(203, 213, 225, 255))

    # 5) નીચે ફુલ-વિડ્થ ન્યૂઝ બ્રાન્ડ બાર
    bar_y = H - 72
    draw.rectangle([0, bar_y, W, H], fill=(10, 15, 28, 248))
    draw.rectangle([0, bar_y, W, bar_y + 4], fill=(*th["accent"], 255))

    f_brand = load_thumb_font(27, bold=True)
    f_meta = load_thumb_font(23, bold=False)
    draw.rounded_rectangle([46, bar_y + 14, 326, H - 12], radius=7, fill=(*th["accent"], 255))
    draw.text((64, bar_y + 21), "MARKET MUNAFA", font=f_brand, fill=(15, 23, 42, 255))

    date_str = get_ist_now().strftime("%d %b %Y").upper()
    draw.text((350, bar_y + 23), f"BSE EQUITY RESEARCH   •   {date_str}", font=f_meta, fill=(226, 232, 240, 255))

    filename = f"thumb_{slug_id}.jpg"
    out_path = os.path.join(THUMBNAIL_DIR, filename)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)
    log.info("Generated custom unique thumbnail: %s", out_path)

    if GITHUB_REPO:
        try:
            subprocess.run(["git", "config", "--local", "user.email", "action@github.com"], check=False)
            subprocess.run(["git", "config", "--local", "user.name", "MarketMunafa Bot"], check=False)
            subprocess.run(["git", "add", out_path], check=True)
            subprocess.run(["git", "commit", "-m", f"Add thumbnail {filename}"], check=False)
            push_res = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push_res.returncode == 0:
                log.info("Pushed thumbnail to GitHub: %s", filename)
            else:
                log.warning("Git push thumbnail warning: %s", push_res.stderr.strip())
            time.sleep(2)
        except Exception as exc:
            log.warning("Thumbnail git push failed: %s", exc)
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{THUMBNAIL_DIR}/{filename}"

    return ""


# ==========================================
# 5. SCREENER FUNDAMENTALS (CONSOLIDATED PRIORITY)
# ==========================================
def _extract_quarters_from_soup(soup):
    q_data = {
        "quarters_text": "", "q_all_labels": [], "q_all_rev": [], "q_all_gp": [], "q_all_np": [],
        "q_labels": [], "q_rev": [], "q_gp": [], "q_np": [],
    }
    q_sec = soup.find("section", id="quarters")
    if not q_sec:
        return q_data

    q_lines, headers = [], []
    for row in q_sec.find_all("tr"):
        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]
        if not cols:
            continue
        if not headers and len(cols) > 3:
            headers = cols[1:]
            q_data["q_all_labels"] = headers
            q_data["q_labels"] = headers[-5:]
            continue

        r_title = cols[0].lower().replace("+", "").strip()
        all_nums = [to_float(c_val) for c_val in cols[1:]]
        nums = all_nums[-5:]

        if r_title in ("sales", "revenue", "revenue from operations", "interest earned") and any(all_nums):
            q_data["q_all_rev"], q_data["q_rev"] = all_nums, nums
            q_lines.append(" | ".join(cols))
        elif r_title in ("operating profit", "ebitda", "financing profit") and any(all_nums):
            q_data["q_all_gp"], q_data["q_gp"] = all_nums, nums
            q_lines.append(" | ".join(cols))
        elif r_title in ("net profit", "profit after tax") and any(all_nums):
            q_data["q_all_np"], q_data["q_np"] = all_nums, nums
            q_lines.append(" | ".join(cols))

    if headers and q_lines:
        q_data["quarters_text"] = "Quarters Header: " + " | ".join(headers) + "\n" + "\n".join(q_lines)
    return q_data


def get_screener_fundamentals(comp_title, scrip_code=""):
    clean_name = re.sub(r"\(\d+\)", "", comp_title).split("-")[0].strip()
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", clean_name, flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]

    queries = []
    if scrip_code:
        queries.append(str(scrip_code).strip())
    if clean_name:
        queries.append(clean_name)

    info = {
        "screener_company_name": "", "nse_bse_symbol": scrip_code or "BSE",
        "price_str": "Live on BSE", "price_num": 0.0,
        "high_52w": "N/A", "high_52w_num": 0.0, "low_52w": "N/A", "low_52w_num": 0.0,
        "dist_from_high": "N/A", "dist_from_low": "N/A",
        "market_cap": "N/A", "market_cap_num": 0.0, "cap_label": "Small Cap", "is_eligible_stock": False,
        "pe_ratio": "N/A", "pb_ratio": "N/A", "industry_pe": "N/A", "roe": "N/A", "roce": "N/A",
        "eps": "N/A", "div_yield": "N/A", "book_value": "N/A",
        "total_debt": "₹0 Cr", "curr_debt_num": 0, "prev_debt_num": 0,
        "debt_to_equity": "0.00", "debt_to_equity_num": 0.0, "face_value": "N/A",
        "quarters_text": "", "q_all_labels": [], "q_all_rev": [], "q_all_gp": [], "q_all_np": [],
        "q_labels": [], "q_rev": [], "q_gp": [], "q_np": [],
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

                raw_url = str(first_hit["url"]).strip()
                base_clean_url = re.sub(r"/consolidated/?$", "/", raw_url)
                cons_url = base_clean_url.rstrip("/") + "/consolidated/"

                c_res = c_requests.get("https://www.screener.in" + cons_url, impersonate="chrome120", timeout=15)
                if c_res.status_code != 200:
                    c_res = c_requests.get("https://www.screener.in" + raw_url, impersonate="chrome120", timeout=15)
                if c_res.status_code != 200:
                    continue

                page_html = c_res.text
                if scrip_code and not re.search(rf"\b{re.escape(str(scrip_code))}\b", page_html):
                    continue

                soup = BeautifulSoup(c_res.content, "html.parser")
                q_extracted = _extract_quarters_from_soup(soup)

                if not any(q_extracted["q_all_rev"]) and not any(q_extracted["q_all_np"]):
                    st_res = c_requests.get("https://www.screener.in" + base_clean_url, impersonate="chrome120", timeout=15)
                    if st_res.status_code == 200:
                        st_soup = BeautifulSoup(st_res.content, "html.parser")
                        st_q = _extract_quarters_from_soup(st_soup)
                        if any(st_q["q_all_rev"]) or any(st_q["q_all_np"]):
                            soup = st_soup
                            q_extracted = st_q

                info.update(q_extracted)
                info["screener_company_name"] = hit_name

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
                            info["high_52w_num"] = to_float(nums[0])
                            info["low_52w"] = f"₹{nums[1]}"
                            info["low_52w_num"] = to_float(nums[1])
                        elif "Market Cap" in t and num_txt:
                            info["market_cap"] = f"₹{num_txt} Cr"
                            info["market_cap_num"] = to_float(num_txt)
                            info["cap_label"] = classify_market_cap_label(info["market_cap_num"])
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
                            info["debt_to_equity_num"] = float(to_float(num_txt))
                        elif "Industry P/E" in t and num_txt:
                            info["industry_pe"] = num_txt
                        elif "EPS" in t and num_txt:
                            info["eps"] = num_txt
                        elif ("P/B" in t or "Price to book" in t) and num_txt:
                            info["pb_ratio"] = num_txt

                if info["market_cap_num"] >= MIN_MARKET_CAP_CR:
                    info["is_eligible_stock"] = True

                if info["price_num"] > 0:
                    if info["high_52w_num"] > 0:
                        diff_h = ((info["price_num"] - info["high_52w_num"]) / info["high_52w_num"]) * 100
                        info["dist_from_high"] = f"{diff_h:+.1f}% vs 52W High"
                    if info["low_52w_num"] > 0:
                        diff_l = ((info["price_num"] - info["low_52w_num"]) / info["low_52w_num"]) * 100
                        info["dist_from_low"] = f"{diff_l:+.1f}% vs 52W Low"

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
                    de_calc = borrowings / equity
                    info["debt_to_equity"] = f"{de_calc:.2f}"
                    info["debt_to_equity_num"] = round(de_calc, 2)
                break
        except Exception as exc:
            log.warning("Screener lookup warning for %s: %s", q, exc)
    return info


# ==========================================
# 6. BSE API, SCRIP PDF RESOLVER & FEEDS
# ==========================================
def fetch_bse_pdf_url_by_scrip(scrip_code):
    if not scrip_code:
        return "", ""
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y%m%d")
    prev_str = (ist_now - timedelta(days=5)).strftime("%Y%m%d")

    for cat in ("Result", "-1"):
        api_url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
            f"?pageno=1&strCat={cat}&strPrevDate={prev_str}&strScrip={scrip_code}&strSearch=P&strToDate={today_str}&strType=C&subcategory=-1"
        )
        try:
            res = get_url(api_url)
            rows = res.json().get("Table", []) if isinstance(res.json(), dict) else []
            for r in rows:
                attach = str(r.get("ATTACHMENTNAME") or "").strip()
                if attach and attach.lower().endswith(".pdf"):
                    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}", attach
        except Exception:
            pass
    return "", ""


def fetch_bse_json_announcements():
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y%m%d")
    prev_str = (ist_now - timedelta(days=2)).strftime("%Y%m%d")
    output = []

    for kind, cat in [("result", "Result"), ("announcement", "Company+Update")]:
        count = 0
        for page_no in (1, 2, 3):
            api_url = (
                f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
                f"?pageno={page_no}&strCat={cat}&strPrevDate={prev_str}&strScrip=&strSearch=P&strToDate={today_str}&strType=C&subcategory=-1"
            )
            try:
                res = get_url(api_url)
                rows = res.json().get("Table", []) if isinstance(res.json(), dict) else []
                if not rows:
                    break
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
                if count >= MAX_ITEMS_PER_FEED:
                    break
            except Exception as exc:
                log.warning("BSE JSON API warning (%s p%d): %s", cat, page_no, exc)
                break
        log.info("BSE JSON API (%s): %d items fetched", cat, count)
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
            "company_raw": clean_t.split("(")[0].split("-")[0].split(":")[0].strip(),
            "scrip_code": str(scrip).strip(),
            "description": html.unescape(re.sub(r"<[^>]+>", " ", description)).strip(),
            "link": urljoin(feed_url, link) if link else "",
            "guid": child_text(node, {"guid", "id"}) or link or title,
        })
        if len(output) >= MAX_ITEMS_PER_FEED:
            break
    return output


def get_document_payload(item, scrip_code=""):
    parts = [f"COMPANY TITLE: {item['title']}"]
    pdf_bytes = None
    extracted_pdf_text = ""
    if item["description"]:
        parts.append("FEED DESCRIPTION:\n" + item["description"])

    url = item.get("link") or ""
    attach_name = item.get("attachment_name") or ""

    if not url or not urlparse(url).path.lower().endswith(".pdf"):
        resolved_url, resolved_attach = fetch_bse_pdf_url_by_scrip(scrip_code)
        if resolved_url:
            url = resolved_url
            attach_name = resolved_attach
            item["link"] = resolved_url
            item["attachment_name"] = resolved_attach

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
            log.warning("Attachment download warning for %s: %s", url, exc)
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
        "regulation 74(5)", "independent director", "composition of board",
    )
    result_words = (
        "financial result", "unaudited financial", "audited financial",
        "quarterly result", "half year result", "annual result", "financial results",
        "standalone result", "consolidated result",
    )
    if any(w in td for w in ignore_words):
        return ""
    if any(w in td for w in order_words):
        return "order"
    if item["kind"] == "result" or any(w in td for w in result_words):
        return "result"
    return ""


def make_item_id(comp_key, file_key, item):
    return hashlib.sha256((file_key or comp_key or item["guid"] or item["title"]).encode("utf-8")).hexdigest()


def compute_metric_dict(curr_val, prev_val):
    c, p = to_float(curr_val), to_float(prev_val)
    diff = round(c - p, 2)
    diff_str = f"{'+' if diff > 0 else ''}{fmt_num(diff)} Cr"
    if p != 0:
        pct = ((c - p) / abs(p)) * 100
        g_str = f"{'+' if pct > 0 else ''}{pct:.1f}%"
    else:
        g_str = "N/A"
    return {"curr": fmt_num(c), "prev": fmt_num(p), "difference": diff_str, "growth": g_str}


def verify_and_normalize_pdf_result_numbers(ai_data, screener_info):
    rev = ai_data.get("revenue") or {}
    gp = ai_data.get("gross_profit") or {}
    np_d = ai_data.get("net_profit") or {}

    curr_lbl = shorten_period_label(ai_data.get("curr_period_label", "Sep 2026"))
    prev_lbl = shorten_period_label(ai_data.get("prev_period_label", "Sep 2025"))
    ai_data["curr_period_label"] = curr_lbl
    ai_data["prev_period_label"] = prev_lbl

    ai_data["revenue"] = compute_metric_dict(rev.get("curr"), rev.get("prev"))
    ai_data["gross_profit"] = compute_metric_dict(gp.get("curr"), gp.get("prev"))
    ai_data["net_profit"] = compute_metric_dict(np_d.get("curr"), np_d.get("prev"))

    q_labels = list(screener_info.get("q_labels", []))
    q_rev = list(screener_info.get("q_rev", []))
    q_gp = list(screener_info.get("q_gp", []))
    q_np = list(screener_info.get("q_np", []))

    r_c = to_float(ai_data["revenue"]["curr"])
    g_c = to_float(ai_data["gross_profit"]["curr"])
    n_c = to_float(ai_data["net_profit"]["curr"])

    if curr_lbl and r_c > 0:
        if not q_labels or shorten_period_label(q_labels[-1]).lower() != curr_lbl.lower():
            q_labels.append(curr_lbl)
            q_rev.append(r_c)
            q_gp.append(g_c)
            q_np.append(n_c)
        else:
            q_rev[-1] = r_c
            q_gp[-1] = g_c
            q_np[-1] = n_c

    ai_data["quarter_chart_labels"] = q_labels[-6:] if q_labels else [prev_lbl, curr_lbl]
    ai_data["quarter_chart_revenue"] = q_rev[-6:] if q_rev else [to_float(ai_data["revenue"]["prev"]), r_c]
    ai_data["quarter_chart_gross_profit"] = q_gp[-6:] if q_gp else [to_float(ai_data["gross_profit"]["prev"]), g_c]
    ai_data["quarter_chart_net_profit"] = q_np[-6:] if q_np else [to_float(ai_data["net_profit"]["prev"]), n_c]

    return ai_data


# ==========================================
# 7. AI API CALLER & STRICT 200/200 VERIFICATION ENGINE
# ==========================================
class DummyResponse:
    def __init__(self, text):
        self.text = text


def call_ai_api(prompt_text, pdf_bytes=None, require_pdf_vision=False, temperature=0.25):
    est_tokens = max(3000, int(len(prompt_text) / 3.5) + (4000 if pdf_bytes else 0))

    if GEMINI_API_KEYS:
        gemini_models = list(dict.fromkeys([m for m in [GEMINI_MODEL, "gemini-3.5-flash-lite"] if m]))

        for attempt_round in range(2):
            for _ in range(len(GEMINI_ROTATOR.keys)):
                key_idx, active_key = GEMINI_ROTATOR.acquire_key(estimated_tokens=est_tokens)
                if active_key is None:
                    break
                try:
                    genai.configure(api_key=active_key)
                except Exception:
                    pass

                key_error = False
                for m_name in gemini_models:
                    try:
                        model = genai.GenerativeModel(
                            m_name,
                            generation_config={"temperature": temperature, "response_mime_type": "application/json"},
                        )
                        if pdf_bytes and len(pdf_bytes) < 19 * 1024 * 1024:
                            resp = model.generate_content([prompt_text, {"mime_type": "application/pdf", "data": pdf_bytes}])
                        else:
                            resp = model.generate_content(prompt_text)
                        if resp and getattr(resp, "text", None):
                            return resp
                    except Exception as exc:
                        err_str = str(exc).lower()
                        if any(w in err_str for w in ("429", "quota", "rate", "depleted", "exhausted", "invalid", "400", "403")):
                            GEMINI_ROTATOR.mark_key_rate_limited(key_idx, str(exc), 45.0)
                            key_error = True
                            break
                if not key_error:
                    break
            if attempt_round == 0:
                time.sleep(15)

    if not require_pdf_vision and OpenAI is not None and OPENROUTER_KEYS:
        or_models = ["google/gemini-2.5-flash", "google/gemma-3-27b-it:free"]
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
                        temperature=temperature,
                        max_tokens=4096,
                    )
                    txt = completion.choices[0].message.content
                    if txt and "{" in txt:
                        return DummyResponse(txt)
                except Exception as exc:
                    err_s = str(exc).lower()
                    if any(w in err_s for w in ("401", "402", "403", "429", "rate", "quota", "credits", "invalid")):
                        OPENROUTER_ROTATOR.mark_key_rate_limited(or_idx, str(exc), 65.0)
                        break
                    continue
    return None


def parse_json_safely(raw_text):
    if not raw_text:
        return None
    s = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.I).strip()
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def script_stats(text, unicode_range):
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    target_chars = len(re.findall(f"[{unicode_range}]", plain))
    latin_chars = len(re.findall(r"[A-Za-z]", plain))
    total = target_chars + latin_chars
    return target_chars, (target_chars / total if total > 0 else 0.0)


def find_copyright_overlaps(source_text, draft_text, ngram_size=8):
    if not source_text or len(source_text) < 200:
        return []
    src_w = re.findall(r"[a-z0-9]+", source_text.lower())
    drf_w = re.findall(r"[a-z0-9]+", draft_text.lower())
    if len(src_w) < ngram_size or len(drf_w) < ngram_size:
        return []
    src_grams = {" ".join(src_w[i:i + ngram_size]) for i in range(len(src_w) - ngram_size + 1)}
    matches = []
    for i in range(len(drf_w) - ngram_size + 1):
        gram = " ".join(drf_w[i:i + ngram_size])
        if gram in src_grams and gram not in matches:
            if any(legal in gram for legal in ("regulation 30", "sebi listing obligations", "disclosure requirements")):
                continue
            matches.append(gram)
            if len(matches) >= 6:
                break
    return matches


def flatten_lang_pack(pack):
    parts = [str(pack.get("lead", ""))]
    parts.extend(str(x) for x in (pack.get("highlights") or []))
    parts.extend(str(x) for x in (pack.get("hooks") or []))
    for sec in (pack.get("sections") or []):
        parts.append(str(sec.get("heading", "")))
        parts.append(str(sec.get("body", "")))
    for fq in (pack.get("faqs") or []):
        parts.append(str(fq.get("q", "")))
        parts.append(str(fq.get("a", "")))
    return " ".join(parts)


def run_hard_python_gate(bundle, doc_type, source_text):
    """100 પોઈન્ટનો કડક Python ગેટ: એક પણ ભૂલ હશે તો 100/100 નહીં મળે."""
    failures = []
    packs = bundle.get("lang_packs") or {}

    # 1) અંગ્રેજી પેક અને વર્ડ કાઉન્ટ ચેક (AdSense + Google News માટે)
    en_pack = packs.get("en") or {}
    en_flat = flatten_lang_pack(en_pack)
    en_words = len(re.findall(r"[A-Za-z0-9]+", en_flat))
    if en_words < MIN_ENGLISH_WORDS:
        failures.append(f"English article too short ({en_words} words; minimum {MIN_ENGLISH_WORDS} required for Google News & AdSense).")
    if len(en_pack.get("sections") or []) < 3:
        failures.append("English pack must have 3 complete analysis sections.")
    if len(en_pack.get("faqs") or []) < 3:
        failures.append("English pack must have at least 3 FAQs.")

    # 2) ગુજરાતી પેક — એક પણ બુલેટ કે સેક્શન અંગ્રેજીમાં લીક ન થવું જોઈએ
    gu_pack = packs.get("gu") or {}
    gu_lead_c, gu_lead_r = script_stats(gu_pack.get("lead", ""), r"\u0A80-\u0AFF")
    if gu_lead_c < 35 or gu_lead_r < 0.35:
        failures.append(f"Gujarati lead is not in natural Gujarati script (gu_chars={gu_lead_c}, ratio={gu_lead_r:.2f}).")
    for idx, item in enumerate((gu_pack.get("highlights") or [])[:4], 1):
        c, r = script_stats(item, r"\u0A80-\u0AFF")
        if c < 14 or r < 0.25:
            failures.append(f"Gujarati highlight #{idx} leaked into English. Rewrite in natural Gujarati (English digits & stock name only).")
    for idx, item in enumerate((gu_pack.get("hooks") or [])[:4], 1):
        c, r = script_stats(item, r"\u0A80-\u0AFF")
        if c < 14 or r < 0.25:
            failures.append(f"Gujarati takeaway #{idx} leaked into English.")
    gu_all_c, gu_all_r = script_stats(flatten_lang_pack(gu_pack), r"\u0A80-\u0AFF")
    if gu_all_c < 900 or gu_all_r < 0.48:
        failures.append(f"Gujarati full article too short or too much English (gu_chars={gu_all_c}, ratio={gu_all_r:.2f}).")
    if script_stats(bundle.get("gujarati_summary", ""), r"\u0A80-\u0AFF")[0] < 30:
        failures.append("gujarati_summary must be written in natural Gujarati script.")

    # 3) હિન્દી પેક — એક પણ બુલેટ કે સેક્શન અંગ્રેજીમાં લીક ન થવું જોઈએ
    hi_pack = packs.get("hi") or {}
    hi_lead_c, hi_lead_r = script_stats(hi_pack.get("lead", ""), r"\u0900-\u097F")
    if hi_lead_c < 35 or hi_lead_r < 0.35:
        failures.append(f"Hindi lead is not in natural Devanagari Hindi script (hi_chars={hi_lead_c}, ratio={hi_lead_r:.2f}).")
    for idx, item in enumerate((hi_pack.get("highlights") or [])[:4], 1):
        c, r = script_stats(item, r"\u0900-\u097F")
        if c < 14 or r < 0.25:
            failures.append(f"Hindi highlight #{idx} leaked into English.")
    hi_all_c, hi_all_r = script_stats(flatten_lang_pack(hi_pack), r"\u0900-\u097F")
    if hi_all_c < 900 or hi_all_r < 0.48:
        failures.append(f"Hindi full article too short or too much English (hi_chars={hi_all_c}, ratio={hi_all_r:.2f}).")

    # 4) Markdown લીક, રોબોટિક AI શબ્દો અને YMYL પોલિસી ચેક
    all_combined = " ".join([en_flat, flatten_lang_pack(gu_pack), flatten_lang_pack(hi_pack), str(bundle.get("seo_title", ""))])
    if "**" in all_combined or "##" in all_combined:
        failures.append("Raw markdown (** or ##) found in text. Remove all markdown symbols.")
    low_combined = all_combined.lower()
    banned_hits = [p for p in BANNED_AI_AND_POLICY_PHRASES if p in low_combined]
    if banned_hits:
        failures.append(f"Robotic AI or policy-violating phrases found: {banned_hits}. Rewrite naturally like a human financial journalist.")

    # 5) 8-gram કોપીરાઇટ ચેક
    overlaps = find_copyright_overlaps(source_text, en_flat, ngram_size=8)
    if overlaps:
        failures.append(f"Copyright overlap (8+ words matched source): {overlaps[:3]}. Rewrite those sentences completely in fresh words.")

    # 6) હેડલાઈન અને થમ્બનેલ ફિટ ચેક
    seo_t = str(bundle.get("seo_title", "")).strip()
    if len(seo_t) < 42 or len(seo_t) > 90:
        failures.append(f"seo_title length ({len(seo_t)}) should be 45-82 chars.")
    th_hl = str(bundle.get("thumb_headline", "")).strip()
    if not th_hl or len(th_hl.split()) > 8:
        failures.append("thumb_headline must be a crisp 3-7 word English punchline for the thumbnail.")

    # 7) ઓર્ડર/રિઝલ્ટ ડેટા ચેક
    if doc_type == "order":
        ord_d = bundle.get("order_details") or {}
        if not str(ord_d.get("order_value", "")).strip() or not str(ord_d.get("client_or_awarder", "")).strip():
            failures.append("order_details missing client_or_awarder or order_value.")
    else:
        r_c = to_float((bundle.get("revenue") or {}).get("curr"))
        n_c = to_float((bundle.get("net_profit") or {}).get("curr"))
        if r_c == 0 and n_c == 0:
            failures.append("Result figures (Revenue and PAT) cannot both be zero.")

    py_score = 100 if not failures else max(0, 100 - len(failures) * 15)
    return py_score, failures


def step1_draft_organic_english(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len):
    c_clean = screener_info.get("screener_company_name") or re.sub(r"\(\d+\)", "", company_title).split("-")[0].strip()
    ist_today = get_ist_now().strftime("%B %d, %Y")

    prompt = f"""
You are a senior Indian Chartered Accountant and business journalist (writing in the calm, authoritative, natural human voice of Mint or Business Standard) for 'Market Munafa'.
Inspect EVERY PAGE of the attached official BSE PDF ({doc_type.upper()}) for {c_clean} (BSE: {scrip_code}).

STRICT JOURNALISTIC, GOOGLE NEWS & ADSENSE RULES:
1. HUMAN ORGANIC WRITING: Write like an experienced Mumbai financial reporter—direct, specific, natural prose. Zero hype, zero robotic clichés ('delve', 'tapestry', 'in conclusion', 'game-changer', 'ever-evolving', 'rollercoaster', 'it is worth noting'). Zero markdown (** or ##).
2. ZERO FAKE TARGETS OR BUY/SELL TIPS: Never invent future price targets (2026-2030) or give buy/sell calls. Stick strictly to verified BSE PDF facts, Screener ratios, and 52-week positioning.
3. EXACT PDF ARITHMETIC:
   - Extract exact figures directly from the attached BSE PDF table (prefer Consolidated over Standalone).
   - Pick the 3-Month Quarter Ended columns: latest quarter (`curr_period_label`, e.g., "Sep 2026") vs same quarter last year (`prev_period_label`, e.g., "Sep 2025").
   - Convert accurately into ₹ Crores (Lakhs ÷ 100, Millions ÷ 10, Thousands ÷ 10,000).
   - Extract `revenue` (Revenue from Operations), `gross_profit` (EBITDA = PBT before exceptional + Finance Cost + Depreciation), and `net_profit` (PAT).
   - Double-check YoY % math: ((curr - prev) / abs(prev)) * 100.
4. QUALITY FILTER:
   - Set `"skip_stock": false` ONLY IF the PDF shows strong, material performance (positive YoY PAT growth >= 5%, turnaround to profit, revenue growth >= 15%, or a genuinely material new order win). Otherwise set `"skip_stock": true`.
5. ZERO COPYRIGHT OVERLAP: Never copy sentences verbatim from the PDF. Express every detail in fresh original reporting prose.

Company: {c_clean} (BSE: {scrip_code}) | Category: {screener_info['cap_label']}
Verified Screener Data:
- Share Price: {screener_info['price_str']} | 52W High: {screener_info['high_52w']} ({screener_info['dist_from_high']}) | 52W Low: {screener_info['low_52w']} ({screener_info['dist_from_low']})
- Market Cap: {screener_info['market_cap']} | Stock P/E: {screener_info['pe_ratio']} | Industry P/E: {screener_info['industry_pe']} | P/B: {screener_info['pb_ratio']}
- ROE: {screener_info['roe']} | ROCE: {screener_info['roce']} | EPS (TTM): {screener_info['eps']} | Book Value: {screener_info['book_value']}
- Total Debt: {screener_info['total_debt']} | Debt-to-Equity: {screener_info['debt_to_equity']} | Div Yield: {screener_info['div_yield']} | FV: {screener_info['face_value']}

Return ONLY valid JSON:
{{
  "pdf_table_verified": true,
  "pdf_original_unit": "Lakhs or Crores or Millions",
  "skip_stock": false,
  "seo_title": "Keyword-first 55-78 char natural English Google News headline starting with '{c_clean}' with exact key figure",
  "meta_description": "140-152 char crisp English meta summary with exact PDF figure and debt ratio",
  "company_short_name": "Clean short brand name <= 20 chars (e.g., '{c_clean[:20]}')",
  "thumb_headline": "Punchy 3-6 word English headline for thumbnail (e.g., 'PAT JUMPS 42% YoY | REV ₹410 Cr' or 'BAGS ₹185 Cr ORDER WIN')",
  "thumb_badge": "2-word badge e.g. 'Q2 RESULTS' or 'ORDER WIN'",
  "result_date": "{ist_today}",
  "price_display": "{screener_info['price_str']}",
  "quarter_label": "Q2 FY27",
  "curr_period_label": "Sep 2026",
  "prev_period_label": "Sep 2025",
  "result_type": "Consolidated",
  "revenue": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "gross_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "net_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "debt_comparison": {{"curr": "{screener_info['curr_debt_num']}", "prev": "{screener_info['prev_debt_num']}"}},
  "order_details": {{
    "client_or_awarder": "Client Name from PDF",
    "order_value": "Order Value in ₹ Cr from PDF",
    "order_to_mcap_ratio": "Order Value as % of Market Cap",
    "scope_of_work": "Project scope from PDF",
    "execution_timeline": "Execution timeline from PDF"
  }},
  "fundamental_impact_note": "1-2 calm factual sentences on balance-sheet and earnings impact.",
  "market_position_note": "1-2 factual sentences noting trading level relative to 52W High ({screener_info['high_52w']}) and Low ({screener_info['low_52w']}).",
  "en_pack": {{
    "lead": "60-75 word crisp news lead summarizing exact PDF numbers (matching YoY % math), share price ({screener_info['price_str']}), ROE ({screener_info['roe']}), and debt ratio.",
    "highlights": [
      "Exact verified Revenue & PAT YoY figures (in ₹ Cr) or Order Value.",
      "52-Week range ({screener_info['high_52w']} / {screener_info['low_52w']}) vs Current Price ({screener_info['price_str']}).",
      "Balance sheet: Total Debt ({screener_info['total_debt']}) and Debt-to-Equity ({screener_info['debt_to_equity']}).",
      "Valuation: Stock P/E ({screener_info['pe_ratio']}) vs Industry P/E ({screener_info['industry_pe']}) and ROE ({screener_info['roe']})."
    ],
    "hooks": [
      "Crisp takeaway 1 with exact PDF revenue/profit or order figure.",
      "Crisp takeaway 2 on operating margin / EBITDA performance or execution scope.",
      "Crisp takeaway 3 on Debt-to-Equity ({screener_info['debt_to_equity']}) and ROE ({screener_info['roe']}).",
      "Crisp takeaway 4 comparing valuation with sector peers."
    ],
    "sections": [
      {{"heading": "{c_clean} BSE Filing Breakdown: Core Numbers & Drivers", "body": "150-175 words of organic financial reporting explaining exact verified PDF numbers and operational performance."}},
      {{"heading": "Valuation Check, 52-Week Position & Balance Sheet", "body": "150-175 words objectively comparing Stock P/E ({screener_info['pe_ratio']}), Industry P/E ({screener_info['industry_pe']}), Book Value ({screener_info['book_value']}), ROE ({screener_info['roe']}), and Total Debt ({screener_info['total_debt']})."}},
      {{"heading": "What Shareholders Should Track Ahead", "body": "120-150 words on execution milestones, margin stability, and working capital discipline without buy/sell advice."}}
    ],
    "faqs": [
      {{"q": "What did {c_clean} report in its latest BSE filing?", "a": "Direct 2-sentence factual answer with exact verified PDF numbers."}},
      {{"q": "What is the debt-to-equity ratio and total debt of {c_clean}?", "a": "Direct factual answer citing Total Debt ({screener_info['total_debt']}) and Debt-to-Equity ({screener_info['debt_to_equity']})."}},
      {{"q": "How is {c_clean} valued compared to its industry peers?", "a": "Direct factual answer comparing Stock P/E ({screener_info['pe_ratio']}), Industry P/E ({screener_info['industry_pe']}), and ROE ({screener_info['roe']})."}}
    ]
  }}
}}

Supplementary Extracted Filing Text:
{source_text[:MAX_TEXT_CHARS]}
"""
    require_vision = (pdf_bytes is not None and pdf_text_len < 400)
    resp = call_ai_api(prompt, pdf_bytes=pdf_bytes, require_pdf_vision=require_vision, temperature=0.25)
    parsed = parse_json_safely(getattr(resp, "text", ""))
    if not parsed:
        return None
    if doc_type == "result":
        parsed = verify_and_normalize_pdf_result_numbers(parsed, screener_info)
    return parsed


def step2_write_native_indic_pack(en_pack, comp_name, lang_code, lang_name):
    """Google Translate વગર સીધું અખબારી ગુજરાતી કે હિન્દીમાં લખે છે (અંક 0-9 અને શેર નામ અંગ્રેજીમાં જ)."""
    style = (
        "Divya Bhaskar Business / Gujarat Samachar Business (natural everyday Gujarati used by Gujarati investors today, never textbook or translated-sounding)"
        if lang_code == "gu"
        else "Mint Hindi / Economic Times Hindi (natural contemporary Hindi used by Indian investors today, never robotic translations)"
    )
    extra_keys = (
        ',\n  "gujarati_summary": "3 natural Gujarati sentences for Telegram alert summarizing exact PDF figures",\n  "gujarati_balance_view": "2 natural Gujarati sentences on Debt-to-Equity, P/E, and ROE factually"'
        if lang_code == "gu"
        else ""
    )

    prompt = f"""
You are a veteran native {lang_name} stock market journalist writing in the crisp, human newspaper style of {style}.
Rewrite the verified English equity report below into 100% authentic, natural, fluent {lang_name}.

NON-NEGOTIABLE RULES:
1. WRITE EVERY SINGLE SENTENCE IN {lang_name} SCRIPT: `lead`, EVERY item in `highlights` and `hooks`, EVERY section `heading` & `body`, and EVERY FAQ `q` & `a` MUST be in natural {lang_name} script — NEVER leave sentences in English!
2. KEEP ENGLISH DIGITS (0-9) & COMPANY/STOCK NAMES ONLY:
   - Keep ALL numbers in English Arabic digits (`0-9`, e.g., `₹450 Cr`, `+24.5%`, `Q2 FY27`, `52W`).
   - Keep company/stock names and standard market acronyms in English (`{comp_name}`, `BSE`, `NSE`, `SEBI`, `PAT`, `EBITDA`, `ROE`, `P/E`, `EPS`, `YoY`).
3. KEEP 100% OF FACTS & LENGTH: Do not shorten or skip any paragraph or number. Zero markdown (`**`, `##`).

VERIFIED ENGLISH REPORT:
{json.dumps(en_pack, ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "pack": {{
    "lead": "Natural {lang_name} lead paragraph",
    "highlights": ["Highlight 1", "Highlight 2", "Highlight 3", "Highlight 4"],
    "hooks": ["Takeaway 1", "Takeaway 2", "Takeaway 3", "Takeaway 4"],
    "sections": [
      {{"heading": "Heading 1", "body": "Full natural {lang_name} paragraph"}},
      {{"heading": "Heading 2", "body": "Full natural {lang_name} paragraph"}},
      {{"heading": "Heading 3", "body": "Full natural {lang_name} paragraph"}}
    ],
    "faqs": [
      {{"q": "Question 1?", "a": "Answer 1"}},
      {{"q": "Question 2?", "a": "Answer 2"}},
      {{"q": "Question 3?", "a": "Answer 3"}}
    ]
  }}{extra_keys}
}}
"""
    resp = call_ai_api(prompt, pdf_bytes=None, require_pdf_vision=False, temperature=0.3)
    parsed = parse_json_safely(getattr(resp, "text", ""))
    return enforce_english_digits(parsed) if parsed else None


def step3_chief_editor_audit_and_fix(bundle, doc_type, source_text, screener_info, py_failures):
    """ચીફ એડિટર & CA ઓડિટ: Python એ પકડેલી બધી ભૂલો + ફેક્ટ/વ્યાકરણ/ઓર્ગેનિક ટોન સુધારી 100/100 એડિટર સ્કોર ચેક કરે છે."""
    alerts = (
        "CRITICAL HARD ERRORS FROM PYTHON GATE (MUST FIX 100% OF THESE):\n- " + "\n- ".join(py_failures)
        if py_failures else "Automated Python Gate passed 100/100! Perform final strict factual, arithmetic, grammar, and human-readability audit."
    )

    prompt = f"""
You are the Ruthless Chief Editor, Chartered Accountant, and Google News SEO Auditor for 'Market Munafa'.
Deeply inspect this complete trilingual financial report JSON against the Official BSE Filing Text and Verified Screener Ratios, and FIX EVERY SINGLE ISSUE.

{alerts}

MANDATORY 100-POINT AUDIT CHECKLIST:
1. FACT & ARITHMETIC ACCURACY: Verify every ₹ Cr figure, YoY % growth, Order Value, Client Name, Share Price ({screener_info['price_str']}), P/E ({screener_info['pe_ratio']}), ROE ({screener_info['roe']}), and Debt-to-Equity ({screener_info['debt_to_equity']}). Ensure zero contradiction between the numbers and text in EN, GU, and HI.
2. NATURAL HUMAN JOURNALISM (ZERO BOT FEEL): Remove any stiff phrasing, AI clichés, or translated-sounding Gujarati/Hindi. Every language must read like an authentic human newspaper report.
3. STRICT SCRIPT PURITY:
   - `lang_packs.gu` + `gujarati_summary` + `gujarati_balance_view` MUST be 100% Gujarati script (only company/share names & 0-9 digits in English).
   - `lang_packs.hi` MUST be 100% Devanagari Hindi script (only company/share names & 0-9 digits in English).
   - `lang_packs.en` MUST be crisp Indian English (>= 460 words total).
4. ZERO COPYRIGHT & ADSENSE SAFETY: Zero 8-word verbatim copies from PDF, zero buy/sell advice, zero fake future price targets, zero markdown (`**` or `##`).
5. HONEST SCORING: Give `"editor_score": 100` ONLY IF every single criterion is genuinely 100% flawless AFTER your fixes. Otherwise give the honest lower score and list remaining `"issues_found"`.

OFFICIAL BSE FILING EXCERPT:
{source_text[:6500]}

CURRENT REPORT BUNDLE TO AUDIT & FIX:
{json.dumps(bundle, ensure_ascii=False)}

Return ONLY valid JSON containing ALL original keys of the bundle PLUS `"editor_score"` (int 0-100) and `"issues_found"` (list of strings).
"""
    resp = call_ai_api(prompt, pdf_bytes=None, require_pdf_vision=False, temperature=0.1)
    parsed = parse_json_safely(getattr(resp, "text", ""))
    return enforce_english_digits(parsed) if parsed else None


def generate_and_verify_200_of_200(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len):
    """સીધી પોસ્ટ બંધ — 200/200 (100 Python + 100 Chief Editor) પાસ થાય તો જ રિટર્ન કરશે, નહીંતર None."""
    log.info("Step 1/3: Reading official BSE PDF & drafting organic English report for %s...", company_title)
    en_base = step1_draft_organic_english(company_title, scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len)
    if not en_base or en_base.get("pdf_table_verified") is False or en_base.get("skip_stock") is True:
        log.info("Skipping stock (weak result / unverified PDF table): %s", company_title)
        return None

    if doc_type == "result":
        r_curr = to_float((en_base.get("revenue") or {}).get("curr"))
        np_curr = to_float((en_base.get("net_profit") or {}).get("curr"))
        curr_p = str(en_base.get("curr_period_label", "")).lower()
        if (r_curr == 0 and np_curr == 0) or any(old_q in curr_p for old_q in ("jun 2026", "mar 2026", "dec 2025")):
            log.warning("Skipping result due to old period (%s) or zero figures: %s", curr_p, company_title)
            return None

    comp_name = str(en_base.get("company_short_name") or screener_info.get("screener_company_name") or company_title).strip()
    en_pack = en_base.pop("en_pack", {})
    time.sleep(2)

    log.info("Step 2/3: Writing native Gujarati editorial report for %s...", comp_name)
    gu_res = step2_write_native_indic_pack(en_pack, comp_name, "gu", "Gujarati")
    if not gu_res or not gu_res.get("pack"):
        log.warning("Native Gujarati generation failed for %s. Rejecting post.", comp_name)
        return None
    time.sleep(2)

    log.info("Step 3/3: Writing native Hindi editorial report for %s...", comp_name)
    hi_res = step2_write_native_indic_pack(en_pack, comp_name, "hi", "Hindi")
    if not hi_res or not hi_res.get("pack"):
        log.warning("Native Hindi generation failed for %s. Rejecting post.", comp_name)
        return None
    time.sleep(2)

    bundle = dict(en_base)
    bundle["lang_packs"] = {"gu": gu_res["pack"], "hi": hi_res["pack"], "en": en_pack}
    bundle["gujarati_summary"] = gu_res.get("gujarati_summary", "")
    bundle["gujarati_balance_view"] = gu_res.get("gujarati_balance_view", "")
    bundle = enforce_english_digits(bundle)

    # કડક 200/200 ઓડિટ અને સુધારા લૂપ (ભૂલ ન સુધરે ત્યાં સુધી પોસ્ટ નહીં થાય)
    for rnd in range(1, MAX_AUDIT_FIX_ROUNDS + 1):
        pre_py_score, pre_py_failures = run_hard_python_gate(bundle, doc_type, source_text)
        log.info("Audit Round %d/%d | Pre-Fix Python Gate: %d/100 | Failures: %d", rnd, MAX_AUDIT_FIX_ROUNDS, pre_py_score, len(pre_py_failures))

        audited = step3_chief_editor_audit_and_fix(bundle, doc_type, source_text, screener_info, pre_py_failures)
        if not audited:
            log.warning("Audit Round %d returned invalid JSON; retrying...", rnd)
            time.sleep(2)
            continue

        editor_score = int(audited.get("editor_score", 0))
        issues = audited.get("issues_found") or []

        # ઓડિટ પછી ફરીથી Python હાર્ડ ચેક (AI ખોટો સ્કોર ન આપી શકે)
        post_py_score, post_py_failures = run_hard_python_gate(audited, doc_type, source_text)
        total_score = post_py_score + editor_score
        log.info("Round %d Score -> Python: %d/100 + Editor: %d/100 = %d/200", rnd, post_py_score, editor_score, total_score)
        if post_py_failures:
            log.info("   Remaining Python Gate Errors: %s", post_py_failures[:3])
        if issues:
            log.info("   Editor Notes: %s", issues[:3])

        if total_score >= REQUIRED_TOTAL_SCORE and post_py_score == 100 and editor_score == 100 and not post_py_failures:
            log.info("✅ PASSED 200/200 ZERO-ERROR ORGANIC QUALITY GATE for %s!", comp_name)
            if doc_type == "result":
                audited = verify_and_normalize_pdf_result_numbers(audited, screener_info)
            return audited

        bundle = {k: v for k, v in audited.items() if k not in ("editor_score", "issues_found")}
        time.sleep(3)

    log.warning("❌ REJECTED '%s': Did not achieve strict 200/200 score after %d fix rounds.", comp_name, MAX_AUDIT_FIX_ROUNDS)
    return None


# ==========================================
# 8. CHARTS & CLEAN NEWSPAPER-STYLE HTML BUILDER
# ==========================================
def build_quarterly_column_chart_url(comp_name, labels, rev_list, gp_list, np_list):
    datasets = [
        {"label": "Revenue", "backgroundColor": "#1b5e20", "data": [to_float(x) for x in rev_list]},
        {"label": "Net Profit", "backgroundColor": "#2563eb", "data": [to_float(x) for x in np_list]},
    ]
    if gp_list and any(to_float(x) != 0 for x in gp_list):
        datasets.insert(1, {"label": "EBITDA", "backgroundColor": "#90a4ae", "data": [to_float(x) for x in gp_list]})

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


def build_yoy_comparison_chart_url(comp_name, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp):
    labels = ["Revenue", "EBITDA", "Net Profit", "Total Debt"]
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
            "title": {"display": True, "text": f"{comp_name} - {curr_lbl} vs {prev_lbl} YoY (Rs.Cr.)", "fontSize": 16, "fontColor": "#475569", "padding": 14},
            "legend": {"display": True, "position": "top", "labels": {"fontSize": 13, "fontColor": "#475569"}},
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True, "padding": 6}}], "xAxes": [{"ticks": {"fontSize": 13, "fontColor": "#475569"}}]},
            "plugins": {"datalabels": {"display": True, "anchor": "end", "align": "top", "offset": 2, "clip": False, "color": "#000000", "font": {"weight": "bold", "size": 12}}},
        },
    }
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={urllib.parse.quote(json.dumps(chart_config))}"


def _render_top_intro_panel_html(lang_code, is_default, summary_label, highlight_label, badge_title, lead_txt, high_list, hooks_list):
    disp = "block" if is_default else "none"
    hid = "" if is_default else ' hidden="hidden"'
    high_li = "".join(f"<li style='margin-bottom:8px; color:#1e293b; line-height:1.7;'>{html.escape(str(k))}</li>" for k in high_list if k)
    hook_li = "".join(f"<li style='margin-bottom:8px; color:#1e293b; line-height:1.7;'>{html.escape(str(h))}</li>" for h in hooks_list if h)

    return f"""
    <div class="mm-lang-pane" data-lang="{lang_code}" lang="{lang_code}" style="display:{disp};"{hid}>
      <p style="font-size: 16px; line-height: 1.85; color: #0f172a; background: #f8fafc; padding: 15px 18px; border-radius: 8px; border: 1px solid #e2e8f0; border-left: 4px solid #1d4ed8; margin-bottom: 18px;">
        <strong>{html.escape(summary_label)}:</strong> {html.escape(str(lead_txt))}
      </p>
      <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid #0f766e; border-radius: 8px; padding: 15px 18px; margin-bottom: 18px;">
        <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">{html.escape(highlight_label)}</h2>
        <ul style="margin: 0; padding-left: 20px; font-size: 15px;">{high_li}</ul>
      </div>
      <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 15px 18px; margin-bottom: 20px;">
        <div style="margin: 0 0 10px 0; font-size: 16.5px; font-weight: 800; color: #0f172a; border-bottom: 1px solid #cbd5e1; padding-bottom: 6px;">{html.escape(badge_title)}</div>
        <ul style="margin: 0; padding-left: 20px; font-size: 14.5px;">{hook_li}</ul>
      </div>
    </div>
    """


def _render_bottom_analysis_panel_html(lang_code, is_default, faq_label, sections_list, faqs_list):
    disp = "block" if is_default else "none"
    hid = "" if is_default else ' hidden="hidden"'
    sec_html = ""
    for s in sections_list:
        if s.get("body"):
            sec_html += (
                f"<h2 style='font-size:19px; color:#0f172a; margin:24px 0 10px 0; border-left:4px solid #1d4ed8; padding-left:10px; line-height:1.4;'>{html.escape(str(s.get('heading', '')))}</h2>"
                f"<p style='color:#1e293b; font-size:15.5px; line-height:1.85; margin-bottom:14px;'>{html.escape(str(s.get('body', '')))}</p>"
            )

    faq_html = ""
    for f_item in faqs_list:
        if f_item.get("q") and f_item.get("a"):
            faq_html += (
                f"<div style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:13px 15px; margin-bottom:11px;'>"
                f"<h3 style='margin:0 0 6px 0; font-size:15.5px; font-weight:700; color:#0f172a;'>Q: {html.escape(str(f_item['q']))}</h3>"
                f"<p style='margin:0; color:#334155; font-size:14.5px; line-height:1.7;'>{html.escape(str(f_item['a']))}</p>"
                f"</div>"
            )

    return f"""
    <div class="mm-lang-pane" data-lang="{lang_code}" lang="{lang_code}" style="display:{disp};"{hid}>
      {sec_html}
      <div style="margin: 24px 0;">
        <h2 style="font-size: 19px; color: #0f172a; margin-bottom: 12px; border-left:4px solid #0f766e; padding-left:10px;">{html.escape(faq_label)}</h2>
        {faq_html}
      </div>
    </div>
    """


def build_post_html(data, doc_type, screener_info, source_url, thumb_url, related_posts, comp_key="", scrip_key="", file_key="", scrip_code=""):
    comp_name_raw = html.unescape(str(data.get("company_short_name", "Company"))).strip()
    comp_name = html.escape(comp_name_raw)
    res_date = html.escape(str(data.get("result_date", get_ist_now().strftime("%B %d, %Y"))))
    ist_now = get_ist_now()
    ist_time_str = ist_now.strftime("%B %d, %Y, %I:%M %p IST")
    iso_time = ist_now.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Quarterly")))
    curr_lbl = html.escape(shorten_period_label(data.get("curr_period_label", "Sep 2026")))
    prev_lbl = html.escape(shorten_period_label(data.get("prev_period_label", "Sep 2025")))
    res_type = html.escape(str(data.get("result_type", "Consolidated")))

    mkt_cap = html.escape(str(screener_info["market_cap"]))
    cap_label = html.escape(str(screener_info.get("cap_label", "Listed")))
    pe_ratio = html.escape(str(screener_info["pe_ratio"]))
    pb_ratio = html.escape(str(screener_info["pb_ratio"]))
    ind_pe = html.escape(str(screener_info["industry_pe"]))
    roe = html.escape(str(screener_info["roe"]))
    eps = html.escape(str(screener_info["eps"]))
    div_yield = html.escape(str(screener_info["div_yield"]))
    book_value = html.escape(str(screener_info["book_value"]))
    total_debt = html.escape(str(screener_info["total_debt"]))
    de_ratio = html.escape(str(screener_info["debt_to_equity"]))
    face_val = html.escape(str(screener_info["face_value"]))
    high_52w = html.escape(str(screener_info.get("high_52w", "N/A")))
    low_52w = html.escape(str(screener_info.get("low_52w", "N/A")))
    dist_high = html.escape(str(screener_info.get("dist_from_high", "N/A")))
    dist_low = html.escape(str(screener_info.get("dist_from_low", "N/A")))
    fund_impact = html.escape(str(data.get("fundamental_impact_note", "")))
    market_pos = html.escape(str(data.get("market_position_note", "")))

    uid = hashlib.md5((comp_name_raw + res_date + scrip_code).encode("utf-8")).hexdigest()[:8]
    universal_data_blocks = []

    if doc_type == "order":
        ord_info = data.get("order_details", {}) or {}
        order_card = f"""
        <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 18px; margin-bottom: 24px;">
          <h2 style="margin: 0 0 12px 0; font-size: 18.5px; color: #0f172a; border-left:4px solid #0f766e; padding-left:10px;">{comp_name} Official Order &amp; Contract Details</h2>
          <table style="width:100%; border-collapse:collapse; font-size:14px;">
            <tbody>
              <tr style="border-bottom:1px solid #e2e8f0;"><td style="padding:9px 8px; color:#475569; font-weight:600; width:40%;">Company</td><td style="padding:9px 8px; color:#0f172a; font-weight:700;">{comp_name} ({price_disp})</td></tr>
              <tr style="border-bottom:1px solid #e2e8f0; background:#f8fafc;"><td style="padding:9px 8px; color:#475569; font-weight:600;">Client / Awarding Entity</td><td style="padding:9px 8px; color:#0f172a;">{html.escape(str(ord_info.get("client_or_awarder", "Disclosed in Filing")))}</td></tr>
              <tr style="border-bottom:1px solid #e2e8f0;"><td style="padding:9px 8px; color:#475569; font-weight:600;">Order Value</td><td style="padding:9px 8px; color:#0f766e; font-weight:700;">{html.escape(str(ord_info.get("order_value", "As per BSE Filing")))}</td></tr>
              <tr style="border-bottom:1px solid #e2e8f0; background:#f8fafc;"><td style="padding:9px 8px; color:#475569; font-weight:600;">Order Size vs Market Cap</td><td style="padding:9px 8px; color:#0f172a;">{html.escape(str(ord_info.get("order_to_mcap_ratio", "Disclosed in Filing")))}</td></tr>
              <tr style="border-bottom:1px solid #e2e8f0;"><td style="padding:9px 8px; color:#475569; font-weight:600;">Scope of Work</td><td style="padding:9px 8px; color:#0f172a;">{html.escape(str(ord_info.get("scope_of_work", "Commercial execution contract")))}</td></tr>
              <tr style="background:#f8fafc;"><td style="padding:9px 8px; color:#475569; font-weight:600;">Execution Timeline</td><td style="padding:9px 8px; color:#0f172a;">{html.escape(str(ord_info.get("execution_timeline", "As per contract terms")))}</td></tr>
            </tbody>
          </table>
        </div>
        """
        universal_data_blocks.append(order_card)
        yoy_chart_url = ""
    else:
        rev = data.get("revenue", {"curr": "0", "prev": "0", "growth": "N/A", "difference": "0 Cr"})
        gp = data.get("gross_profit", {"curr": "0", "prev": "0", "growth": "N/A", "difference": "0 Cr"})
        np_d = data.get("net_profit", {"curr": "0", "prev": "0", "growth": "N/A", "difference": "0 Cr"})
        debt_comp = data.get("debt_comparison") or {"curr": screener_info["curr_debt_num"], "prev": screener_info["prev_debt_num"]}

        q_labels = data.get("quarter_chart_labels") or [prev_lbl, curr_lbl]
        q_rev = data.get("quarter_chart_revenue") or [to_float(rev.get("prev")), to_float(rev.get("curr"))]
        q_gp = data.get("quarter_chart_gross_profit") or [to_float(gp.get("prev")), to_float(gp.get("curr"))]
        q_np = data.get("quarter_chart_net_profit") or [to_float(np_d.get("prev")), to_float(np_d.get("curr"))]

        quarter_chart_url = build_quarterly_column_chart_url(comp_name_raw, q_labels, q_rev, q_gp, q_np)
        yoy_chart_url = build_yoy_comparison_chart_url(comp_name_raw, prev_lbl, curr_lbl, rev, gp, np_d, debt_comp)

        def growth_color(growth_str):
            return "#b91c1c" if "-" in str(growth_str) else "#0f766e"

        result_card = f"""
        <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 16px; margin-bottom: 24px; box-sizing: border-box;">
          <h2 style="margin: 0 0 8px 0; font-size: 18px; color: #0f172a; border-left:4px solid #0f766e; padding-left:10px;">{comp_name} {qtr_lbl} Official BSE Results ({res_type})</h2>
          <div style="display: flex; justify-content: space-between; align-items: center; color: #64748b; font-size: 12.5px; margin-bottom: 10px;">
            <span>{res_date} | Price: <strong style="color:#0f172a;">{price_disp}</strong></span>
            <span>Figures in ₹ Crore</span>
          </div>
          <div style="width: 100%; overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: right;">
              <thead>
                <tr style="border-top: 1px solid #cbd5e1; border-bottom: 1px solid #cbd5e1; color: #0f172a; font-weight: 700; background: #f8fafc;">
                  <th style="text-align: left; padding: 9px 6px; white-space: nowrap;">Metric</th>
                  <th style="padding: 9px 6px; white-space: nowrap;">{curr_lbl}</th>
                  <th style="padding: 9px 6px; white-space: nowrap;">{prev_lbl}</th>
                  <th style="padding: 9px 6px; white-space: nowrap;">Change</th>
                  <th style="padding: 9px 6px; white-space: nowrap;">YoY %</th>
                </tr>
              </thead>
              <tbody>
                <tr style="border-bottom: 1px solid #e2e8f0;">
                  <td style="text-align: left; padding: 10px 6px; color: #1e293b; font-weight: 600; white-space: nowrap;">Revenue</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">₹{html.escape(str(rev.get('curr', '0')))}</td>
                  <td style="padding: 10px 6px; color: #64748b; white-space: nowrap;">₹{html.escape(str(rev.get('prev', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">{html.escape(str(rev.get('difference', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 700; white-space: nowrap; color: {growth_color(rev.get('growth', ''))};">{html.escape(str(rev.get('growth', 'N/A')))}</td>
                </tr>
                <tr style="border-bottom: 1px solid #e2e8f0; background:#f8fafc;">
                  <td style="text-align: left; padding: 10px 6px; color: #1e293b; font-weight: 600; white-space: nowrap;">EBITDA</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">₹{html.escape(str(gp.get('curr', '0')))}</td>
                  <td style="padding: 10px 6px; color: #64748b; white-space: nowrap;">₹{html.escape(str(gp.get('prev', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">{html.escape(str(gp.get('difference', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 700; white-space: nowrap; color: {growth_color(gp.get('growth', ''))};">{html.escape(str(gp.get('growth', 'N/A')))}</td>
                </tr>
                <tr>
                  <td style="text-align: left; padding: 10px 6px; color: #1e293b; font-weight: 600; white-space: nowrap;">Net Profit (PAT)</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">₹{html.escape(str(np_d.get('curr', '0')))}</td>
                  <td style="padding: 10px 6px; color: #64748b; white-space: nowrap;">₹{html.escape(str(np_d.get('prev', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 600; white-space: nowrap;">{html.escape(str(np_d.get('difference', '0')))}</td>
                  <td style="padding: 10px 6px; font-weight: 700; white-space: nowrap; color: {growth_color(np_d.get('growth', ''))};">{html.escape(str(np_d.get('growth', 'N/A')))}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; margin-bottom: 22px; text-align: center;">
          <h3 style="margin: 0 0 12px 0; font-size: 16.5px; color: #0f172a;">{comp_name} YoY Revenue, PAT &amp; Debt Comparison</h3>
          <img src="{yoy_chart_url}" alt="{comp_name} Quarterly Results YoY Chart" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; margin-bottom: 22px; text-align: center;">
          <h3 style="margin: 0 0 12px 0; font-size: 16.5px; color: #0f172a;">{comp_name} Multi-Quarter Earnings Trend</h3>
          <img src="{quarter_chart_url}" alt="{comp_name} Multi-Quarter Profit Trend" width="750" height="420" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px;" />
        </div>
        """
        universal_data_blocks.append(result_card)

    fundamentals_card = f"""
    <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 16px; margin-bottom: 24px; box-sizing: border-box;">
      <h2 style="margin: 0 0 12px 0; font-size: 18px; color: #0f172a; border-left:4px solid #1d4ed8; padding-left:10px;">{comp_name} Valuation Ratios &amp; 52-Week Range</h2>
      <table style="width: 100%; border-collapse: collapse; font-size: 13.5px;">
        <tbody>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 6px; color: #64748b;">Share Price</td><td style="padding: 8px 6px; text-align: right; font-weight: 700; color: #0f172a;">{price_disp}</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">52W High / Low</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{high_52w} / {low_52w}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0; background:#f8fafc;">
            <td style="padding: 8px 6px; color: #64748b;">52W Position</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{dist_high}</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">From 52W Low</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f766e;">{dist_low}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 6px; color: #64748b;">Market Cap</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{mkt_cap} ({cap_label})</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">ROE</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{roe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0; background:#f8fafc;">
            <td style="padding: 8px 6px; color: #64748b;">Stock P/E</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{pe_ratio}</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">Industry P/E</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{ind_pe}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 6px; color: #64748b;">EPS (TTM)</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{eps}</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">Book Value</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{book_value} ({pb_ratio}x)</td>
          </tr>
          <tr style="background:#f8fafc;">
            <td style="padding: 8px 6px; color: #64748b;">Debt / Equity</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{de_ratio} ({total_debt})</td>
            <td style="padding: 8px 6px; color: #64748b; border-left: 1px solid #e2e8f0;">Div Yield / FV</td><td style="padding: 8px 6px; text-align: right; font-weight: 600; color: #0f172a;">{div_yield} (₹{face_val})</td>
          </tr>
        </tbody>
      </table>
      <p style="margin: 11px 0 0 0; font-size: 14px; color: #334155; line-height:1.65;"><strong>Fundamental Overview:</strong> {market_pos} {fund_impact}</p>
    </div>
    """
    universal_data_blocks.append(fundamentals_card)

    lang_packs = data.get("lang_packs") or {}
    gu_p = lang_packs.get("gu") or {}
    hi_p = lang_packs.get("hi") or {}
    en_p = lang_packs.get("en") or {}

    top_gu = _render_top_intro_panel_html("gu", True, "મુખ્ય સારાંશ", "રોકાણકારો માટે મહત્વના મુદ્દા", f"{comp_name_raw} એક નજરમાં", gu_p.get("lead", ""), gu_p.get("highlights", []), gu_p.get("hooks", []))
    top_hi = _render_top_intro_panel_html("hi", False, "मुख्य सारांश", "निवेशकों के लिए प्रमुख बिंदु", f"{comp_name_raw} एक नज़र में", hi_p.get("lead", ""), hi_p.get("highlights", []), hi_p.get("hooks", []))
    top_en = _render_top_intro_panel_html("en", False, "Executive Summary", "Key Financial Highlights", f"{comp_name_raw} at a Glance", en_p.get("lead", ""), en_p.get("highlights", []), en_p.get("hooks", []))

    bot_gu = _render_bottom_analysis_panel_html("gu", True, f"{comp_name_raw} શેર વિશે વારંવાર પૂછાતા પ્રશ્નો (FAQs)", gu_p.get("sections", []), gu_p.get("faqs", []))
    bot_hi = _render_bottom_analysis_panel_html("hi", False, f"{comp_name_raw} शेयर से जुड़े मुख्य प्रश्न (FAQs)", hi_p.get("sections", []), hi_p.get("faqs", []))
    bot_en = _render_bottom_analysis_panel_html("en", False, f"Frequently Asked Questions ({comp_name_raw})", en_p.get("sections", []), en_p.get("faqs", []))

    related_li = "".join(
        f"<li style='margin-bottom:7px;'><a href='{html.escape(rp['url'], quote=True)}' style='color:#1d4ed8; font-weight:600; text-decoration:none;'>{html.escape(rp['title'])}</a></li>"
        for rp in (related_posts or [])[:3]
    )
    internal_links_html = f"""
    <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 15px 18px; margin: 22px 0; font-size: 14.5px; color: #0f172a;">
      <strong>Also Read on Market Munafa</strong>
      <ul style="margin: 8px 0 0 0; padding-left: 20px; line-height: 1.75;">
        {related_li}
        <li style="margin-bottom:6px;"><a href="{BLOG_BASE_URL}/search/label/STOCKS%20RESULT" style="color:#1d4ed8; font-weight:600; text-decoration:none;">Latest Quarterly Stocks Results &amp; Analysis</a></li>
        <li><a href="{BLOG_BASE_URL}/search/label/DEBT%20FREE%2010%20STOCKS" style="color:#1d4ed8; font-weight:600; text-decoration:none;">Debt-Free &amp; Low-Debt Stocks in India</a></li>
      </ul>
    </div>
    """

    source_link_html = (
        f'<p style="margin-top:14px; font-size:14px;"><strong>Official Reference:</strong> <a href="{html.escape(source_url, quote=True)}" target="_blank" rel="nofollow noopener" style="color:#1d4ed8; font-weight:600; text-decoration:none;">View Official BSE Exchange Filing PDF</a></p>'
        if source_url else ""
    )

    meta_desc_clean = html.unescape(str(data.get("meta_description") or en_p.get("lead") or "")).strip()[:155]
    headline_clean = html.unescape(str(data.get("seo_title") or f"{comp_name_raw} Quarterly Results & Fundamental Analysis")).strip()[:110]

    faq_schema_entities = [
        {
            "@type": "Question",
            "name": html.unescape(str(f_item["q"])).strip(),
            "acceptedAnswer": {"@type": "Answer", "text": html.unescape(str(f_item["a"])).strip()},
        }
        for f_item in (en_p.get("faqs") or [])
        if isinstance(f_item, dict) and f_item.get("q") and f_item.get("a")
    ]

    final_img = thumb_url or yoy_chart_url or SITE_LOGO_URL
    schema_bundle = [
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": headline_clean,
            "description": meta_desc_clean,
            "articleSection": "Indian Stock Market",
            "datePublished": iso_time,
            "dateModified": iso_time,
            "image": [final_img],
            "author": {"@type": "Organization", "name": "Market Munafa Editorial Desk", "url": BLOG_BASE_URL},
            "publisher": {
                "@type": "Organization",
                "name": "Market Munafa",
                "url": BLOG_BASE_URL,
                "logo": {"@type": "ImageObject", "url": SITE_LOGO_URL},
            },
        }
    ]
    if faq_schema_entities:
        schema_bundle.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq_schema_entities})

    thumb_block = ""
    if thumb_url:
        thumb_block = f"""
        <figure style="margin: 0 0 18px 0; text-align: center;">
          <img src="{thumb_url}" alt="{headline_clean}" title="{headline_clean}" width="1280" height="720" fetchpriority="high" style="width: 100%; height: auto; border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,0.12);" />
          <figcaption style="font-size: 12.5px; color: #64748b; margin-top: 6px;">{comp_name} (BSE: {html.escape(str(scrip_code or 'Listed'))}) | {cap_label} | Market Munafa</figcaption>
        </figure>
        """

    meta_comment = f"<!-- MM_META:type={doc_type}|comp_key={comp_key}|scrip={scrip_key}|file={file_key}|comp_name={comp_name} -->"

    html_body = f"""
    {meta_comment}
    <article id="mm-post-{uid}" style="font-family: 'Noto Sans Gujarati', 'Noto Sans Devanagari', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 800px; margin: 0 auto; color: #1e293b; box-sizing: border-box;">
      <style>
        #mm-post-{uid} .mm-langbar {{
          display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: center;
          background: #f8fafc; padding: 10px 12px; border-radius: 8px;
          border: 1px solid #e2e8f0; margin-bottom: 18px;
        }}
        #mm-post-{uid} .mm-lbtn {{
          cursor: pointer; padding: 8px 20px; border-radius: 6px; font-size: 14px; font-weight: 700;
          background: #ffffff; color: #0f172a; border: 1px solid #cbd5e1; transition: 0.2s;
        }}
        #mm-post-{uid} .mm-lbtn.active, #mm-post-{uid} .mm-lbtn:hover {{
          background: #0f172a; color: #ffffff; border-color: #0f172a;
        }}
      </style>

      {thumb_block}

      <div style="font-size: 13px; color: #64748b; text-align: center; margin-bottom: 14px;">
        Published: <time datetime="{iso_time}">{ist_time_str}</time> &nbsp;|&nbsp; Market Munafa Equity Research Desk
      </div>

      <div class="mm-langbar">
        <button type="button" class="mm-lbtn active" onclick="mmSwitchBseLang('{uid}','gu',this)">ગુજરાતી</button>
        <button type="button" class="mm-lbtn" onclick="mmSwitchBseLang('{uid}','hi',this)">हिन्दी</button>
        <button type="button" class="mm-lbtn" onclick="mmSwitchBseLang('{uid}','en',this)">English</button>
      </div>

      {top_gu}
      {top_hi}
      {top_en}

      <!--more-->

      {"".join(universal_data_blocks)}

      {bot_gu}
      {bot_hi}
      {bot_en}

      {internal_links_html}
      {source_link_html}

      <script type="application/ld+json">{json.dumps(schema_bundle, ensure_ascii=False)}</script>
      <script>
        function mmSwitchBseLang(uid, code, btn) {{
          var root = document.getElementById("mm-post-" + uid);
          if (!root) return;
          root.querySelectorAll(".mm-lang-pane").forEach(function(sec) {{
            var match = sec.getAttribute("data-lang") === code;
            sec.style.display = match ? "block" : "none";
            sec.hidden = !match;
          }});
          root.querySelectorAll(".mm-lbtn").forEach(function(b) {{ b.classList.remove("active"); }});
          if (btn) btn.classList.add("active");
        }}
      </script>

      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 22px 0;" />
      <p style="font-size: 12.5px; color: #64748b; line-height: 1.6;">
        <strong>Disclaimer:</strong> This independent equity report on Market Munafa is prepared strictly from publicly disclosed BSE filings and financial ratios for informational and educational purposes only. It is not investment advice. Please consult a SEBI-registered financial advisor before making any investment decision.
      </p>
    </article>
    """
    return html_body, yoy_chart_url, meta_desc_clean


# ==========================================
# 9. TELEGRAM & SAFE BLOGGER PUBLISHER
# ==========================================
def send_telegram_gujarati(data, doc_type, screener_info, blog_url, source_url, thumb_url, chart_url):
    if not SEND_TO_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    comp_name = html.escape(str(data.get("company_short_name", "Company")))
    res_date = html.escape(str(data.get("result_date", "")))
    price_disp = html.escape(str(data.get("price_display", screener_info["price_str"])))
    qtr_lbl = html.escape(str(data.get("quarter_label", "Quarterly")))
    curr_lbl = html.escape(shorten_period_label(data.get("curr_period_label", "Sep 2026")))
    prev_lbl = html.escape(shorten_period_label(data.get("prev_period_label", "Sep 2025")))
    mkt_cap = html.escape(str(screener_info["market_cap"]))
    pe_ratio = html.escape(str(screener_info["pe_ratio"]))
    roe = html.escape(str(screener_info["roe"]))
    de_ratio = html.escape(str(screener_info["debt_to_equity"]))
    total_debt = html.escape(str(screener_info["total_debt"]))
    high_52w = html.escape(str(screener_info.get("high_52w", "N/A")))
    low_52w = html.escape(str(screener_info.get("low_52w", "N/A")))
    guj_summary = html.escape(str(data.get("gujarati_summary", "")))
    guj_view = html.escape(str(data.get("gujarati_balance_view", "")))

    if doc_type == "order":
        header_title = f"🏆 <b>{comp_name} - નવો ઓર્ડર / કોન્ટ્રેક્ટ અપડેટ</b>"
        ord_info = data.get("order_details", {}) or {}
        dynamic_block_gu = (
            f"📦 <b>ઓર્ડરની વિગત:</b> {html.escape(str(ord_info.get('order_value', '-')))} "
            f"({html.escape(str(ord_info.get('client_or_awarder', '-')))})\n\n"
        )
    else:
        header_title = f"📊 <b>{comp_name} - {qtr_lbl} ઓફિશિયલ BSE PDF રિઝલ્ટ</b>"
        rev = data.get("revenue", {"curr": "0", "prev": "0", "growth": "N/A"})
        gp = data.get("gross_profit", {"curr": "0", "prev": "0", "growth": "N/A"})
        np_d = data.get("net_profit", {"curr": "0", "prev": "0", "growth": "N/A"})
        dynamic_block_gu = (
            f"📈 <b>નાણાકીય કામગીરી ({curr_lbl} vs {prev_lbl}):</b>\n"
            f"• <b>આવક (Revenue):</b> ₹{html.escape(str(rev.get('curr', '0')))} Cr ({html.escape(str(rev.get('growth', 'N/A')))} YoY)\n"
            f"• <b>EBITDA:</b> ₹{html.escape(str(gp.get('curr', '0')))} Cr ({html.escape(str(gp.get('growth', 'N/A')))} YoY)\n"
            f"• <b>ચોખ્ખો નફો (PAT):</b> ₹{html.escape(str(np_d.get('curr', '0')))} Cr (<b>{html.escape(str(np_d.get('growth', 'N/A')))} YoY</b>)\n\n"
        )

    msg_html = (
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>તારીખ:</b> {res_date} | 💰 <b>ભાવ:</b> {price_disp}\n"
        f"📊 <b>52W High/Low:</b> {high_52w} / {low_52w}\n\n"
        f"{dynamic_block_gu}"
        f"🔍 <b>Mkt Cap:</b> {mkt_cap} | <b>P/E:</b> {pe_ratio} | <b>ROE:</b> {roe}\n"
        f"🏦 <b>Debt-to-Equity:</b> {de_ratio} ({total_debt})\n\n"
        f"📝 <b>સારાંશ:</b> {guj_summary}\n"
        f"💡 <b>ફંડામેન્ટલ નોંધ:</b> {guj_view}\n"
    )

    if blog_url:
        msg_html += f"\n🌐 <a href=\"{html.escape(blog_url, quote=True)}\"><b>ગુજરાતી, હિન્દી અને English માં પૂરો રિપોર્ટ વાંચો</b></a>"
    if source_url:
        msg_html += f" | <a href=\"{html.escape(source_url, quote=True)}\"><b>BSE PDF</b></a>"
    msg_html += "\n\n✅ <b>Market Munafa</b>"

    try:
        photo_to_send = thumb_url or chart_url
        if photo_to_send:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "photo": photo_to_send, "caption": f"📊 {comp_name} - Market Munafa Report"},
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
    state = {"comp_keys": set(), "scrip_keys": set(), "file_keys": set(), "recent_posts": []}
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
                if len(state["recent_posts"]) < 30 and post.get("title") and post.get("url"):
                    state["recent_posts"].append({
                        "title": post.get("title", ""),
                        "url": post.get("url", ""),
                        "labels": post.get("labels", []),
                    })
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


def select_approved_labels(doc_type, screener_info):
    if doc_type == "order":
        labels = ["MARKET NEWS"]
    else:
        labels = ["STOCKS RESULT", "MARKET NEWS"]
        if screener_info.get("debt_to_equity_num", 1.0) <= 0.25:
            labels.append("DEBT FREE 10 STOCKS")
    return [lb for lb in labels if lb in APPROVED_LABELS][:3]


def pick_related_posts(recent_posts, labels, max_links=3):
    if not recent_posts:
        return []
    lset = {str(l).lower() for l in labels}
    scored = []
    for rp in recent_posts:
        rset = {str(x).lower() for x in (rp.get("labels") or [])}
        scored.append((len(lset & rset), rp))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:max_links]]


def blogger_publish(title, content_html, labels, meta_description):
    if not BLOG_ID:
        raise RuntimeError("BLOG_ID is missing")
    token = blogger_access_token()
    clean_title = re.sub(r"[\r\n\t]+", " ", re.sub(r"<[^>]+>", "", html.unescape(str(title)))).strip()[:140]
    clean_meta = re.sub(r"\s+", " ", html.unescape(meta_description or "")).strip()
    if len(clean_meta) > 152:
        clean_meta = clean_meta[:149].rsplit(" ", 1)[0] + "..."

    payload = {
        "title": clean_title,
        "content": content_html,
        "labels": labels,
        "customMetaData": clean_meta,
    }

    for attempt in range(2):
        response = requests.post(
            f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=25,
        )
        if response.status_code == 400 and "customMetaData" in response.text:
            payload.pop("customMetaData", None)
            response = requests.post(
                f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
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


def publish_item(item, doc_type, ai_data, screener_info, blog_state, item_id, comp_key, scrip_key, file_key, scrip_code, theme_index):
    source_url = item["link"]
    c_short = ai_data.get("company_short_name") or screener_info.get("screener_company_name") or item["title"]
    raw_title = ai_data.get("seo_title") or (
        f"{c_short} Order Win Update & Financial Overview" if doc_type == "order" else f"{c_short} Quarterly Results & Fundamental Analysis"
    )
    labels = select_approved_labels(doc_type, screener_info)
    related_posts = pick_related_posts(blog_state.get("recent_posts", []), labels, max_links=3)

    # દરેક પોસ્ટ માટે અલગ યુનિક ઓટો-થમ્બનેલ બનાવી GitHub પર લાઈવ કરો
    slug_id = hashlib.md5(f"{c_short}|{scrip_code}|{item_id}".encode("utf-8")).hexdigest()[:10]
    thumb_badge = ai_data.get("thumb_badge") or ("ORDER WIN" if doc_type == "order" else "Q2 RESULTS")
    thumb_hl = ai_data.get("thumb_headline") or raw_title[:45]
    thumb_url = create_and_push_unique_thumbnail(
        comp_short=c_short,
        thumb_headline=thumb_hl,
        badge_text=thumb_badge,
        cap_label=screener_info.get("cap_label", "Listed"),
        price_disp=ai_data.get("price_display", screener_info["price_str"]),
        slug_id=slug_id,
        theme_index=theme_index,
    )

    blog_html, yoy_chart_url, meta_desc = build_post_html(
        ai_data,
        doc_type,
        screener_info,
        source_url,
        thumb_url=thumb_url,
        related_posts=related_posts,
        comp_key=comp_key,
        scrip_key=scrip_key,
        file_key=file_key,
        scrip_code=scrip_code,
    )

    blog_url = ""
    blogger_quota_hit = False
    if POST_TO_BLOGGER:
        res_url = blogger_publish(raw_title, blog_html, labels, meta_desc)
        if res_url is None:
            blogger_quota_hit = True
        else:
            blog_url = res_url
            mark_posted(item_id, comp_key, scrip_key, file_key, item["title"], source_url)
            blog_state["recent_posts"].insert(0, {"title": raw_title, "url": blog_url, "labels": labels})
            log.info("✅ Published 200/200 Verified Post (%s) | Labels: %s | URL: %s", doc_type, labels, blog_url)

    send_telegram_gujarati(ai_data, doc_type, screener_info, blog_url, source_url, thumb_url, yoy_chart_url)
    return not blogger_quota_hit


# ==========================================
# 10. MAIN RUNNER (UP TO 10 VERIFIED POSTS PER RUN)
# ==========================================
def run():
    init_db()
    log.info("Scanning existing posts on Market Munafa blog...")
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
    seen_in_run = set()

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

        run_dedup_token = scrip_key or comp_key or item_id
        if run_dedup_token in seen_in_run:
            continue
        seen_in_run.add(run_dedup_token)

        is_dup, _ = is_already_on_blog_or_db(item_id, comp_key, scrip_key, file_key, blog_state)
        if is_dup:
            continue

        try:
            screener_info = get_screener_fundamentals(item["title"], scrip_code=scrip_code)

            if not screener_info["is_eligible_stock"]:
                log.info("Skipping Micro-Cap/Unlisted/Mismatched Stock (Mkt Cap: %s): %s", screener_info["market_cap"], item["title"])
                continue

            if screener_info.get("screener_company_name"):
                scr_comp_key = normalize_company_key(screener_info["screener_company_name"], doc_type)
                if scr_comp_key and scr_comp_key in blog_state["comp_keys"]:
                    continue

            source_text, pdf_bytes, pdf_text_len = get_document_payload(item, scrip_code=scrip_code)
            file_key = extract_file_key(item["link"]) or file_key

            if not pdf_bytes:
                log.warning("Skipping %s because official BSE PDF could not be downloaded.", item["title"])
                continue

            # કડક 200/200 વેરિફિકેશન (સીધી પોસ્ટ બંધ — ભૂલ સુધરે પછી જ આગળ વધશે)
            ai_data = generate_and_verify_200_of_200(
                item["title"], scrip_code, doc_type, source_text, pdf_bytes, screener_info, pdf_text_len
            )
            if not ai_data:
                continue

            ai_comp_name = str(ai_data.get("company_short_name") or "").strip()
            ai_comp_key = normalize_company_key(ai_comp_name, doc_type) if ai_comp_name else ""
            if ai_comp_key and ai_comp_key in blog_state["comp_keys"]:
                continue

            final_comp_key = ai_comp_key or comp_key
            blogger_ok = publish_item(
                item=item,
                doc_type=doc_type,
                ai_data=ai_data,
                screener_info=screener_info,
                blog_state=blog_state,
                item_id=item_id,
                comp_key=final_comp_key,
                scrip_key=scrip_key,
                file_key=file_key,
                scrip_code=scrip_code,
                theme_index=published_count,
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

            time.sleep(15)
        except Exception:
            log.exception("Item processing failed: %s", item["title"])

    log.info("Run finished cleanly. Total 200/200 verified posts published: %d", published_count)


if __name__ == "__main__":
    run()
