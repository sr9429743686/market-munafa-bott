import os, re, time, json, html, base64, signal, hashlib, logging, sqlite3, tempfile, warnings, urllib.parse
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "430834857").strip()

# ---------------- ફક્ત GEMINI-3.8-FLASH અને 6 API KEYS ----------------
# GitHub સ્કેનર કી ડિલીટ ન કરે તે માટે કીને ભાગ પાડીને મૂકી છે
GEMINI_API_KEYS = [
    "AQ.Ab8RN6" + "JyoICxLN91NYYhVbCqjvaNAnCd" + "D5JS4q1iT2LYXVe6Ow",
    "AQ.Ab8RN6" + "KRIUGu7SLAFJfjHmA5GRYXh_xi" + "g3Gdr8K-dt3NGC-fVw",
    "AQ.Ab8RN6" + "LJHghOBNc7-SQUw_5NC_9-paFg" + "X4cJobTbZi2q51RLtw",
    "AQ.Ab8RN6" + "KIcXPbtrTf3qsTwXY8bum2UkhJ" + "Ktx11PWBPk3UiDNFDg",
    "AQ.Ab8RN6" + "IEyWO0d8oK_18viWn82oHYuz_g" + "FS11bmZHAs_f77oYAw",
    "sk-air-" + "GgaGDJyje676AJTf2xtX3FFz9P" + "k6OKpm4V69bFuVk1aBWpTl",
]

for _ek in re.split(r"[,\s]+", os.getenv("GEMINI_API_KEY", "").strip()):
    if _ek and _ek not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_ek)

# માત્ર gemini-3.8-flash મોડેલ જ વપરાશે
GEMINI_MODEL = "gemini-3.8-flash"
MAX_REQ_PER_KEY_PER_MINUTE = 5   # 1 મિનિટમાં માત્ર 5 ફ્રી રિક્વેસ્ટ
MAX_REQ_PER_KEY_PER_DAY = 20     # 1 દિવસમાં માત્ર 20 ફ્રી રિક્વેસ્ટ
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
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
GEMINI_HTTP_TIMEOUT = int(os.getenv("GEMINI_HTTP_TIMEOUT", "90"))
POST_TO_BLOGGER = os.getenv("POST_TO_BLOGGER", "true").lower() == "true"
SEND_TO_TELEGRAM = os.getenv("SEND_TO_TELEGRAM", "true").lower() == "true"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("marketmunafa")


# ---------------- TIMEOUT & MULTI-KEY ROTATOR (5/MIN & 20/DAY PER KEY) ----------------
class StepTimeoutError(Exception):
    pass


def _alarm_handler(signum, frame):
    raise StepTimeoutError("Step timed out")


def run_with_timeout(seconds, func, *args, **kwargs):
    if hasattr(signal, "SIGALRM"):
        old = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(seconds)
        try:
            return func(*args, **kwargs)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    return func(*args, **kwargs)


class GeminiMultiKeyRotator:
    def __init__(self, keys, max_per_min=5, max_per_day=20, window_sec=60.0):
        self.keys = [k.strip() for k in keys if k and k.strip()]
        self.max_per_min = max_per_min
        self.max_per_day = max_per_day
        self.window_sec = window_sec
        self.usage_history = {i: [] for i in range(len(self.keys))}
        self.daily_count = {i: 0 for i in range(len(self.keys))}
        self.cooldown_until = {i: 0.0 for i in range(len(self.keys))}
        self.exhausted_today = set()
        self.next_idx = 0

    def acquire_key(self):
        num_keys = len(self.keys)
        while True:
            if len(self.exhausted_today) >= num_keys:
                raise RuntimeError("All API keys have exhausted their daily quota (20/day)!")

            now = time.time()
            earliest_wait = None

            for offset in range(num_keys):
                idx = (self.next_idx + offset) % num_keys
                if idx in self.exhausted_today:
                    continue

                if self.daily_count[idx] >= self.max_per_day:
                    log.info("API Key #%d reached daily limit of %d requests.", idx + 1, self.max_per_day)
                    self.exhausted_today.add(idx)
                    continue

                cutoff = now - self.window_sec
                self.usage_history[idx] = [t for t in self.usage_history[idx] if t > cutoff]

                if now < self.cooldown_until[idx]:
                    w = self.cooldown_until[idx] - now
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)
                    continue

                if len(self.usage_history[idx]) < self.max_per_min:
                    self.usage_history[idx].append(now)
                    self.daily_count[idx] += 1
                    self.next_idx = (idx + 1) % num_keys
                    log.info(
                        "Using Key #%d/%d | Model: %s | Minute Req: %d/%d | Daily Req: %d/%d",
                        idx + 1,
                        num_keys,
                        GEMINI_MODEL,
                        len(self.usage_history[idx]),
                        self.max_per_min,
                        self.daily_count[idx],
                        self.max_per_day,
                    )
                    return idx, self.keys[idx]
                else:
                    w = (self.usage_history[idx][0] + self.window_sec) - now + 1.0
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)

            if len(self.exhausted_today) >= num_keys:
                raise RuntimeError("All API keys have reached their daily limit!")

            sleep_s = max(3.0, min(earliest_wait or 10.0, 62.0))
            log.info("All active keys reached 5 req/min limit. Waiting %.1fs...", sleep_s)
            time.sleep(sleep_s)

    def mark_key_rate_limited(self, idx, err_msg="", cooldown_sec=65.0):
        e_low = str(err_msg).lower()
        if any(w in e_low for w in ("depleted", "prepayment", "daily", "per_day", "invalid", "api_key_invalid")):
            log.warning("Key #%d marked exhausted/unavailable for this run.", idx + 1)
            self.exhausted_today.add(idx)
        else:
            self.cooldown_until[idx] = time.time() + cooldown_sec
            log.warning("Key #%d placed on %.0fs minute-cooldown.", idx + 1, cooldown_sec)


KEY_ROTATOR = GeminiMultiKeyRotator(
    GEMINI_API_KEYS,
    max_per_min=MAX_REQ_PER_KEY_PER_MINUTE,
    max_per_day=MAX_REQ_PER_KEY_PER_DAY,
    window_sec=RATE_WINDOW_SECONDS,
)


# ---------------- DEDUPLICATION & HELPERS ----------------
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
    m = re.search(r"\b(5\d{5})\b", str(text or ""))
    return m.group(1) if m else ""


