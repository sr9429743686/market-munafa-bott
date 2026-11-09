import os
import time
import random
import requests
import PyPDF2
import google.generativeai as genai
from datetime import datetime, timedelta
from curl_cffi import requests as c_requests
from fake_useragent import UserAgent

# --- GitHub Secrets માંથી લેવાતી Credentials ---
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- તમારી વિગતો ---
BLOG_ID = "6996150531679127159"
TELEGRAM_BOT_TOKEN = "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM"
TELEGRAM_CHAT_ID = "430834857"

# Gemini AI સેટઅપ
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    print("❌ Gemini API Key મળતી નથી!")

def get_ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def get_random_impersonate():
    """BSE ને છેતરવા માટે અલગ-અલગ બ્રાઉઝર પ્રોફાઇલ"""
    profiles = ["chrome120", "chrome110", "edge101", "safari15_3"]
    return random.choice(profiles)

def get_random_headers():
    """અસલી માણસ જેવો નવો User-Agent બનાવશે"""
    ua = UserAgent()
    return {
        'User-Agent': ua.random,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.google.com/',
        'Connection': 'keep-alive'
    }

def get_latest_bse_pdfs():
    """BSE પરથી લેટેસ્ટ કોર્પોરેટ જાહેરાતો ખેંચીને PDF લિંક્સ લાવશે (Stealth Mode)"""
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C"
    
    headers = get_random_headers()
    browser_profile = get_random_impersonate()
    
    pdf_list = []
    try:
        # બોટ જેવું ન લાગે એટલે 1 થી 3 સેકન્ડનો રેન્ડમ ડીલે
        time.sleep(random.uniform(1.0, 3.0)) 
        
        res = c_requests.get(url, headers=headers, impersonate=browser_profile, timeout=15)
        
        if res.status_code == 200:
            data = res.json()
            if 'Table' in data:
                # શરૂઆતની ટોપ 3 નવી જાહેરાતો ચેક કરશે
                for item in data['Table'][:3]: 
                    comp_name = item.get('SLONGNAME', '')
                    heading = item.get('NEWSSUB', '')
                    pdf_link = item.get('ATTACHMENTNAME', '')
                    
                    if comp_name and pdf_link and ('result' in heading.lower() or 'financial' in heading.lower()):
                        full_pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}"
                        pdf_list.append({
                            'company': comp_name,
                            'heading': heading,
                            'pdf_url': full_pdf_url
                        })
    except Exception as e:
        print(f"❌ BSE API Error: {e}")
        
    return pdf_list

def extract_text_from_pdf_url(pdf_url):
    """PDF ડાઉનલોડ કરીને તેમાંથી ફક્ત મુખ્ય આંકડા વાંચશે"""
    headers = get_random_headers()
    try:
        response = requests.get(pdf_url, headers=headers, timeout=20)
        if response.status_code == 200:
            with open("temp.pdf", "wb") as f:
                f.write(response.content)
            
            text = ""
            with open("temp.pdf", "rb") as f:
                reader = PyPDF2.PdfReader(f)
                # શરૂઆતના 3 પેજ જ વાંચશે જેથી સમય અને ટોકન બચે
                for page in reader.pages[:3]: 
                    text += page.extract_text() + "\n"
            return text
    except Exception as e:
        print(f"❌ PDF Extraction Error: {e}")
    return ""

