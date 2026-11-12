import os
import time
import re
import json
import html
import hashlib
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from curl_cffi import requests as c_requests
from deep_translator import GoogleTranslator

# --- Blogger API Credentials ---
CLIENT_ID = (os.environ.get("CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.environ.get("SECRET_ID") or os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
REFRESH_TOKEN = (os.environ.get("REFRESH_TOKEN") or os.environ.get("GOOGLE_REFRESH_TOKEN") or "").strip()
BLOG_ID = os.environ.get("BLOG_ID", "6996150531679127159").strip()
BLOG_BASE_URL = os.environ.get("BLOG_BASE_URL", "https://marketmunafa.blogspot.com").rstrip("/")

# --- Telegram Credentials ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "430834857").strip()

# --- Official BSE Forthcoming Results Link ---
BSE_FORTH_RESULTS_URL = "https://www.bseindia.com/corporates/forth_results"

# ---------------- ફક્ત 2 નવી GEMINI API KEYS (દરેક કી પર 1 મિનિટની 5 અને દિવસની 20 રિક્વેસ્ટ) ----------------
GEMINI_API_KEYS = [
    "AQ.Ab8RN6" + "JWzU70V3RM1zMEx0VEYmUj4q" + "EvvqqKbH27t1oHIO3Xjg",
    "AQ.Ab8RN6" + "Iw_vLVx2SPdac2gbUhK5fiSy" + "ihpDMHASZdZzdd4SnFGQ",
]

for _ek in re.split(r"[,\s]+", os.getenv("GEMINI_API_KEY", "").strip()):
    if _ek and _ek not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_ek)

GEMINI_MODEL = "gemini-3.8-flash"
MAX_REQ_PER_KEY_PER_MINUTE = 5
MAX_REQ_PER_KEY_PER_DAY = 20
RATE_WINDOW_SECONDS = 60.0

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, application/xml, text/xml, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


def get_ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def clean_md(text):
    if not text:
        return ""
    return str(text).replace("*", "").replace("_", "").replace("`", "").replace("[", "(").replace("]", ")").strip()


def translate_text(text, src_lang="en", dest_lang="gu"):
    if not text or not str(text).strip():
        return ""
    clean_t = str(text).strip()
    try:
        url = (
            f"https://translate.googleapis.com/translate_a/single?client=gtx"
            f"&sl={src_lang}&tl={dest_lang}&dt=t&q={urllib.parse.quote(clean_t)}"
        )
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            translated = "".join([part[0] for part in data[0] if part[0]])
            if translated:
                return clean_md(translated)
    except Exception:
        pass
    try:
        return clean_md(GoogleTranslator(source=src_lang, target=dest_lang).translate(clean_t))
    except Exception:
        return clean_md(clean_t)


def translate_to_gu(text):
    return translate_text(text, src_lang="en", dest_lang="gu")


def make_copyright_free_en(raw_text, company_name=""):
    if not raw_text:
        return ""
    cleaned = re.split(r"\s+(?:-|\||–|—)\s+(?=[A-Z])", str(raw_text))[0].strip()
    gu_ver = translate_text(cleaned, src_lang="en", dest_lang="gu")
    re_en = translate_text(gu_ver, src_lang="gu", dest_lang="en") if gu_ver else cleaned
    if not re_en or len(re_en) < 12:
        re_en = cleaned
    if company_name and company_name.lower() not in re_en.lower():
        re_en = f"{company_name} is witnessing active market interest as {re_en[0].lower() + re_en[1:]}"
    return re_en.strip()


def to_float_price(val_str):
    try:
        cleaned = re.sub(r"[^\d.-]", "", str(val_str))
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


def calculate_intraday_levels(price_val):
    p = to_float_price(price_val)
    if p <= 0:
        return {"support": "N/A", "resistance": "N/A", "target": "N/A"}

    support_s1 = round(p * 0.985, 2)
    resistance_r1 = round(p * 1.012, 2)
    target_r2 = round(p * 1.026, 2)

    return {
        "support": f"₹{support_s1:,.2f}",
        "resistance": f"₹{resistance_r1:,.2f}",
        "target": f"₹{target_r2:,.2f}",
    }


# ---------------- MULTI-KEY ROTATOR ----------------
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
                    return idx, self.keys[idx]
                else:
                    w = (self.usage_history[idx][0] + self.window_sec) - now + 1.0
                    earliest_wait = w if earliest_wait is None else min(earliest_wait, w)

            if len(self.exhausted_today) >= num_keys:
                raise RuntimeError("All API keys have reached their daily limit!")

            sleep_s = max(3.0, min(earliest_wait or 10.0, 62.0))
            time.sleep(sleep_s)

    def mark_key_rate_limited(self, idx, err_msg="", cooldown_sec=65.0):
        e_low = str(err_msg).lower()
        if any(w in e_low for w in ("depleted", "prepayment", "daily", "per_day", "invalid", "api_key_invalid")):
            self.exhausted_today.add(idx)
        else:
            self.cooldown_until[idx] = time.time() + cooldown_sec


KEY_ROTATOR = GeminiMultiKeyRotator(
    GEMINI_API_KEYS,
    max_per_min=MAX_REQ_PER_KEY_PER_MINUTE,
    max_per_day=MAX_REQ_PER_KEY_PER_DAY,
    window_sec=RATE_WINDOW_SECONDS,
)


# ---------------- 0. LIVE NIFTY 50 & BANK NIFTY PULSE ----------------
def get_index_morning_pulse():
    indices = [("Nifty 50", "^NSEI"), ("Bank Nifty", "^NSEBANK")]
    pulse_list = []
    for name, symbol in indices:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=5m&range=2d"
            r = c_requests.get(url, impersonate="chrome120", timeout=10)
            if r.status_code == 200:
                meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                curr = float(meta.get("regularMarketPrice") or 0.0)
                prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0)
                if curr > 0 and prev > 0:
                    diff = curr - prev
                    pct = (diff / prev) * 100.0
                    pulse_list.append({
                        "name": name,
                        "level": f"{curr:,.1f}",
                        "change": f"{diff:+.1f} ({pct:+.2f}%)",
                        "support": f"{round(curr * 0.993, 0):,.0f}",
                        "resistance": f"{round(curr * 1.007, 0):,.0f}",
                    })
        except Exception:
            continue
    return pulse_list