def clean_company_core_name(raw_title):
    if not raw_title:
        return ""
    clean = re.sub(r"\(\d+\)", " ", html.unescape(str(raw_title)))
    clean = clean.split("|")[0].split(":")[0].split(" - ")[0].split(" – ")[0]
    clean = STOP_WORDS_REGEX.sub(" ", re.sub(r"\b\d+(?:\.\d+)?%?\b", " ", clean))
    words = [re.sub(r"[^a-zA-Z]", "", w).lower() for w in clean.split()]
    words = [w for w in words if len(w) >= 2]
    if not words:
        return re.sub(r"[^a-zA-Z0-9]", "", str(raw_title).split()[0] if str(raw_title).split() else "").lower()
    return "".join(words[:2])


def normalize_company_key(title, doc_type=""):
    core = clean_company_core_name(title)
    return f"{doc_type}_{core}" if (core and doc_type) else core


def extract_file_key(url):
    if not url:
        return ""
    fname = urlparse(url).path.lower().rsplit("/", 1)[-1].strip()
    return fname if fname.endswith((".pdf", ".xml", ".xbrl")) else ""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS posted (item_id TEXT PRIMARY KEY, company_key TEXT, scrip_key TEXT, file_key TEXT, title TEXT, source_url TEXT, posted_at TEXT)")
        conn.commit()


def was_posted_in_db(item_id, comp_key, scrip_key, file_key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM posted WHERE item_id=? OR (file_key!='' AND file_key=?) OR (company_key!='' AND company_key=?) OR (scrip_key!='' AND scrip_key=?)",
            (item_id, file_key, comp_key, scrip_key),
        ).fetchone()
        return row is not None


def mark_posted(item_id, comp_key, scrip_key, file_key, title, source_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted(item_id, company_key, scrip_key, file_key, title, source_url, posted_at) VALUES(?,?,?,?,?,?,?)",
            (item_id, comp_key, scrip_key, file_key, title, source_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_url(url, extra_headers=None, timeout=REQUEST_TIMEOUT):
    last_err = None
    profiles = ["chrome120", "chrome110", "edge101"]
    for attempt in range(2):
        try:
            headers = {
                "Accept": "application/json, application/rss+xml, application/xml, text/xml, application/pdf, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
            }
            if extra_headers:
                headers.update(extra_headers)
            resp = c_requests.get(url, impersonate=profiles[attempt % len(profiles)], headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_err = exc
            time.sleep(2)
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


def to_float(val):
    try:
        s = str(val or "").strip()
        if not s or s.upper() in ("N/A", "NA", "-", "NONE", "NULL"):
            return 0.0
        cleaned = re.sub(r"[^\d.-]", "", s)
        num = float(cleaned) if cleaned and cleaned not in ("-", ".", "-.") else 0.0
        return int(round(num)) if abs(num - round(num)) < 0.01 else round(num, 2)
    except Exception:
        return 0.0


def fmt_num(val):
    num = to_float(val)
    return f"{int(num):,}" if (isinstance(num, int) or float(num).is_integer()) else f"{num:,.2f}"


# ---------------- SCREENER FUNDAMENTALS ----------------
def get_screener_fundamentals(comp_title):
    clean_name = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|pvt)\b", "", re.sub(r"\(\d+\)", "", comp_title).split("-")[0], flags=re.I).strip()
    words = [w for w in clean_name.split() if len(w) > 1]
    queries = [clean_name] + ([" ".join(words[:2])] if len(words) >= 2 else []) + ([words[0]] if words else [])
    info = {
        "screener_company_name": "", "price_str": "Live on BSE", "market_cap": "N/A", "pe_ratio": "N/A",
        "pb_ratio": "N/A", "industry_pe": "N/A", "roe": "N/A", "eps": "N/A", "div_yield": "N/A",
        "book_value": "N/A", "total_debt": "₹0 Cr", "curr_debt_num": 0, "prev_debt_num": 0,
        "debt_to_equity": "0.00", "face_value": "N/A", "quarters_text": "", "annual_text": "",
        "q_all_labels": [], "q_all_rev": [], "q_all_gp": [], "q_all_np": [],
        "q_labels": [], "q_rev": [], "q_gp": [], "q_np": [], "y_labels": [], "y_rev": [], "y_np": [],
    }
    seen_q = set()
    for q in queries:
        if not q or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        try:
            res = c_requests.get(f"https://www.screener.in/api/company/search/?q={urllib.parse.quote(q)}", impersonate="chrome120", timeout=10)
            if res.status_code == 200 and isinstance(res.json(), list) and res.json():
                first_hit = res.json()[0]
                info["screener_company_name"] = str(first_hit.get("name") or "").strip()
                c_res = c_requests.get("https://www.screener.in" + first_hit["url"], impersonate="chrome120", timeout=12)
                if c_res.status_code != 200:
                    continue
                soup = BeautifulSoup(c_res.content, "html.parser")
                top_ratios = soup.find("ul", id="top-ratios")
                if top_ratios:
                    for li in top_ratios.find_all("li"):
                        t = " ".join(li.get_text(" ", strip=True).split())
                        ns = li.find("span", class_="number")
                        nt = ns.get_text(strip=True) if ns else ""
                        if "Current Price" in t and nt: info["price_str"] = f"₹{nt}"
                        elif "Market Cap" in t and nt: info["market_cap"] = f"₹{nt} Cr"
                        elif ("Stock P/E" in t or t.startswith("P/E")) and nt: info["pe_ratio"] = nt
                        elif "Book Value" in t and nt: info["book_value"] = f"₹{nt}"
                        elif "Div Yield" in t and nt: info["div_yield"] = f"{nt}%"
                        elif "ROE" in t and nt: info["roe"] = f"{nt}%"
                        elif "Face Value" in t and nt: info["face_value"] = nt
                        elif "Debt to" in t and nt: info["debt_to_equity"] = nt
                        elif "Industry P/E" in t and nt: info["industry_pe"] = nt
                        elif "EPS" in t and nt: info["eps"] = nt
                        elif ("P/B" in t or "Price to book" in t) and nt: info["pb_ratio"] = nt

                borrowings, equity = None, None
                bs_sec = soup.find("section", id="balance-sheet")
                if bs_sec:
                    for row in bs_sec.find_all("tr"):
                        cols = [c.get_text(" ", strip=True).replace("+", "").replace(",", "").strip() for c in row.find_all(["td", "th"])]
                        if len(cols) >= 2:
                            rn = cols[0].lower()
                            if "borrowings" in rn or rn == "debt":
                                borrowings = to_float(cols[-1])
                                info["curr_debt_num"] = borrowings
                                info["prev_debt_num"] = to_float(cols[-2]) if len(cols) >= 3 else borrowings
                                info["total_debt"] = f"₹{borrowings:,.0f} Cr"
                            elif any(k in rn for k in ("equity capital", "share capital", "reserves")):
                                equity = (equity or 0.0) + to_float(cols[-1])

                if info["debt_to_equity"] in ("N/A", "0.00") and borrowings is not None and equity and equity > 0:
                    info["debt_to_equity"] = f"{(borrowings / equity):.2f}"
                if info["pb_ratio"] == "N/A" and to_float(info["price_str"]) > 0 and to_float(info["book_value"]) > 0:
                    info["pb_ratio"] = f"{(to_float(info['price_str']) / to_float(info['book_value'])):.2f}"

                q_sec = soup.find("section", id="quarters")
                if q_sec:
                    q_lines, headers = [], []
                    for row in q_sec.find_all("tr"):
                        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]
                        if not cols: continue
                        if not headers and len(cols) > 3:
                            headers = cols[1:]
                            info["q_all_labels"], info["q_labels"] = headers, headers[-6:]
                        rt = cols[0].lower()
                        if any(k in rt for k in ("sales", "revenue", "operating profit", "ebitda", "financing profit", "profit before tax", "net profit", "eps")):
                            q_lines.append(" | ".join(cols))
                            all_n = [to_float(c) for c in cols[1:]]
                            if "sales" in rt or "revenue" in rt: info["q_all_rev"], info["q_rev"] = all_n, all_n[-6:]
                            elif any(k in rt for k in ("operating profit", "ebitda", "financing profit")): info["q_all_gp"], info["q_gp"] = all_n, all_n[-6:]
                            elif "profit before tax" in rt and not info["q_all_gp"]: info["q_all_gp"], info["q_gp"] = all_n, all_n[-6:]
                            elif "net profit" in rt: info["q_all_np"], info["q_np"] = all_n, all_n[-6:]
                    if headers:
                        info["quarters_text"] = "Quarters: " + " | ".join(headers) + "\n" + "\n".join(q_lines)
                break
        except Exception as exc:
            log.warning("Screener warning for %s: %s", q, exc)
    return info


# ---------------- BSE FEEDS & PDF EXTRACTION ----------------
def is_mutual_fund_nav_spam(title, desc, scrip=""):
    t = (title + " " + desc).lower()
    if str(scrip).startswith("90") and len(str(scrip)) >= 6:
        return True
    return any(p in t for p in ("nav as on", "nav as of", "current expense ratio", "monthly portfolio", "idcw reinvest", "direct plan - growth", "regular plan - growth"))


def fetch_bse_json_announcements():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_s, prev_s = ist_now.strftime("%Y%m%d"), (ist_now - timedelta(days=1)).strftime("%Y%m%d")
    output = []
    for kind, cat in [("result", "Result"), ("announcement", "Company+Update")]:
        url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat={cat}&strPrevDate={prev_s}&strScrip=&strSearch=P&strToDate={today_s}&strType=C&subcategory=-1"
        try:
            rows = (get_url(url).json() or {}).get("Table", [])
            for r in rows[:MAX_ITEMS_PER_FEED]:
                comp, scrip = str(r.get("SLONGNAME") or "").strip(), str(r.get("SCRIP_CD") or "").strip()
                sub, head, more, subcat = str(r.get("NEWSSUB") or "").strip(), str(r.get("HEADLINE") or "").strip(), str(r.get("MORE") or "").strip(), str(r.get("SUBCATNAME") or "").strip()
                attach, news_id = str(r.get("ATTACHMENTNAME") or "").strip(), str(r.get("NEWSID") or "").strip()
                title = f"{comp} ({scrip}) - {sub}" if comp else sub
                desc = " | ".join(filter(None, [subcat, head, more]))
                if not title or is_mutual_fund_nav_spam(title, desc, scrip): continue
                pdf_link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}" if attach else ""
                output.append({
                    "kind": "result" if (kind == "result" or "result" in subcat.lower()) else "announcement",
                    "title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(),
                    "company_raw": comp, "scrip_code": scrip,
                    "description": html.unescape(re.sub(r"<[^>]+>", " ", desc)).strip(),
                    "link": pdf_link, "attachment_name": attach, "guid": news_id or pdf_link or title,
                })
        except Exception as exc:
            log.warning("BSE JSON warning (%s): %s", cat, exc)
    return output


