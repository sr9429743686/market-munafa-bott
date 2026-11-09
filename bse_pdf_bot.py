import os
import time
import random
import requests
import PyPDF2
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
    return random.choice(["chrome120", "chrome110", "edge101", "safari15_3"])

def get_random_headers():
    ua = UserAgent()
    return {
        'User-Agent': ua.random,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.bseindia.com/',
        'Origin': 'https://www.bseindia.com',
        'Connection': 'keep-alive'
    }

def get_latest_bse_pdfs():
    """BSE પરથી રિઝલ્ટ અને ઓર્ડરની કેટેગરી મુજબ ડેટા ખેંચશે"""
    
    # તમે ફોટામાં બતાવેલી કેટેગરી મુજબની બે અલગ લિંક્સ
    urls = {
        "result": "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=Result&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C",
        "order": "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=Company%20Update&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C"
    }
    
    headers = get_random_headers()
    browser_profile = get_random_impersonate()
    pdf_list = []
    
    for req_type, url in urls.items():
        try:
            time.sleep(random.uniform(1.0, 3.0)) 
            res = c_requests.get(url, headers=headers, impersonate=browser_profile, timeout=15)
            
            if res.status_code == 200:
                try:
                    data = res.json()
                except:
                    continue

                if 'Table' in data:
                    for item in data['Table'][:3]: # દરેક કેટેગરીની ટોપ 3 જાહેરાતો
                        comp_name = item.get('SLONGNAME', '')
                        heading = item.get('NEWSSUB', '')
                        pdf_link = item.get('ATTACHMENTNAME', '')
                        
                        heading_lower = heading.lower()
                        is_valid = False
                        
                        # જો રિઝલ્ટ વાળી લિંક હોય
                        if req_type == "result" and ('result' in heading_lower or 'financial' in heading_lower):
                            is_valid = True
                        # જો ઓર્ડર વાળી લિંક હોય (Company Update માંથી)
                        elif req_type == "order" and any(k in heading_lower for k in ['order', 'award', 'contract', 'receipt']):
                            is_valid = True
                            
                        if comp_name and pdf_link and is_valid:
                            full_pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}"
                            pdf_list.append({
                                'company': comp_name,
                                'heading': heading,
                                'pdf_url': full_pdf_url,
                                'type': req_type # ઓળખ માટે કે આ રિઝલ્ટ છે કે ઓર્ડર
                            })
        except Exception as e:
            print(f"BSE API Error ({req_type}): {e}")
            
    return pdf_list

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

def analyze_with_gemini(company_name, pdf_text, doc_type):
    if not GEMINI_API_KEY: return ""
    
    if doc_type == "result":
        prompt = f"""
        You are an expert Indian stock market analyst for 'MarketMunafa'.
        Analyze this BSE corporate announcement PDF for {company_name}.
        
        This is a FINANCIAL RESULT. Output EXACTLY in this Gujarati format:
        **આવક (Revenue):** [amount] ([growth % YoY])
        **ચોખ્ખો નફો (Net Profit):** [amount] ([growth % YoY])
        **EBITDA:** [amount] ([growth % YoY])
        
        **નિષ્કર્ષ (Summary):** [2 lines in Gujarati about market impact]
        
        PDF Text:
        {pdf_text[:4000]}
        """
    else:
        prompt = f"""
        You are an expert Indian stock market analyst for 'MarketMunafa'.
        Analyze this BSE corporate announcement PDF for {company_name}.
        
        This is a NEW ORDER / CONTRACT. Output EXACTLY in this Gujarati format:
        **ક્લાયન્ટ (Client):** [Name of the client giving the order]
        **ઓર્ડરની કિંમત (Order Value):** [Amount in Rs/Crores]
        **વિગતો (Details):** [1-2 lines explaining the order/work in Gujarati]
        
        **નિષ્કર્ષ (Summary):** [1 line in Gujarati about how good this is for the company]
        
        PDF Text:
        {pdf_text[:4000]}
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

def run_pdf_bot():
    ist_now_str = get_ist_now().strftime("%d %b %Y")
    pdfs = get_latest_bse_pdfs()
    
    if not pdfs:
        print(f"[{ist_now_str}] ⚪ કોઈ નવી રિઝલ્ટ કે ઓર્ડરની PDF મળી નથી.")
        return
        
    for item in pdfs:
        print(f"Processing: {item['company']} - {item['type'].upper()}")
        pdf_text = extract_text_from_pdf_url(item['pdf_url'])
        
        if pdf_text and len(pdf_text) > 200:
            ai_summary_gu = analyze_with_gemini(item['company'], pdf_text, item['type'])
            
            if ai_summary_gu:
                tag = "New Order Win" if item['type'] == "order" else "Q2 Financial Results"
                post_title = f"{item['company']} {tag} ({ist_now_str}) | MarketMunafa"
                
                # --- Telegram Message ---
                t_msg = f"📊 *{item['company']} {tag} (BSE Update)*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                t_msg += ai_summary_gu
                t_msg += "\n\n✅ *MarketMunafa Analysis*"
                send_telegram(t_msg)
                
                formatted_summary = ai_summary_gu.replace('**', '<b>').replace('\n', '<br>')
                
                # --- Blogger HTML ---
                b_html = f"""
                <div class="market-update-post">
                    <h3>{item['company']} - {tag}</h3>
                    <p><i>Official BSE Update via MarketMunafa AI</i></p>
                    <div style="background-color: #f8f9fa; padding: 15px; border-left: 4px solid #10b981; margin-bottom: 20px;">
                        {formatted_summary}
                    </div>
                    <p><a href="{item['pdf_url']}" target="_blank" rel="nofollow noopener">Download Original BSE PDF Here</a></p>
                    <hr>
                </div>
                """
                post_to_blogger(post_title, b_html)
                time.sleep(5)

if __name__ == "__main__":
    run_pdf_bot()
