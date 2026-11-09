import random
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as c_requests
from fake_useragent import UserAgent

def get_random_impersonate():
    """BSE ને છેતરવા માટે દર વખતે અલગ બ્રાઉઝર પ્રોફાઇલ પસંદ કરશે"""
    profiles = [
        "chrome120", "chrome110", "chrome116",
        "edge101", "edge99", 
        "safari15_3", "safari15_5"
    ]
    return random.choice(profiles)

def get_random_headers():
    """દર વખતે એકદમ નવો અને અસલી દેખાતો User-Agent બનાવશે"""
    ua = UserAgent()
    return {
        'User-Agent': ua.random,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.google.com/',  # એવું લાગશે કે માણસ Google પરથી સર્ચ કરીને આવ્યો છે
        'Connection': 'keep-alive'
    }

def fetch_bse_data_safely(url):
    """BSE પરથી કોઈપણ ડેટા (Forth Results કે Announcements) સુરક્ષિત રીતે ખેંચવા માટેનું ફંક્શન"""
    
    headers = get_random_headers()
    browser_profile = get_random_impersonate()
    
    print(f"🕵️ Using Mask: {browser_profile} | User-Agent: {headers['User-Agent'][:40]}...")
    
    # 1 થી 3 સેકન્ડનો રેન્ડમ વિરામ (માણસ જેવી વર્તણૂક માટે)
    time.sleep(random.uniform(1.0, 3.0))
    
    try:
        # curl_cffi નો ઉપયોગ જે Cloudflare અને BSE ના એન્ટી-બોટ સિસ્ટમને બાયપાસ કરે છે
        response = c_requests.get(
            url, 
            headers=headers, 
            impersonate=browser_profile, 
            timeout=15
        )
        
        if response.status_code == 200:
            print("✅ Data Fetched Successfully without blocking!")
            return response
        else:
            print(f"⚠️ Status Code: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        return None

# --- ઉપયોગ કેવી રીતે કરવો? (ઉદાહરણ) ---
if __name__ == "__main__":
    # ઉદાહરણ તરીકે: BSE ની Forth Results ની લિંક
    bse_api_url = "https://api.bseindia.com/BseIndiaAPI/api/ForthResults/w"
    
    # સુરક્ષિત રીતે ડેટા ખેંચો
    res = fetch_bse_data_safely(bse_api_url)
    
    if res:
        try:
            data = res.json()
            # ફક્ત ચેક કરવા માટે કે ડેટા આવ્યો કે નહીં
            print("📊 Received Data Points:", len(data.get('Table', [])) if isinstance(data, dict) else len(data))
        except:
            print("Data is not JSON format. Probably HTML.")
