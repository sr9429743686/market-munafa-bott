import os
import time
import random
import requests
import PyPDF2
import xml.etree.ElementTree as ET
import google.generativeai as genai
from datetime import datetime, timedelta
from curl_cffi import requests as c_requests
from fake_useragent import UserAgent

# --- GitHub Secrets ---
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- તમારી વિગતો ---
BLOG_ID = "6996150531679127159"
TELEGRAM_BOT_TOKEN = "9086072964:AAEUyn-GAyJLdsYR4YS5l6Xy6HJsOOoA8HM"
TELEGRAM_CHAT_ID = "430834857"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

def get_ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def get_random_impersonate():
    """ક્રૉમ, સફારી, એજ અને ફાયરફોક્સ જેવી બધી પ્રોફાઇલ રેન્ડમ પસંદ કરશે"""
    profiles = ["chrome120", "chrome110", "edge101", "safari15_3", "safari14", "chrome104"]
    return random.choice(profiles)

def get_random_headers():
    ua = UserAgent()
    return {
        'User-Agent': ua.random,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
        'Referer': 'https://www.bseindia.com/corporates/ann.aspx',
        'Origin': 'https://www.bseindia.com',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site'
    }

def get_latest_bse_announcements():
    """બધા જ બ્રાઉઝર્સના એટેક સાથે BSE ની તમામ લેટેસ્ટ જાહેરાતો ખેંચશે"""
    
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C"
    
    announcement_list = []
    max_retries = 3
    
    for attempt in range(max_retries):
        headers = get_random_headers()
        browser_profile = get_random_impersonate()
        
        try:
            time.sleep(random.uniform(2.0, 4.0)) 
            res = c_requests.get(url, headers=headers, impersonate=browser_profile, timeout=25)
            
            if res.status_code == 200:
                try:
                    data = res.json()
                except:
                    continue

                if 'Table' in data:
                    print(f"✅ સફળતાપૂર્વક કનેક્ટ થયું ({browser_profile} દ્વારા)")
                    # ટોપ 25 લેટેસ્ટ એનાઉન્સમેન્ટ ચેક કરશે
                    for item in data['Table'][:25]: 
                        comp_name = item.get('SLONGNAME', '')
                        heading = item.get('NEWSSUB', '')
                        pdf_link = item.get('ATTACHMENTNAME', '')
                        xbrl_link = item.get('XMLFILE', '')
                        
                        heading_lower = heading.lower()
                        
                        is_result = any(k in heading_lower for k in ['result', 'financial'])
                        is_order = any(k in heading_lower for k in ['order', 'award', 'contract', 'receipt'])
                        
                        if comp_name and (is_result or is_order):
                            doc_type = "result" if is_result else "order"
                            announcement_list.append({
                                'company': comp_name,
                                'heading': heading,
                                'pdf_url': f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}" if pdf_link else "",
                                'xbrl_url': f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{xbrl_link}" if xbrl_link else "",
                                'type': doc_type
                            })
                    break 
        except Exception as e:
            print(f"⚠️ પ્રયાસ {attempt+1} નિષ્ફળ: {e}")
            time.sleep(4)
            
    return announcement_list

def extract_text_from_xbrl(xbrl_url):
    headers = get_random_headers()
    try:
        response = requests.get(xbrl_url, headers=headers, timeout=20)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            extracted_text = ""
            for elem in root.iter():
                if elem.text and len(elem.text.strip()) > 2:
                    extracted_text += elem.text.strip() + " "
            return extracted_text[:5000]
    except:
        pass
    return ""