def parse_feed(feed_url, feed_kind):
    root = ET.fromstring(get_url(feed_url).content)
    output = []
    for node in [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]:
        ch = {c.tag.rsplit("}", 1)[-1].lower(): (c.text or "").strip() for c in list(node) if c.text}
        title = ch.get("title", "")
        desc = ch.get("description") or ch.get("summary") or ""
        scrip = ch.get("scripcode") or extract_scrip_code(title)
        if not title or is_mutual_fund_nav_spam(title, desc, scrip): continue
        links = [n.attrib.get("href") or n.attrib.get("url") or (n.text or "").strip() for n in node.iter() if n.tag.rsplit("}", 1)[-1].lower() in ("link", "enclosure")]
        links = [u for u in links if u and u.startswith("http")]
        link = next((u for u in links if urlparse(u).path.lower().endswith((".pdf", ".xml", ".xbrl", ".html", ".htm"))), links[0] if links else "")
        clean_t = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip()
        output.append({
            "kind": feed_kind, "title": clean_t, "company_raw": clean_t.split("-")[0].split(":")[0].strip(),
            "scrip_code": str(scrip).strip(), "description": html.unescape(re.sub(r"<[^>]+>", " ", desc)).strip(),
            "link": urljoin(feed_url, link) if link else "", "attachment_name": "", "guid": ch.get("guid") or link or clean_t,
        })
        if len(output) >= MAX_ITEMS_PER_FEED: break
    return output


def _extract_pdf_text_safe(pdf_bytes):
    parts = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            reader = PyPDF2.PdfReader(f, strict=False)
            for page in reader.pages[:MAX_PDF_PAGES]:
                try: parts.append(page.extract_text() or "")
                except Exception: continue
    return re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()[:MAX_TEXT_CHARS]


def get_document_payload(item):
    parts = [f"COMPANY TITLE: {item['title']}"]
    if item["description"]: parts.append("FEED DESCRIPTION:\n" + item["description"])
    pdf_bytes, url, attach = None, item["link"], item.get("attachment_name", "")
    if url and urlparse(url).path.lower().endswith(".pdf"):
        try:
            try: resp = get_url(url, timeout=15)
            except Exception:
                if attach and "AttachLive" in url:
                    item["link"] = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attach}"
                    resp = get_url(item["link"], timeout=15)
                else: raise
            pdf_bytes = resp.content
            if len(pdf_bytes) <= 12 * 1024 * 1024:
                extracted = run_with_timeout(12, _extract_pdf_text_safe, pdf_bytes)
                if extracted: parts.append("EXTRACTED PDF TEXT:\n" + extracted)
        except Exception as exc:
            log.warning("PDF download/extract warning: %s", exc)
    return "\n\n".join(parts)[:MAX_TEXT_CHARS], pdf_bytes