def analyze_with_gemini(company_name, pdf_text):
    """Gemini AI દ્વારા PDF નું સીધું ગુજરાતી એનાલિસિસ"""
    if not GEMINI_API_KEY: return ""
    
    prompt = f"""
    You are an expert Indian stock market analyst writing for the 'MarketMunafa' blog.
    Analyze the following financial result PDF text for the company {company_name}.
    
    Extract the following details comparing this quarter (Q2/Current) with the previous year's same quarter (YoY):
    1. Total Revenue / Income growth
    2. Net Profit (PAT) growth
    3. EBITDA or Operating Profit growth
    4. Any major dividend or order announcement mentioned.
    
    Write the output ONLY in Gujarati language.
    Format the output exactly like this:
    **આવક (Revenue):** [amount] ([growth %])
    **ચોખ્ખો નફો (Net Profit):** [amount] ([growth %])
    **EBITDA:** [amount] ([growth %])
    
    **નિષ્કર્ષ (Summary):** [2 lines explaining if the result is positive, negative, or neutral for the stock price in simple Gujarati].
    
    Do not include any English text or extra markdown code blocks.
    
    PDF Text:
    {pdf_text[:4000]}
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Gemini API Error: {e}")
        return ""

def post_to_blogger(title, html_content):
    """Google Cloud OAuth2 થી બ્લોગર પર પોસ્ટ પબ્લિશ કરશે"""
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        print("❌ Blogger API Keys ખૂટે છે!")
        return
        
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": CLIENT_ID, 
        "client_secret": CLIENT_SECRET, 
        "refresh_token": REFRESH_TOKEN, 
        "grant_type": "refresh_token"
    }
    try:
        res = requests.post(token_url, data=payload, timeout=15)
        if res.status_code == 200:
            access_token = res.json().get("access_token")
            api_url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/"
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            post_data = {"title": title, "content": html_content, "labels": ["STOCK RESULT", "BSE PDF ANALYSIS"]}
            
            post_res = requests.post(api_url, json=post_data, headers=headers, timeout=15)
            if post_res.status_code in [200, 201]:
                print("✅ Blogger પર પોસ્ટ સફળતાપૂર્વક પબ્લિશ થઈ ગઈ!")
            else:
                print(f"❌ Blogger Error: {post_res.text}")
        else:
            print(f"❌ Token Refresh Error: {res.text}")
    except Exception as e: 
        print(f"❌ Blogger API Exception: {e}")

def send_telegram(text_msg):
    """ટેલિગ્રામ ચેનલ પર મેસેજ મોકલશે"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text_msg, "parse_mode": "Markdown"}
    try: 
        requests.post(url, data=payload, timeout=15)
    except: 
        pass

def run_pdf_bot():
    ist_now_str = get_ist_now().strftime("%d %b %Y")
    pdfs = get_latest_bse_pdfs()
    
    if not pdfs:
        print(f"[{ist_now_str}] ⚪ કોઈ નવી ફાઈનાન્સિયલ રિઝલ્ટ PDF મળી નથી.")
        return
        
    for item in pdfs:
        print(f"Processing PDF for: {item['company']}")
        pdf_text = extract_text_from_pdf_url(item['pdf_url'])
        
        if pdf_text and len(pdf_text) > 200:
            ai_summary_gu = analyze_with_gemini(item['company'], pdf_text)
            
            if ai_summary_gu:
                post_title = f"{item['company']} Q2 Financial Results Analysis ({ist_now_str}) | MarketMunafa"
                
                # --- Telegram Message ---
                t_msg = f"📊 *{item['company']} Q2 રિઝલ્ટ (BSE PDF અપડેટ)*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                t_msg += ai_summary_gu
                t_msg += "\n\n✅ *MarketMunafa Analysis*"
                send_telegram(t_msg)
                
                # --- Blogger HTML ---
                b_html = f"""
                <div class="market-update-post">
                    <h3>{item['company']} Q2 Financial Results</h3>
                    <p><i>Official BSE PDF Analysis by MarketMunafa AI</i></p>
                    <div style="background-color: #f8f9fa; padding: 15px; border-left: 4px solid #10b981; margin-bottom: 20px;">
                        {ai_summary_gu.replace('**', '<b>').replace('\n', '<br>')}
                    </div>
                    <p><a href="{item['pdf_url']}" target="_blank" rel="nofollow noopener">Download Original BSE PDF Here</a></p>
                    <hr>
                    <p><small>Disclaimer: AI generated analysis. Please verify with official documents before investing.</small></p>
                </div>
                """
                post_to_blogger(post_title, b_html)
                
                # એક સાથે વધુ પડતી રિક્વેસ્ટ ન જાય એટલે 5 સેકન્ડનો વિરામ
                time.sleep(5) 

if __name__ == "__main__":
    run_pdf_bot()