# ---------------- 1. LARGE CAP & MID CAP STOCK FETCHER ----------------
LARGE_CAP_WATCHLIST = [
    ("Reliance Industries", "RELIANCE.NS"), ("TCS", "TCS.NS"), ("HDFC Bank", "HDFCBANK.NS"),
    ("ICICI Bank", "ICICIBANK.NS"), ("Infosys", "INFY.NS"), ("State Bank of India", "SBIN.NS"),
    ("Tata Motors", "TATAMOTORS.NS"), ("Larsen & Toubro", "LT.NS"), ("Axis Bank", "AXISBANK.NS"),
    ("Bajaj Finance", "BAJFINANCE.NS"), ("Sun Pharma", "SUNPHARMA.NS"), ("Mahindra & Mahindra", "M%26M.NS"),
    ("NTPC", "NTPC.NS"), ("Tata Steel", "TATASTEEL.NS"), ("Bharat Electronics", "BEL.NS"),
    ("Hindustan Aeronautics", "HAL.NS"), ("ITC", "ITC.NS"), ("Bharti Airtel", "BHARTIARTL.NS"),
]

MID_CAP_WATCHLIST = [
    ("RVNL", "RVNL.NS"), ("IRCTC", "IRCTC.NS"), ("Dixon Technologies", "DIXON.NS"),
    ("Polycab India", "POLYCAB.NS"), ("Persistent Systems", "PERSISTENT.NS"), ("Suzlon Energy", "SUZLON.NS"),
    ("IREDA", "IREDA.NS"), ("Mazagon Dock", "MAZDOCK.NS"), ("Cochin Shipyard", "COCHINSHIP.NS"),
    ("KPIT Technologies", "KPITTECH.NS"), ("Federal Bank", "FEDERALBNK.NS"), ("IDFC First Bank", "IDFCFIRSTB.NS"),
    ("Ashok Leyland", "ASHOKLEY.NS"), ("Voltas", "VOLTAS.NS"), ("BHEL", "BHEL.NS"),
    ("SAIL", "SAIL.NS"), ("NMDC", "NMDC.NS"), ("Lupin", "LUPIN.NS"),
]