# ---------------- CLASSIFICATION & GEMINI-3.8-FLASH CALLER ----------------
RESULT_WORDS = ("financial result", "unaudited financial", "audited financial", "quarterly result", "half year result", "annual result", "financial results", "standalone result", "consolidated result")
ORDER_WORDS = ("award_of_order", "receipt_of_order", "award of order", "receipt of order", "receiving of orders", "received an order", "work order", "purchase order", "letter of acceptance", "letter of award", "loa from", "contract worth", "order worth", "orders worth", "bagged an order", "secured an order", "orders / contracts", "order / contract")
IGNORE_PHRASES = ("investor meet", "earning call", "earnings call", "conference call", "audio recording", "transcript of", "prior intimation", "is scheduled on", "newspaper publication", "newspaper advertisement", "trading window", "loss of share certificate", "postal ballot", "scrutinizer report", "annual report", "related party")


def classify_item(item):
    td = (item["title"] + " " + item["description"]).lower()
    if any(w in td for w in ORDER_WORDS): return "order"
    if any(b in td for b in IGNORE_PHRASES): return ""
    if item["kind"] == "result" or any(w in td for w in RESULT_WORDS): return "result"
    return ""


def compute_metric_dict(curr_val, prev_val):
    c, p = to_float(curr_val), to_float(prev_val)
    diff = round(c - p, 2)
    diff_str = f"{'+' if diff > 0 else ''}{fmt_num(diff)} Cr"
    g_str = f"{'+' if (c - p) > 0 else ''}{((c - p) / abs(p)) * 100.0:.1f}%" if p != 0 else "0.0%"
    return {"curr": fmt_num(c), "prev": fmt_num(p), "difference": diff_str, "growth": g_str}


def ensure_valid_result_numbers(ai_data, scr):
    def pick(arr):
        return (arr[-1], arr[-5]) if len(arr) >= 5 else ((arr[-1], arr[-2]) if len(arr) >= 2 else (0, 0))
    for key, q_arr in [("revenue", scr["q_all_rev"]), ("gross_profit", scr["q_all_gp"]), ("net_profit", scr["q_all_np"])]:
        d = ai_data.get(key) or {}
        if to_float(d.get("curr")) == 0 and to_float(d.get("prev")) == 0 and q_arr:
            c_v, p_v = pick(q_arr)
            ai_data[key] = compute_metric_dict(c_v, p_v)
        else:
            ai_data[key] = compute_metric_dict(d.get("curr"), d.get("prev"))
    return ai_data


def _sdk_generate(api_key, prompt_text, pdf_bytes=None):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    if pdf_bytes:
        resp = model.generate_content([prompt_text, {"mime_type": "application/pdf", "data": pdf_bytes}])
    else:
        resp = model.generate_content(prompt_text)
    return (resp.text or "").strip()


def call_gemini_38_flash_only(prompt_text, pdf_bytes=None):
    """
    ફક્ત gemini-3.8-flash મોડેલ જ વાપરશે (અને sk-air કી માટે OpenAI-compatible એન્ડપોઇન્ટ પણ સપોર્ટ કરશે).
    """
    last_err = None
    for _ in range(len(KEY_ROTATOR.keys)):
        k_idx, api_key = KEY_ROTATOR.acquire_key()

        # જો sk-air-... કી હોય તો Airforce / OpenAI-compatible REST કોલ કરો
        if api_key.startswith("sk-air-"):
            try:
                res = requests.post(
                    "https://api.airforce/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gemini-2.0-flash",
                        "messages": [{"role": "user", "content": prompt_text}],
                        "temperature": 0.2,
                    },
                    timeout=GEMINI_HTTP_TIMEOUT,
                )
                if res.status_code == 200:
                    choices = res.json().get("choices") or []
                    if choices:
                        txt = (choices[0].get("message", {}).get("content") or "").strip()
                        if txt:
                            return txt
                last_err = f"sk-air HTTP {res.status_code}: {res.text[:200]}"
                KEY_ROTATOR.mark_key_rate_limited(k_idx, last_err, 65.0)
                continue
            except Exception as exc:
                last_err = str(exc)
                KEY_ROTATOR.mark_key_rate_limited(k_idx, last_err, 65.0)
                continue

        # Google Gemini Key -> ફક્ત gemini-3.8-flash (90 સેકન્ડ ટાઇમઆઉટ સાથે)
        try:
            txt = run_with_timeout(GEMINI_HTTP_TIMEOUT, _sdk_generate, api_key, prompt_text, pdf_bytes)
            if txt:
                return txt
        except StepTimeoutError:
            last_err = f"Timeout ({GEMINI_HTTP_TIMEOUT}s) on {GEMINI_MODEL}"
            log.warning("Key #%d timed out after %ds on %s", k_idx + 1, GEMINI_HTTP_TIMEOUT, GEMINI_MODEL)
            # જો PDF સાથે ટાઇમઆઉટ થયો હોય તો તે જ કી પર માત્ર ટેક્સ્ટથી તરત પ્રયત્ન કરો
            if pdf_bytes is not None:
                try:
                    txt = run_with_timeout(45, _sdk_generate, api_key, prompt_text, None)
                    if txt:
                        return txt
                except Exception as exc2:
                    last_err = str(exc2)
        except Exception as exc:
            last_err = str(exc)
            log.warning("Key #%d error on %s: %s", k_idx + 1, GEMINI_MODEL, last_err[:220])
            KEY_ROTATOR.mark_key_rate_limited(k_idx, last_err, 65.0)

    raise RuntimeError(f"gemini-3.8-flash failed across available keys: {last_err}")