def extract_text_from_pdf_url(pdf_url):
    headers = get_random_headers()
    try:
        response = requests.get(pdf_url, headers=headers, timeout=20)
        if response.status_code == 200:
            with open("temp.pdf", "wb") as f:
                f.write(response.content)
            text = ""
            with open("temp.pdf", "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages[:3]: 
                    text += page.extract_text() + "\n"
            return text
    except:
        pass
    return ""

def analyze_with_gemini(company_name, raw_text, doc_type):
    if not GEMINI_API_KEY: return ""
    
    if doc_type == "result":
        prompt = f"""
        You are an expert Indian stock market analyst for 'MarketMunafa'.
        Analyze this BSE financial result data for {company_name}.
        
        Output EXACTLY in this Gujarati format:
        **કંપની (Company):** {company_name}
        **આવક અને નફો (Revenue & Profit):** [2 lines about revenue/profit numbers in Gujarati]
        **નિષ્કર્ષ (Summary):** [1 line about stock/market impact]
        
        Data Text:
        {raw_text[:4000]}
        """
    else:
        prompt = f"""
        You are an expert Indian stock market analyst for 'MarketMunafa'.
        Analyze this BSE order win data for {company_name}.
        
        Output EXACTLY in this Gujarati format:
        **કંપની (Company):** {company_name}
        **ઓર્ડર વિગતો (Order Details):** [2 lines about the order value and client in Gujarati]
        **નિષ્કર્ષ (Summary):** [1 line about stock/market impact]
        
        Data Text:
        {raw_text[:4000]}
        """
        
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini Error: {e}")
        return ""

def post_to_blogger(title, html_content):
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]): return
    token_url = "https://oauth2.googleapis.com/token"
    payload = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"}
    try:
        res = requests.post(token_url, data=payload, timeout=15)
        if res.status_code == 200:
            access_token = res.json().get("access_token")
            api_url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/"
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            post_data = {"title": title, "content": html_content, "labels": ["BSE UPDATE", "MARKET NEWS"]}
            requests.post(api_url, json=post_data, headers=headers, timeout=15)
    except: pass

def send_telegram(text_msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text_msg, "parse_mode": "Markdown"}
    try: requests.post(url, data=payload, timeout=15)
    except: pass

def run_bot():
    ist_now_str = get_ist_now().strftime("%d %b %Y")
    items = get_latest_bse_announcements()
    
    if not items:
        print(f"[{ist_now_str}] ⚪ કોઈ નવી રિઝલ્ટ કે ઓર્ડરની ફાઇલ મળી નથી.")
        return
        
    for item in items:
        print(f"Processing: {item['company']} ({item['type'].upper()})")
        raw_text = ""
        source_url = ""
        
        if item['xbrl_url']:
            raw_text = extract_text_from_xbrl(item['xbrl_url'])
            source_url = item['xbrl_url']
            
        if not raw_text and item['pdf_url']:
            raw_text = extract_text_from_pdf_url(item['pdf_url'])
            source_url = item['pdf_url']
            
        if raw_text and len(raw_text) > 100:
            ai_summary_gu = analyze_with_gemini(item['company'], raw_text, item['type'])
            
            if ai_summary_gu:
                tag = "New Order Win" if item['type'] == "order" else "Q2 Financial Results"
                post_title = f"{item['company']} {tag} ({ist_now_str}) | MarketMunafa"
                
                t_msg = f"📊 *{item['company']} {tag}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                t_msg += ai_summary_gu
                t_msg += "\n\n✅ *MarketMunafa Bot*"
                send_telegram(t_msg)
                
                formatted_summary = ai_summary_gu.replace('**', '<b>').replace('\n', '<br>')
                
                b_html = f"""
                <div class="market-update-post">
                    <h3>{item['company']} - {tag}</h3>
                    <p><i>Official BSE Update via MarketMunafa AI</i></p>
                    <div style="background-color: #f8f9fa; padding: 15px; border-left: 4px solid #10b981; margin-bottom: 20px;">
                        {formatted_summary}
                    </div>
                    <p><a href="{source_url}" target="_blank" rel="nofollow noopener">View Original BSE Filing Here</a></p>
                    <hr>
                </div>
                """
                post_to_blogger(post_title, b_html)
                time.sleep(5)

if __name__ == "__main__":
    run_bot()