def fetch_yf_category_movers(watchlist, max_count=5):
    movers = []
    for comp_name, ticker in watchlist:
        try:
            yf_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=2d"
            r = c_requests.get(yf_url, impersonate="chrome120", timeout=7)
            if r.status_code == 200:
                meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                curr_p = float(meta.get("regularMarketPrice") or 0.0)
                prev_c = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0)
                if curr_p > 0 and prev_c > 0:
                    diff = curr_p - prev_c
                    pct = (diff / prev_c) * 100.0
                    movers.append({
                        "name": comp_name,
                        "price_num": curr_p,
                        "price": f"₹{curr_p:,.2f}",
                        "change": f"{diff:+.2f}",
                        "pChange": f"{pct:.2f}",
                        "pct_num": pct,
                        "levels": calculate_intraday_levels(curr_p),
                    })
        except Exception:
            continue

    movers.sort(key=lambda x: x["pct_num"], reverse=True)
    pos = [m for m in movers if m["pct_num"] > 0]
    return (pos + [m for m in movers if m["pct_num"] <= 0])[:max_count]


def get_categorized_intraday_stocks():
    """માત્ર લાર્જ કેપ અને મિડ કેપ શેર્સનું જ એનાલિસિસ કરશે."""
    return {
        "large_cap": fetch_yf_category_movers(LARGE_CAP_WATCHLIST, max_count=5),
        "mid_cap": fetch_yf_category_movers(MID_CAP_WATCHLIST, max_count=5),
    }


def generate_intraday_movers_chart(categories, today_str):
    combined = categories.get("large_cap", [])[:3] + categories.get("mid_cap", [])[:3]
    if not combined:
        return ""
    labels = [s["name"][:14] for s in combined]
    data_vals = [round(to_float_price(s["pChange"]), 2) for s in combined]
    colors = ["#16a34a" if v >= 0 else "#dc2626" for v in data_vals]

    chart_cfg = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{"label": "Intraday % Move", "backgroundColor": colors, "data": data_vals}],
        },
        "options": {
            "layout": {"padding": {"top": 26, "right": 14, "left": 10, "bottom": 8}},
            "title": {
                "display": True,
                "text": f"Top Large & Mid Cap Intraday Movers (%) - {today_str}",
                "fontSize": 15,
                "fontColor": "#0f172a",
                "padding": 14,
            },
            "legend": {"display": False},
            "scales": {"yAxes": [{"ticks": {"beginAtZero": True}}]},
            "plugins": {
                "datalabels": {
                    "display": True,
                    "anchor": "end",
                    "align": "top",
                    "color": "#0f172a",
                    "font": {"weight": "bold", "size": 11},
                }
            },
        },
    }
    return f"https://quickchart.io/chart.png?w=720&h=380&bkg=white&c={urllib.parse.quote(json.dumps(chart_cfg))}"


# ---------------- 2. CORPORATE ANNOUNCEMENTS ----------------
def fetch_google_news_rss(query, max_items=4):
    rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    items_found = []
    try:
        res = c_requests.get(rss_url, impersonate="chrome120", timeout=12)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for item in root.findall(".//item")[:max_items]:
                t_el = item.find("title")
                if t_el is not None and t_el.text:
                    items_found.append(t_el.text.strip())
    except Exception:
        pass
    return items_found