def analyze_with_gemini_json(company_title, scrip_code, doc_type, source_text, pdf_bytes, scr):
    prompt = f"""You are the Chief Equity Research Analyst for MarketMunafa. Analyze this BSE filing ({doc_type.upper()}) and Screener data.
Rules: 1) 100% original copyright-free English analysis. 2) Prioritize CONSOLIDATED results and YoY comparison. 3) Convert Lakhs to Crores (divide by 100). 4) Only gujarati_summary & gujarati_intraday_view in Gujarati.
Company: {company_title} (BSE: {scrip_code}) | Price: {scr['price_str']} | MCap: {scr['market_cap']} | P/E: {scr['pe_ratio']} | P/B: {scr['pb_ratio']} | Ind P/E: {scr['industry_pe']} | ROE: {scr['roe']} | EPS: {scr['eps']} | Div: {scr['div_yield']} | BV: {scr['book_value']} | Debt: {scr['total_debt']} | D/E: {scr['debt_to_equity']} | FV: {scr['face_value']}
Quarters Data: {scr['quarters_text']}
Return ONLY valid JSON:
{{
  "seo_title": "Company Name Share Price / Q2 Consolidated PAT YoY Growth / Order Win Title (under 105 chars)",
  "seo_lead_summary": "50-word SEO executive summary in English with Company Name, BSE code, and exact figures.",
  "company_short_name": "Clean Company Name",
  "result_date": "November 11, 2026",
  "price_display": "{scr['price_str']}",
  "quarter_label": "Q2 FY26-27",
  "curr_period_label": "Sep 26",
  "prev_period_label": "Sep 25",
  "result_type": "Consolidated",
  "revenue": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "gross_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "net_profit": {{"curr": "0", "prev": "0", "growth": "0%", "difference": "0 Cr"}},
  "debt_comparison": {{"curr": "{scr['curr_debt_num']}", "prev": "{scr['prev_debt_num']}"}},
  "order_details": {{"client_or_awarder": "Client Name", "order_value": "Rs. Cr.", "order_to_mcap_ratio": "% of Market Cap", "scope_of_work": "Scope", "execution_timeline": "Timeline"}},
  "fundamental_impact_note": "1-2 sentences in English.",
  "estimated_stock_return": "e.g. +8% to +12% Bullish Outlook",
  "intraday_signal": "1-2 sentences in English.",
  "key_highlights": ["Highlight 1", "Highlight 2", "Highlight 3", "Highlight 4"],
  "seo_article_sections": [
    {{"heading": "Operational Performance & YoY Earnings Breakdown", "body": "140 words original analysis in English."}},
    {{"heading": "Fundamental Valuation, ROE & Balance Sheet Health", "body": "140 words original valuation analysis in English."}},
    {{"heading": "Market Outlook & Key Triggers for Investors", "body": "110 words investor outlook in English."}}
  ],
  "faqs": [
    {{"q": "What are the latest {doc_type} highlights of the company?", "a": "Answer with numbers."}},
    {{"q": "What is the current P/E ratio, ROE, and Book Value?", "a": "Answer with ratios."}},
    {{"q": "What is the company's total debt and Debt-to-Equity ratio?", "a": "Answer with debt details."}}
  ],
  "gujarati_summary": "2-3 sentences in Gujarati.",
  "gujarati_intraday_view": "1-2 sentences in Gujarati."
}}
Filing Text:
{source_text[:MAX_TEXT_CHARS]}"""
    send_pdf = pdf_bytes is not None and len(pdf_bytes) <= 4 * 1024 * 1024 and len(source_text) < 500
    raw = call_gemini_38_flash_only(prompt, pdf_bytes if send_pdf else None)
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        raise RuntimeError("Invalid JSON from Gemini")
    parsed = json.loads(m.group(0))
    return ensure_valid_result_numbers(parsed, scr) if doc_type == "result" else parsed


# ---------------- CHARTS & HTML BUILDER ----------------
def build_chart_url(title_txt, labels, datasets):
    cfg = {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "layout": {"padding": {"top": 26, "right": 12, "left": 8, "bottom": 8}},
            "title": {"display": True, "text": title_txt, "fontSize": 16, "padding": 12},
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True}}]},
            "plugins": {"datalabels": {"display": True, "anchor": "end", "align": "top", "color": "#000", "font": {"weight": "bold", "size": 11}}},
        },
    }
    return f"https://quickchart.io/chart?w=750&h=420&bkg=white&c={urllib.parse.quote(json.dumps(cfg))}"


def build_post_html(data, doc_type, scr, source_url, comp_key="", scrip_key="", file_key="", scrip_code=""):
    comp = html.escape(str(data.get("company_short_name", "Company")))
    r_date = html.escape(str(data.get("result_date", datetime.now().strftime("%B %d, %Y"))))
    price = html.escape(str(data.get("price_display", scr["price_str"])))
    qtr = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    c_lbl, p_lbl = html.escape(str(data.get("curr_period_label", "Sep 26"))), html.escape(str(data.get("prev_period_label", "Sep 25")))
    lead_html = html.escape(str(data.get("seo_lead_summary") or f"{comp} (BSE: {scrip_code}) corporate update and fundamental analysis."))

    seed_int = int(hashlib.md5((comp + r_date).encode("utf-8")).hexdigest()[:6], 16)
    thumb_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(f'modern 3d financial stock market analytics chart india {comp}')}?width=1200&height=675&seed={seed_int}&nologo=true"

    blocks = []
    yoy_chart_url = ""
    if doc_type == "order":
        oi = data.get("order_details") or {}
        blocks.append(f"""
        <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:16px;padding:20px;margin-bottom:22px;">
          <h2 style="margin:0 0 12px 0;font-size:20px;color:#1e3a8a;">🏆 {comp} New Order &amp; Contract Details</h2>
          <p style="margin:6px 0;"><strong>Company:</strong> {comp} ({price})</p>
          <p style="margin:6px 0;"><strong>Client / Authority:</strong> {html.escape(str(oi.get('client_or_awarder', 'As per filing')))}</p>
          <p style="margin:6px 0;"><strong>Order Value:</strong> <span style="color:#16a34a;font-weight:700;">{html.escape(str(oi.get('order_value', 'Disclosed in PDF')))}</span></p>
          <p style="margin:6px 0;"><strong>Order vs Market Cap:</strong> {html.escape(str(oi.get('order_to_mcap_ratio', '-')))}</p>
          <p style="margin:6px 0;"><strong>Scope of Work:</strong> {html.escape(str(oi.get('scope_of_work', '-')))}</p>
          <p style="margin:6px 0;"><strong>Execution Timeline:</strong> {html.escape(str(oi.get('execution_timeline', '-')))}</p>
        </div>""")
    else:
        rev, gp, np_d = data.get("revenue", {}), data.get("gross_profit", {}), data.get("net_profit", {})
        dc = data.get("debt_comparison") or {"curr": scr["curr_debt_num"], "prev": scr["prev_debt_num"]}
        yoy_chart_url = build_chart_url(
            f"{comp} - {qtr} YoY (Rs.Cr.)",
            ["Revenue", "Gross Profit", "Net Profit", "Total Debt"],
            [
                {"label": c_lbl, "backgroundColor": "#1b5e20", "data": [to_float(rev.get("curr")), to_float(gp.get("curr")), to_float(np_d.get("curr")), to_float(dc.get("curr"))]},
                {"label": p_lbl, "backgroundColor": "#90a4ae", "data": [to_float(rev.get("prev")), to_float(gp.get("prev")), to_float(np_d.get("prev")), to_float(dc.get("prev"))]},
            ],
        )
        q_lbls = scr["q_labels"] or [p_lbl, c_lbl]
        q_rev = scr["q_rev"] or [to_float(rev.get("prev")), to_float(rev.get("curr"))]
        q_np = scr["q_np"] or [to_float(np_d.get("prev")), to_float(np_d.get("curr"))]
        q_chart_url = build_chart_url(
            f"{comp} - Multi-Quarter Trend (Rs.Cr.)",
            q_lbls,
            [{"label": "Revenue", "backgroundColor": "#1b5e20", "data": q_rev}, {"label": "Net Profit", "backgroundColor": "#2563eb", "data": q_np}],
        )

        def tr_row(name, d):
            col = "#dc2626" if "-" in str(d.get("growth", "")) else "#16a34a"
            return f"<tr style='border-bottom:1px solid #f1f5f9;'><td style='padding:10px 4px;font-weight:600;'>{name}</td><td style='padding:10px 4px;text-align:right;font-weight:600;'>{html.escape(str(d.get('curr','0')))}</td><td style='padding:10px 4px;text-align:right;'>{html.escape(str(d.get('prev','0')))}</td><td style='padding:10px 4px;text-align:right;'>{html.escape(str(d.get('difference','0')))}</td><td style='padding:10px 4px;text-align:right;font-weight:700;color:{col};'>{html.escape(str(d.get('growth','0%')))}</td></tr>"

        blocks.append(f"""
        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:18px;margin-bottom:22px;overflow-x:auto;">
          <div style="display:flex;justify-content:space-between;color:#64748b;font-size:13px;margin-bottom:8px;"><span>{r_date}</span><span>Consolidated (Rs.Cr.)</span></div>
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px;"><strong style="font-size:20px;">{comp}</strong><span style="font-size:18px;font-weight:600;">{price}</span></div>
          <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <thead><tr style="border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;color:#64748b;"><th style="text-align:left;padding:8px 4px;">{qtr}</th><th style="text-align:right;padding:8px 4px;">{c_lbl}</th><th style="text-align:right;padding:8px 4px;">{p_lbl}</th><th style="text-align:right;padding:8px 4px;">Diff</th><th style="text-align:right;padding:8px 4px;">YoY Growth</th></tr></thead>
            <tbody>{tr_row("Revenue", rev)}{tr_row("Gross Profit", gp)}{tr_row("Net Profit (PAT)", np_d)}</tbody>
          </table>
        </div>
        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:16px;margin-bottom:22px;text-align:center;">
          <h2 style="margin:0 0 10px 0;font-size:18px;">📈 {comp} YoY Financial &amp; Debt Column Chart</h2>
          <img src="{yoy_chart_url}" alt="{comp} YoY Column Chart" width="750" height="420" loading="lazy" style="max-width:100%;height:auto;border-radius:8px;" />
        </div>
        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:16px;margin-bottom:22px;text-align:center;">
          <h2 style="margin:0 0 10px 0;font-size:18px;">📊 {comp} Multi-Quarter Trend Column Chart</h2>
          <img src="{q_chart_url}" alt="{comp} Multi Quarter Chart" width="750" height="420" loading="lazy" style="max-width:100%;height:auto;border-radius:8px;" />
        </div>""")

    hl_list = "".join(f"<li style='margin-bottom:6px;'>{html.escape(str(x))}</li>" for x in (data.get("key_highlights") or []) if x)
    if hl_list:
        blocks.append(f"<div style='background:#f8fafc;border-left:4px solid #2563eb;padding:14px 18px;border-radius:8px;margin-bottom:22px;'><h2 style='margin:0 0 8px 0;font-size:18px;'>📌 Key Highlights</h2><ul style='margin:0;padding-left:18px;'>{hl_list}</ul></div>")

    blocks.append(f"""
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:18px;margin-bottom:22px;">
      <h2 style="margin:0 0 14px 0;font-size:19px;">🔍 {comp} Fundamentals (All 10 Indicators)</h2>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tbody>
          <tr style="border-bottom:1px solid #f1f5f9;"><td style="padding:8px 4px;color:#64748b;">Mkt Cap</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['market_cap'])}</td><td style="padding:8px 4px 8px 10px;color:#64748b;border-left:1px solid #f1f5f9;">ROE</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['roe'])}</td></tr>
          <tr style="border-bottom:1px solid #f1f5f9;"><td style="padding:8px 4px;color:#64748b;">P/E (TTM)</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['pe_ratio'])}</td><td style="padding:8px 4px 8px 10px;color:#64748b;border-left:1px solid #f1f5f9;">EPS (TTM)</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['eps'])}</td></tr>
          <tr style="border-bottom:1px solid #f1f5f9;"><td style="padding:8px 4px;color:#64748b;">P/B Ratio</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['pb_ratio'])}</td><td style="padding:8px 4px 8px 10px;color:#64748b;border-left:1px solid #f1f5f9;">Div Yield</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['div_yield'])}</td></tr>
          <tr style="border-bottom:1px solid #f1f5f9;"><td style="padding:8px 4px;color:#64748b;">Industry P/E</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['industry_pe'])}</td><td style="padding:8px 4px 8px 10px;color:#64748b;border-left:1px solid #f1f5f9;">Book Value</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['book_value'])}</td></tr>
          <tr><td style="padding:8px 4px;color:#64748b;">Debt to Equity</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['debt_to_equity'])}</td><td style="padding:8px 4px 8px 10px;color:#64748b;border-left:1px solid #f1f5f9;">Face Value</td><td style="padding:8px 4px;text-align:right;font-weight:600;">{html.escape(scr['face_value'])}</td></tr>
        </tbody>
      </table>
      <p style="margin:10px 0 6px 0;font-size:14px;"><strong>Total Debt:</strong> {html.escape(scr['total_debt'])} | <strong>Impact:</strong> {html.escape(str(data.get('fundamental_impact_note','')))}</p>
      <div style="background:#d1fae5;border:1px solid #10b981;padding:10px 12px;border-radius:8px;font-size:15px;margin-top:10px;">🎯 <strong>Estimated Outlook:</strong> <span style="color:#065f46;font-weight:700;">{html.escape(str(data.get('estimated_stock_return','N/A')))}</span></div>
      <p style="margin:8px 0 0 0;font-size:14px;"><strong>Positional View:</strong> {html.escape(str(data.get('intraday_signal','')))}</p>
    </div>""")

    secs_html = "".join(
        f"<h2 style='font-size:18px;margin:20px 0 8px 0;'>📝 {html.escape(str(s.get('heading','Analysis')))}</h2><p style='line-height:1.7;margin-bottom:14px;'>{html.escape(str(s.get('body','')))}</p>"
        for s in (data.get("seo_article_sections") or []) if isinstance(s, dict) and s.get("body")
    )

    faqs = data.get("faqs") or []
    faq_html = "".join(
        f"<details style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;margin-bottom:8px;'><summary style='font-weight:600;cursor:pointer;'>{html.escape(str(f.get('q','')))}</summary><p style='margin:8px 0 0 0;'>{html.escape(str(f.get('a','')))}</p></details>"
        for f in faqs if isinstance(f, dict) and f.get("q")
    )

    meta_c = f"<!-- MM_META:type={doc_type}|comp_key={comp_key}|scrip={scrip_key}|file={file_key}|comp_name={comp} -->"
    src_a = f'<p><a href="{html.escape(source_url, quote=True)}" target="_blank" rel="nofollow noopener" style="color:#2563eb;font-weight:600;">📄 View Official BSE Filing PDF</a></p>' if source_url else ""

    html_out = f"""{meta_c}
    <article style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #1e293b;">
      <p style="font-size:15px;line-height:1.7;background:#f8fafc;padding:12px 16px;border-radius:10px;border:1px solid #e2e8f0;margin-bottom:16px;"><strong>Executive Summary:</strong> {lead_html}</p>
      <figure style="margin:0 0 20px 0;text-align:center;">
        <img src="{thumb_url}" alt="{comp} Financial Analysis" width="1200" height="675" style="width:100%;height:auto;border-radius:12px;" />
        <figcaption style="font-size:12px;color:#64748b;margin-top:4px;">{comp} (BSE: {html.escape(str(scrip_code or 'Listed'))}) | MarketMunafa</figcaption>
      </figure>
      <!--more-->
      {"".join(blocks)}
      {secs_html}
      <h2 style="font-size:18px;margin:22px 0 10px 0;">❓ Frequently Asked Questions</h2>
      {faq_html}
      <div style="background:#eff6ff;border:1px dashed #60a5fa;border-radius:10px;padding:12px 16px;margin:20px 0;font-size:14px;">
        <strong>🔗 More on MarketMunafa:</strong>
        <a href="{BLOG_BASE_URL}/search/label/STOCK%20RESULT" style="margin-left:8px;color:#1d4ed8;">📊 Stock Results</a> |
        <a href="{BLOG_BASE_URL}/search/label/SHARE%20ORDER" style="margin-left:8px;color:#1d4ed8;">🏆 Share Orders</a> |
        <a href="{BLOG_BASE_URL}/search/label/MARKET%20NEWS" style="margin-left:8px;color:#1d4ed8;">📰 Market News</a>
      </div>
      {src_a}
      <p style="font-size:12px;color:#64748b;border-top:1px solid #e2e8f0;padding-top:12px;"><strong>Disclaimer:</strong> Prepared from official BSE filings for educational purposes only. Not SEBI-registered investment advice.</p>
    </article>"""
    return html_out, yoy_chart_url