def get_corporate_announcements():
    announcements = []
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y%m%d")
    prev_str = (ist_now - timedelta(days=1)).strftime("%Y%m%d")

    url = (
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={prev_str}&strScrip=&strSearch=P&strToDate={today_str}&strType=C&subcategory=-1"
    )
    try:
        res = c_requests.get(url, headers=BSE_HEADERS, impersonate="chrome120", timeout=12)
        if res.status_code == 200:
            data = res.json()
            for item in data.get("Table", [])[:8]:
                comp_name = str(item.get("SLONGNAME") or "").strip()
                heading = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
                if comp_name and heading and "nav as on" not in heading.lower():
                    cf_heading = make_copyright_free_en(heading, comp_name)
                    announcements.append({
                        "company": clean_md(comp_name),
                        "heading": cf_heading,
                        "heading_gu": translate_to_gu(heading),
                    })
                    if len(announcements) >= 4:
                        break
    except Exception:
        pass
    return announcements


# ---------------- 3. UPCOMING RESULTS ----------------
def get_today_results_and_anticipated_stocks():
    ist_now = get_ist_now()
    today_ymd = ist_now.strftime("%Y%m%d")
    today_formats = [
        ist_now.strftime("%d %b %Y").lower(),
        f"{ist_now.day} {ist_now.strftime('%b %Y')}".lower(),
        ist_now.strftime("%d-%b-%Y").lower(),
        ist_now.strftime("%d-%b-%y").lower(),
        ist_now.strftime("%d/%m/%Y").lower(),
        ist_now.strftime("%Y-%m-%d").lower(),
    ]
    upcoming_today = []
    try:
        bse_api = f"https://api.bseindia.com/BseIndiaAPI/api/ForthResults/w?scripcode=&fromdate={today_ymd}&todate={today_ymd}"
        res = c_requests.get(bse_api, headers=BSE_HEADERS, impersonate="chrome120", timeout=12)
        if res.status_code == 200:
            data = res.json()
            rows = data if isinstance(data, list) else (data.get("Table", []) if isinstance(data, dict) else [])
            for item in rows:
                if not isinstance(item, dict):
                    continue
                c_name = str(item.get("short_name") or item.get("Long_Name") or item.get("CompanyName") or "").strip()
                if c_name and c_name not in upcoming_today:
                    upcoming_today.append(clean_md(c_name))
    except Exception:
        pass

    return upcoming_today, []


# ---------------- 4. BLOGGER & TELEGRAM PUBLISHING ----------------
def post_to_blogger_api(title, html_content):
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        return
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    for attempt in range(3):
        try:
            res = requests.post(token_url, data=payload, timeout=15)
            if res.status_code == 200:
                access_token = res.json().get("access_token")
                api_url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/"
                headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                post_data = {
                    "title": title[:140],
                    "content": html_content,
                    "labels": ["INTRADAY", "MARKET NEWS", "STOCK RESULT"],
                }
                post_res = requests.post(api_url, json=post_data, headers=headers, timeout=20)
                if post_res.status_code in [200, 201]:
                    return
                elif post_res.status_code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
        except Exception:
            time.sleep(10)


def send_telegram_message(text_msg, chart_url=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if chart_url:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": chart_url,
                    "caption": "📊 લાર્જ કેપ અને મિડ કેપ શેર્સનો ઇન્ટ્રાડે ચાર્ટ",
                },
                timeout=15,
            )
    except Exception:
        pass

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text_msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception:
        pass