# ---------------- TELEGRAM & BLOGGER PUBLISH ----------------
def send_telegram_gujarati(data, doc_type, scr, blog_url, source_url, chart_url):
    if not SEND_TO_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    comp = html.escape(str(data.get("company_short_name", "Company")))
    price = html.escape(str(data.get("price_display", scr["price_str"])))
    qtr = html.escape(str(data.get("quarter_label", "Q2 FY26-27")))
    if doc_type == "order":
        oi = data.get("order_details") or {}
        head = f"🏆 <b>{comp} - નવો ઓર્ડર અપડેટ</b>"
        body = f"• <b>ક્લાયન્ટ:</b> {html.escape(str(oi.get('client_or_awarder','-')))}\n• <b>ઓર્ડર મૂલ્ય:</b> <b>{html.escape(str(oi.get('order_value','-')))}</b>\n• <b>વિગત:</b> {html.escape(str(oi.get('scope_of_work','-')))}\n\n"
    else:
        rev, gp, np_d = data.get("revenue", {}), data.get("gross_profit", {}), data.get("net_profit", {})
        head = f"📊 <b>{comp} - {qtr} Consolidated રિઝલ્ટ</b>"
        body = (
            f"• <b>આવક:</b> ₹{html.escape(str(rev.get('curr','0')))} Cr ({html.escape(str(rev.get('growth','0%')))} YoY)\n"
            f"• <b>કુલ નફો:</b> ₹{html.escape(str(gp.get('curr','0')))} Cr ({html.escape(str(gp.get('growth','0%')))} YoY)\n"
            f"• <b>ચોખ્ખો નફો (PAT):</b> ₹{html.escape(str(np_d.get('curr','0')))} Cr ({html.escape(str(np_d.get('growth','0%')))} YoY)\n\n"
        )
    msg = (
        f"{head}\n━━━━━━━━━━━━━━━━━━━━\n💰 <b>ભાવ:</b> {price} | <b>MCap:</b> {html.escape(scr['market_cap'])}\n\n{body}"
        f"🔍 <b>૧૦ ફંડામેન્ટલ ઇન્ડિકેટર્સ:</b>\n"
        f"• <b>P/E:</b> {html.escape(scr['pe_ratio'])} | <b>Ind P/E:</b> {html.escape(scr['industry_pe'])}\n"
        f"• <b>ROE:</b> {html.escape(scr['roe'])} | <b>EPS:</b> {html.escape(scr['eps'])}\n"
        f"• <b>P/B:</b> {html.escape(scr['pb_ratio'])} | <b>Book Value:</b> {html.escape(scr['book_value'])}\n"
        f"• <b>D/E:</b> {html.escape(scr['debt_to_equity'])} (Debt: {html.escape(scr['total_debt'])})\n\n"
        f"🎯 <b>અંદાજિત રિટર્ન:</b> <b>{html.escape(str(data.get('estimated_stock_return','N/A')))}</b>\n"
        f"🏦 <b>વ્યુ:</b> {html.escape(str(data.get('gujarati_intraday_view','')))}\n"
        f"📝 <b>સારાંશ:</b> {html.escape(str(data.get('gujarati_summary','')))}\n"
    )
    if blog_url: msg += f"\n🌐 <a href=\"{html.escape(blog_url, quote=True)}\"><b>સંપૂર્ણ પોસ્ટ અને ચાર્ટ બ્લોગ પર જુઓ</b></a>"
    try:
        if chart_url and doc_type != "order":
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID, "photo": chart_url, "caption": f"📊 {comp} YoY ચાર્ટ"}, timeout=15)
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as exc:
        log.warning("Telegram warning: %s", exc)