def build_category_table_html(cat_title, cat_desc, stocks_list, badge_color="#1e3a8a"):
    if not stocks_list:
        return ""
    rows = ""
    for s in stocks_list:
        sign = "+" if not str(s["pChange"]).startswith("-") else ""
        col = "#dc2626" if str(s["pChange"]).startswith("-") else "#16a34a"
        lv = s["levels"]
        rows += f"""
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 10px 6px; font-weight: 700; color: #0f172a;">{html.escape(s['name'])}</td>
            <td style="padding: 10px 6px; text-align: right; font-weight: 600;">{html.escape(s['price'])}</td>
            <td style="padding: 10px 6px; text-align: right; font-weight: 700; color: {col};">{sign}{html.escape(s['pChange'])}%</td>
            <td style="padding: 10px 6px; text-align: right; color: #dc2626;">{html.escape(lv['support'])}</td>
            <td style="padding: 10px 6px; text-align: right; color: #1d4ed8; font-weight: 600;">{html.escape(lv['resistance'])}</td>
            <td style="padding: 10px 6px; text-align: right; color: #15803d; font-weight: 700;">{html.escape(lv['target'])}</td>
        </tr>
        """
    return f"""
    <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 14px; padding: 16px; margin-bottom: 22px; overflow-x: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.03);">
        <h3 style="margin: 0 0 6px 0; font-size: 18px; color: {badge_color};">{cat_title}</h3>
        <p style="margin: 0 0 12px 0; font-size: 14px; color: #475569;">{cat_desc}</p>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 2px solid #e2e8f0; color: #475569;">
                    <th style="padding: 9px 6px; text-align: left;">Stock Name</th>
                    <th style="padding: 9px 6px; text-align: right;">CMP</th>
                    <th style="padding: 9px 6px; text-align: right;">% Move</th>
                    <th style="padding: 9px 6px; text-align: right;">Support (S1)</th>
                    <th style="padding: 9px 6px; text-align: right;">Breakout (R1)</th>
                    <th style="padding: 9px 6px; text-align: right;">Target (R2)</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """


# ---------------- 5. MAIN RUNNER ----------------
def run_intraday_bot():
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%d %b %Y")
    iso_date = ist_now.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    index_pulse = get_index_morning_pulse()
    categorized = get_categorized_intraday_stocks()
    announcements = get_corporate_announcements()
    upcoming_today, _ = get_today_results_and_anticipated_stocks()

    top_picks_names = [s["name"].split()[0] for s in (categorized["large_cap"][:2] + categorized["mid_cap"][:2])]
    if top_picks_names:
        post_title = f"{', '.join(top_picks_names)} in Focus: Large Cap & Mid Cap Intraday Picks ({today_str})"
    else:
        post_title = f"Large Cap & Mid Cap Intraday Stocks to Watch ({today_str}): Breakouts & Pivot Levels"
    post_title = post_title[:108]

    seed_val = int(hashlib.md5((today_str + post_title).encode("utf-8")).hexdigest()[:6], 16)
    image_prompt = f"modern 3d indian stock market trading screen candlestick breakout green arrows financial analytics {today_str}"
    thumb_url = (
        f"https://image.pollinations.ai/prompt/{urllib.parse.quote(image_prompt)}"
        f"?width=1200&height=675&seed={seed_val}&nologo=true"
    )

    movers_chart_url = generate_intraday_movers_chart(categorized, today_str)

    lead_summary_en = (
        f"MarketMunafa Morning Intraday Research ({today_str}): Explore high-liquidity Large Cap and Mid Cap breakout setups "
        f"with calculated Support (S1), Resistance (R1), and Target (R2) levels, alongside live BSE corporate order announcements "
        f"and the official BSE Forthcoming Results schedule."
    )

    # Telegram Message
    t_msg = f"🔔 <b>ઇન્ટ્રાડે મોર્નિંગ માર્કેટ અપડેટ ({today_str})</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"

    if index_pulse:
        t_msg += "📊 <b>નિફ્ટી અને બેંક નિફ્ટી સપોર્ટ-રેઝિસ્ટન્સ:</b>\n"
        for idx_p in index_pulse:
            t_msg += (
                f"• <b>{html.escape(idx_p['name'])}</b>: {html.escape(idx_p['level'])} ({html.escape(idx_p['change'])})\n"
                f"  Support: <code>{html.escape(idx_p['support'])}</code> | Resistance: <code>{html.escape(idx_p['resistance'])}</code>\n"
            )
        t_msg += "\n"

    tg_sections = [
        ("🏛️ ૧. લાર્જ કેપ શેર (Large Cap Shares):", categorized.get("large_cap", [])),
        ("📈 ૨. મિડ કેપ શેર (Mid Cap Shares):", categorized.get("mid_cap", [])),
    ]

    for sec_heading, sec_stocks in tg_sections:
        if sec_stocks:
            t_msg += f"<b>{sec_heading}</b>\n"
            for s in sec_stocks[:5]:
                sign = "+" if not str(s["pChange"]).startswith("-") else ""
                lv = s["levels"]
                t_msg += (
                    f"👉 <b>{html.escape(clean_md(s['name']))}</b> | ભાવ: {html.escape(s['price'])} ({sign}{html.escape(s['pChange'])}%)\n"
                    f"   🎯 Support: <code>{html.escape(lv['support'])}</code> | Breakout: <code>{html.escape(lv['resistance'])}</code> | Target: <code>{html.escape(lv['target'])}</code>\n"
                )
            t_msg += "\n"

    if announcements:
        t_msg += "📰 <b>લેટેસ્ટ કોર્પોરેટ જાહેરાતો:</b>\n"
        for a in announcements[:3]:
            t_msg += f"🏢 <b>{html.escape(a['company'])}</b>: {html.escape(a['heading_gu'])}\n\n"

    t_msg += f"📅 <b>આજે રિઝલ્ટ જાહેર કરનારી કંપનીઓ:</b> કુલ {len(upcoming_today)} શેર્સ\n"
    t_msg += f"🔗 <a href=\"{BSE_FORTH_RESULTS_URL}\"><b>BSE રિઝલ્ટ લિસ્ટ અહીં જુઓ</b></a>\n\n"
    t_msg += "✅ <b>MarketMunafa - સ્માર્ટ મની ટ્રેડિંગ</b>"

    # Blogger HTML
    index_html = ""
    if index_pulse:
        idx_rows = ""
        for ip in index_pulse:
            idx_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 8px; font-weight: 700; color: #0f172a;">{html.escape(ip['name'])}</td>
                <td style="padding: 10px 8px; text-align: right; font-weight: 600;">{html.escape(ip['level'])}</td>
                <td style="padding: 10px 8px; text-align: right; color: #16a34a; font-weight: 600;">{html.escape(ip['change'])}</td>
                <td style="padding: 10px 8px; text-align: right; color: #dc2626; font-weight: 600;">{html.escape(ip['support'])}</td>
                <td style="padding: 10px 8px; text-align: right; color: #2563eb; font-weight: 600;">{html.escape(ip['resistance'])}</td>
            </tr>
            """
        index_html = f"""
        <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; padding: 16px; margin-bottom: 24px; overflow-x: auto;">
            <h2 style="margin: 0 0 12px 0; font-size: 19px; color: #0f172a;">📊 Nifty 50 &amp; Bank Nifty Intraday Key Levels ({today_str})</h2>
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 2px solid #e2e8f0; color: #475569;">
                        <th style="padding: 10px 8px; text-align: left;">Index</th>
                        <th style="padding: 10px 8px; text-align: right;">Spot Level</th>
                        <th style="padding: 10px 8px; text-align: right;">Change</th>
                        <th style="padding: 10px 8px; text-align: right;">Key Support (S1)</th>
                        <th style="padding: 10px 8px; text-align: right;">Resistance (R1)</th>
                    </tr>
                </thead>
                <tbody>{idx_rows}</tbody>
            </table>
        </div>
        """

    large_table = build_category_table_html("🏛️ 1. Large Cap Shares", "High-liquidity Nifty 50 leaders:", categorized.get("large_cap", []), "#0f172a")
    mid_table = build_category_table_html("📈 2. Mid Cap Shares", "Mid-cap momentum counters:", categorized.get("mid_cap", []), "#1e3a8a")

    chart_block = (
        f'<figure style="margin: 20px 0 26px 0; text-align: center;">'
        f'<img src="{movers_chart_url}" alt="Large & Mid Cap Momentum Chart {today_str}" width="720" height="380" loading="lazy" style="max-width: 100%; height: auto; border-radius: 10px; border: 1px solid #e2e8f0;" />'
        f'<figcaption style="font-size: 12px; color: #64748b; margin-top: 6px;">Large &amp; Mid Cap Momentum Leaders (%) | MarketMunafa</figcaption>'
        f'</figure>'
        if movers_chart_url else ""
    )

    segments_html = f"""
    <h2 style="font-size: 21px; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-top: 28px;">
        🚀 Large Cap &amp; Mid Cap Intraday Breakout Stocks
    </h2>
    <p style="color: #334155; font-size: 15px;">
        Our quantitative screener focuses exclusively on high-liquidity Large Cap and Mid Cap stocks with calculated <strong>Support (S1)</strong>, <strong>Breakout (R1)</strong>, and <strong>Upside Target (R2)</strong> levels:
    </p>
    {large_table}
    {mid_table}
    {chart_block}
    """

    ann_html = ""
    if announcements:
        ann_cards = ""
        for a in announcements:
            ann_cards += f"""
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px;">
                <h3 style="margin: 0 0 6px 0; font-size: 16px; color: #0f172a;">🏢 {html.escape(a['company'])}</h3>
                <p style="margin: 0; font-size: 14px; color: #475569;">{html.escape(a['heading'])}</p>
            </div>
            """
        ann_html = f"""
        <h2 style="font-size: 21px; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-top: 28px;">
            📰 Official Corporate Filings &amp; Order Wins
        </h2>
        {ann_cards}
        """

    upcoming_html = f"""
    <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 14px; padding: 18px; margin-top: 28px;">
        <h2 style="margin: 0 0 10px 0; font-size: 20px; color: #0f172a;">📅 BSE Forthcoming Results &amp; Board Meeting Calendar ({today_str})</h2>
        <p style='font-size:15px;color:#334155;margin-bottom:14px;'>
            Traders tracking quarterly earnings reactions and meeting outcomes for <strong>{today_str}</strong> can access the live company schedule directly on the official BSE portal below:
        </p>
        <p style="margin: 0;">
            <a href="{BSE_FORTH_RESULTS_URL}" target="_blank" rel="nofollow noopener" style="display: inline-block; background: #1d4ed8; color: #ffffff; font-weight: 600; font-size: 14px; padding: 10px 18px; border-radius: 8px; text-decoration: none;">
                🔗 View Live BSE Forthcoming Results Schedule ({today_str})
            </a>
        </p>
    </div>
    """

    schema_json = json.dumps([
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": post_title,
            "description": lead_summary_en,
            "image": [thumb_url],
            "datePublished": iso_date,
            "dateModified": iso_date,
            "author": [{"@type": "Organization", "name": "MarketMunafa Equity Research Desk", "url": BLOG_BASE_URL}],
            "publisher": {"@type": "Organization", "name": "MarketMunafa", "url": BLOG_BASE_URL},
        }
    ])

    b_html = f"""
    <article class="market-update-post" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; max-width: 740px; margin: 0 auto; color: #1e293b; line-height: 1.7;">
        <p style="font-size: 16px; color: #0f172a; background: #f8fafc; padding: 14px 18px; border-radius: 10px; border-left: 4px solid #16a34a; margin-bottom: 16px;">
            <strong>Executive Summary:</strong> {html.escape(lead_summary_en)}
        </p>

        <div style="font-size: 13px; color: #64748b; margin-bottom: 14px;">
            <span><strong>By MarketMunafa Equity Research Desk</strong></span> | 
            <time datetime="{iso_date}">Published: {today_str} (Morning Session IST)</time>
        </div>

        <figure style="text-align:center; margin: 0 0 22px 0;">
            <img src="{thumb_url}" alt="{html.escape(post_title)}" width="1200" height="675" fetchpriority="high" style="width:100%; height:auto; border-radius:12px; aspect-ratio: 16/9; object-fit: cover;" />
            <figcaption style="font-size: 13px; color: #64748b; margin-top: 6px;">{html.escape(post_title)} | MarketMunafa</figcaption>
        </figure>
        <!--more-->

        {index_html}
        {segments_html}
        {ann_html}
        {upcoming_html}

        <script type="application/ld+json">{schema_json}</script>

        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
        <p style="font-size: 12px; color: #64748b; line-height: 1.6;">
            <strong>Disclaimer:</strong> Educational purposes only. We are not SEBI-registered investment advisors.
        </p>
    </article>
    """

    send_telegram_message(t_msg, chart_url=movers_chart_url)
    post_to_blogger_api(post_title, b_html)


if __name__ == "__main__":
    run_intraday_bot()