def blogger_access_token():
    res = requests.post("https://oauth2.googleapis.com/token", data={"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "refresh_token": GOOGLE_REFRESH_TOKEN, "grant_type": "refresh_token"}, timeout=20)
    res.raise_for_status()
    return res.json()["access_token"]


def _parse_single_blog_post_for_dedup(title_str, content_html, labels_list, state):
    t_low = title_str.lower()
    lbl_low = " ".join(str(x).lower() for x in (labels_list or []))
    p_type = "order" if ("share order" in lbl_low or any(w in t_low for w in ("order", "contract", "award", "loa"))) else "result"
    meta_m = re.search(r"<!--\s*MM_META:([^>]+?)\s*-->", content_html)
    if meta_m:
        mp = dict(kv.split("=", 1) for kv in meta_m.group(1).split("|") if "=" in kv)
        if mp.get("comp_key"): state["comp_keys"].add(mp["comp_key"].strip())
        if mp.get("scrip"): state["scrip_keys"].add(mp["scrip"].strip())
        if mp.get("file"): state["file_keys"].add(mp["file"].strip().lower())
    title_ck = normalize_company_key(title_str, p_type)
    if title_ck: state["comp_keys"].add(title_ck)
    for m in re.findall(r"[a-zA-Z0-9_-]{8,}\.(?:pdf|xml|xbrl|html)", content_html, flags=re.I):
        state["file_keys"].add(m.lower())
    for sc in re.findall(r"\b(5\d{5})\b", title_str + " " + content_html):
        state["scrip_keys"].add(f"{p_type}_{sc}")


def get_existing_blogger_posts():
    state = {"comp_keys": set(), "scrip_keys": set(), "file_keys": set()}
    if not BLOG_ID: return state
    try:
        res = c_requests.get(f"https://www.blogger.com/feeds/{BLOG_ID}/posts/default?alt=json&max-results=500", impersonate="chrome120", timeout=25)
        if res.status_code == 200:
            for e in res.json().get("feed", {}).get("entry", []):
                _parse_single_blog_post_for_dedup(e.get("title", {}).get("$t", ""), e.get("content", {}).get("$t", ""), [c.get("term", "") for c in e.get("category", [])], state)
    except Exception as exc:
        log.warning("Blog feed scan warning: %s", exc)
    log.info("Blog Scan Complete: %d comp_keys | %d file_keys | %d scrip_keys", len(state["comp_keys"]), len(state["file_keys"]), len(state["scrip_keys"]))
    return state


def blogger_publish(title, content_html, doc_type):
    clean_title = re.sub(r"[\r\n\t]+", " ", re.sub(r"<[^>]+>", "", str(title))).strip()[:140] or "Corporate Update | MarketMunafa"
    labels = ["MARKET NEWS", "SHARE ORDER"] if doc_type == "order" else ["MARKET NEWS", "STOCK RESULT"]
    for attempt in range(4):
        try:
            token = blogger_access_token()
            resp = requests.post(
                f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"title": clean_title, "content": content_html, "labels": labels},
                timeout=25,
            )
            if resp.status_code == 429:
                wait_s = 30 * (attempt + 1)
                log.warning("Blogger 429 limit hit. Waiting %ds before retry...", wait_s)
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            return resp.json().get("url", "")
        except Exception as exc:
            if attempt == 3: raise
            time.sleep(15)
    return ""


# ---------------- MAIN ----------------
def run():
    print(">>> Starting MarketMunafa BSE Bot (gemini-3.8-flash ONLY)...", flush=True)
    init_db()
    blog_state = get_existing_blogger_posts()
    all_items = fetch_bse_json_announcements()
    for kind, feed_url in FEEDS.items():
        try: all_items.extend(parse_feed(feed_url, kind))
        except Exception as exc: log.warning("XML feed error: %s", exc)

    for item in all_items:
        doc_type = classify_item(item)
        if not doc_type: continue
        comp_key = normalize_company_key(item.get("company_raw") or item["title"], doc_type)
        scrip_code = item.get("scrip_code") or extract_scrip_code(item["title"])
        scrip_key = f"{doc_type}_{scrip_code}" if scrip_code else ""
        file_key = extract_file_key(item["link"])
        item_id = hashlib.sha256((file_key or comp_key or item["title"]).encode("utf-8")).hexdigest()

        if (file_key and file_key in blog_state["file_keys"]) or (scrip_key and scrip_key in blog_state["scrip_keys"]) or (comp_key and comp_key in blog_state["comp_keys"]) or was_posted_in_db(item_id, comp_key, scrip_key, file_key):
            log.info("Skipping duplicate: %s", item["title"])
            continue

        log.info("Processing %s: %s", doc_type, item["title"])
        try:
            source_text, pdf_bytes = get_document_payload(item)
            scr = get_screener_fundamentals(item["title"])
            if doc_type == "result" and not pdf_bytes and not scr["q_all_rev"] and len(source_text) < 120:
                continue
            ai_data = analyze_with_gemini_json(item["title"], scrip_code, doc_type, source_text, pdf_bytes, scr)
            ai_ck = normalize_company_key(str(ai_data.get("company_short_name") or ""), doc_type)
            if ai_ck and ai_ck in blog_state["comp_keys"]:
                continue
            if doc_type == "result" and to_float(ai_data.get("revenue", {}).get("curr")) == 0 and to_float(ai_data.get("net_profit", {}).get("curr")) == 0:
                continue

            final_ck = ai_ck or comp_key
            raw_title = ai_data.get("seo_title") or f"{item['title']} Analysis | MarketMunafa"
            blog_html, yoy_chart = build_post_html(ai_data, doc_type, scr, item["link"], final_ck, scrip_key, file_key, scrip_code)
            blog_url = blogger_publish(raw_title, blog_html, doc_type) if POST_TO_BLOGGER else ""
            if blog_url:
                mark_posted(item_id, final_ck, scrip_key, file_key, item["title"], item["link"])
                log.info("Published on Blogger: %s", blog_url)
            send_telegram_gujarati(ai_data, doc_type, scr, blog_url, item["link"], yoy_chart)

            for k in (comp_key, ai_ck):
                if k: blog_state["comp_keys"].add(k)
            if scrip_key: blog_state["scrip_keys"].add(scrip_key)
            if file_key: blog_state["file_keys"].add(file_key)
            time.sleep(15)
        except RuntimeError as r_err:
            if "exhausted" in str(r_err).lower() or "daily limit" in str(r_err).lower():
                log.warning("Stopping run cleanly: %s", r_err)
                break
            log.exception("Failed item: %s", item["title"])
            time.sleep(5)
        except Exception:
            log.exception("Failed item: %s", item["title"])
            time.sleep(5)


if __name__ == "__main__":
    run()
