import os
import sys
import datetime
import time
import json
import logging
import smtplib
import re
import argparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import trafilatura
from dotenv import load_dotenv

# Load local environment variables if .env file exists (useful for local testing)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# --- Default Fallback API Key ---
# Using the Tavily API key provided by the user
DEFAULT_TAVILY_KEY = "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"

class DailyDigestPipeline:
    def __init__(self, dry_run=False, email_test=False):
        self.dry_run = dry_run
        self.email_test = email_test
        
        # Keys setup
        self.gemini_key = os.environ.get("GEMINI_API_KEY")
        self.openai_key = os.environ.get("OPENAI_API_KEY")
        self.tavily_key = os.environ.get("TAVILY_API_KEY", DEFAULT_TAVILY_KEY)
        
        # Email settings
        self.smtp_email = os.environ.get("SMTP_EMAIL", "yogeshgujar@gmail.com")
        self.smtp_password = os.environ.get("SMTP_PASSWORD")  # Gmail App Password

        # Environment Diagnostics
        logging.info("=== ENV DIAGNOSTICS ===")
        for name, val in [
            ("GEMINI_API_KEY", self.gemini_key),
            ("OPENAI_API_KEY", self.openai_key),
            ("TAVILY_API_KEY", self.tavily_key),
            ("SMTP_EMAIL", self.smtp_email),
            ("SMTP_PASSWORD", self.smtp_password),
        ]:
            if val:
                val_str = str(val).strip()
                masked = f"{val_str[:4]}...{val_str[-4:]}" if len(val_str) > 8 else "too_short"
                logging.info(f"  {name}: SET (len={len(val_str)}, pattern={masked})")
            else:
                logging.info(f"  {name}: NOT SET")
        logging.info("=======================")
        
        # Scraper session
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Target URLs in sites.txt
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sites_file = os.path.join(self.base_dir, 'sites.txt')
        self.sites = self.load_sites()
        
        # State tracking files
        self.active_events_file = os.path.join(self.base_dir, 'data', 'active_events.json')
        self.shown_tools_file = os.path.join(self.base_dir, 'data', 'shown_tools.json')
        self.shown_intel_file = os.path.join(self.base_dir, 'data', 'shown_intel.json')
        
        # Crawler keywords
        self.update_keywords = ['notification', 'circular', 'public notice', 'press release', 'news', 'update', 'latest']
        self.ignore_keywords = ['login', 'register', 'signup', 'apply', 'contact', 'about', 'help', 'search', 'lang=']

    def load_sites(self):
        """Loads crawling targets from sites.txt."""
        if not os.path.exists(self.sites_file):
            logging.warning(f"sites.txt not found in {self.base_dir}. Using DGFT default.")
            return ["https://www.dgft.gov.in/CP/"]
            
        with open(self.sites_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]

    # ==========================================
    # CRAWLING & SCRAPING ENGINE (sites.txt)
    # ==========================================
    def is_link_interesting(self, text, url):
        text = text.lower()
        url = url.lower()
        if not any(k in text or k in url for k in self.update_keywords):
            return False
        if any(k in text or k in url for k in self.ignore_keywords):
            return False
        return True

    def find_sub_links(self, url):
        """Scans a website landing page for relevant sub-links (notifications, circulars, etc.)."""
        logging.info(f"Scanning target URL: {url}...")
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            interesting_links = {url} # include main page itself
            for a in soup.find_all('a', href=True):
                href = a['href']
                text = a.get_text(" ", strip=True)
                full_url = urljoin(url, href)
                
                if self.is_link_interesting(text, full_url):
                    interesting_links.add(full_url)
            
            # Return top 5 most promising links + main page
            return list(interesting_links)[:6]
        except Exception as e:
            logging.error(f"Failed to scan target sub-links for {url}: {e}")
            return [url]

    def scrape_url_content(self, url):
        """Deep extracts clean text content from a URL using trafilatura."""
        logging.info(f"Extracting content from {url}...")
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                return trafilatura.extract(downloaded)
        except Exception as e:
            logging.error(f"Trafilatura failed for {url}: {e}")
        return None

    def crawl_target_sites(self):
        """Crawls all configured sites and returns combined text corpora."""
        crawled_data = []
        for url in self.sites:
            sub_links = self.find_sub_links(url)
            logging.info(f"Found {len(sub_links)} pages to extract on {url}")
            
            site_content = []
            for link in sub_links:
                text = self.scrape_url_content(link)
                if text and len(text) > 100:
                    # Ignore generic login/account pages
                    if "login" in text.lower().split()[:20]:
                        continue
                    site_content.append(f"Source URL: {link}\n---\n{text}\n")
                    time.sleep(1) # Be polite
                    
            if site_content:
                crawled_data.append(f"=== TARGET SITE SCANNED: {url} ===\n" + "\n".join(site_content))
        return "\n\n".join(crawled_data)

    # ==========================================
    # WEB INTELLIGENCE SEARCH ENGINE (Tavily/DDG)
    # ==========================================
    def search_web_tavily(self, query):
        """Queries Tavily API with strict 24-hour time constraint."""
        logging.info(f"Tavily Search: '{query}'")
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "search_depth": "advanced",
            "include_domains": [],
            "exclude_domains": [],
            "max_results": 5,
            "days": 1 # STRICT 24 HOUR FILTER
        }
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            results = response.json().get("results", [])
            
            formatted_results = []
            for r in results:
                formatted_results.append(f"Title: {r.get('title')}\nURL: {r.get('url')}\nContent: {r.get('content')}\n")
            return "\n".join(formatted_results)
        except Exception as e:
            logging.error(f"Tavily search failed for '{query}': {e}. Falling back to DuckDuckGo.")
            return self.search_web_ddg(query)

    def search_web_ddg(self, query):
        """DuckDuckGo Search fallback with 24h filter."""
        logging.info(f"DuckDuckGo Search: '{query}'")
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                # time='d' filters results to the last 24 hours
                results = list(ddgs.text(query, max_results=5, timelimit='d'))
                
            formatted_results = []
            for r in results:
                formatted_results.append(f"Title: {r.get('title')}\nURL: {r.get('href')}\nContent: {r.get('body')}\n")
            return "\n".join(formatted_results)
        except Exception as e:
            logging.error(f"DuckDuckGo search failed for '{query}': {e}")
            return ""

    def run_broad_search(self):
        """Runs optimized search queries for the topics and social media handles."""
        today_str = datetime.date.today().isoformat()
        
        # Load user context to read custom social handles
        social_handles = {
            "x": ["dgftindia", "CimGOI", "DoC_GoI", "FieoHq", "PiyushGoyal", "APEDADOC", "AgriGoI", "theresanaiforit"],
            "linkedin": [],
            "instagram": []
        }
        
        user_context_path = os.path.join(self.base_dir, 'user_context.json')
        if os.path.exists(user_context_path):
            try:
                with open(user_context_path, 'r', encoding='utf-8') as f:
                    ucontext = json.load(f)
                    if "social_handles" in ucontext:
                        for platform in ["x", "linkedin", "instagram"]:
                            if platform in ucontext["social_handles"]:
                                social_handles[platform] = ucontext["social_handles"][platform]
            except Exception as e:
                logging.error(f"Failed to load user_context.json for social handles: {e}")

        # 1. Social Media Handles Search
        social_queries = []
        
        # Construct X/Twitter queries (grouped in 3s to save search calls)
        x_handles = social_handles.get("x", [])
        if x_handles:
            for i in range(0, len(x_handles), 3):
                chunk = x_handles[i:i+3]
                or_terms = " OR ".join([f"site:x.com/{handle}" for handle in chunk])
                social_queries.append(f"({or_terms}) {today_str}")

        # Construct LinkedIn queries (grouped in 2s)
        # Supports both slugs and company names
        linkedin_handles = social_handles.get("linkedin", [])
        if linkedin_handles:
            for i in range(0, len(linkedin_handles), 2):
                chunk = linkedin_handles[i:i+2]
                terms = []
                for handle in chunk:
                    if " " in handle or "(" in handle:
                        # Clean company name
                        clean_name = re.sub(r'\(.*?\)', '', handle).strip()
                        terms.append(f'site:linkedin.com/company "{clean_name}"')
                    else:
                        terms.append(f"site:linkedin.com/company/{handle} OR site:linkedin.com/in/{handle}")
                or_terms = " OR ".join(terms)
                social_queries.append(f"({or_terms}) {today_str}")

        # Construct Instagram queries (grouped in 2s)
        instagram_handles = social_handles.get("instagram", [])
        if instagram_handles:
            for i in range(0, len(instagram_handles), 2):
                chunk = instagram_handles[i:i+2]
                or_terms = " OR ".join([f"site:instagram.com/{handle}" for handle in chunk])
                social_queries.append(f"({or_terms}) {today_str}")

        # 2. Broad Intelligence batched queries
        topic_queries = [
            # Currency & Mandi Prices (Strictly fetch today's rates/prices)
            f"(\"USD/INR\" OR \"EUR/INR\" exchange rate) AND (\"Nashik onion price\" OR \"Nashik garlic price\" OR \"turmeric price\" mandi) {today_str}",
            
            # Topic 1: Indian Export Policy & Trade Bodies
            f"(\"India export policy\" OR \"DGFT notification\" OR \"APEDA\" OR \"Spices Board\" OR \"FIEO\" OR \"FTA India\" OR \"JNPT customs\" OR \"CBIC customs\") {today_str}",
            
            # Topic 2: Dehydrated Foods (Spices/Onion/Garlic/Turmeric/Banana/Tomato)
            f"(\"dehydrated onion\" OR \"dehydrated garlic\" OR \"turmeric curcumin\" OR \"banana powder\" OR \"dehydrated ginger\") AND (export OR market OR trade) {today_str}",
            f"(\"dehydrated vegetable\" OR \"dehydrated potato flakes\" OR \"dehydrated mushroom\" OR \"dehydrated tomato powder\") AND (export OR market OR trade) {today_str}",
            
            # Topic 3: Target Markets (GCC, EU, US, Asia)
            f"(\"India GCC food trade\" OR \"UAE food import\" OR \"Saudi Arabia agri import\") {today_str}",
            f"(\"India EU food trade\" OR \"Germany food ingredient\" OR \"EU food safety regulation\" OR \"Listeria\") {today_str}",
            f"(\"FDA import alert India food\" OR \"US food ingredient market India\" OR \"Salmonella\") {today_str}",
            f"(\"India Asia food trade\" OR \"Japan Korea food import India\" OR \"Singapore food import\" OR \"West Africa food import\") {today_str}",
            
            # Topic 4: Market Intelligence & Opportunities
            f"(\"dehydrated food buyer\" OR \"food ingredient procurement tender\" OR \"dehydrated vegetable importer\" OR \"competitor country dehydrated veg export\") {today_str}",
            
            # Topic 5: Events, Training, Buyer-Seller Meets (Upcoming focus)
            f"(\"food trade fair\" OR \"expo\" OR \"exhibition\" OR \"buyer seller meet\" OR \"agri food trade show\" OR \"food ingredient show\") AND (India OR Mumbai OR Delhi OR Riyadh OR GCC OR Europe) 2026",
            f"\"FIEO\" AND (\"buyer seller meet\" OR \"reverse buyer seller\" OR \"training program\" OR \"seminar\" OR \"Nagpur\" OR \"Mumbai\" OR \"Maharashtra\") 2026",
            f"\"APEDA\" AND (\"buyer seller meet\" OR \"Riyadh\" OR \"exhibition\" OR \"trade fair\" OR \"participation\" OR \"training\" OR \"event\") 2026",
            f"(\"Spices Board\" OR \"DGFT\") AND (\"training\" OR \"seminar\" OR \"webinar\" OR \"buyer seller meet\" OR \"advisory\" OR \"event\") 2026",
            
            # Dedicated Maharashtra Buyer-Seller Meets and Exporter events search
            f"(\"buyer seller meet\" OR \"reverse buyer seller\" OR \"RBSM\" OR \"B2B meet\" OR \"exporters meet\" OR \"exporter workshop\" OR \"export seminar\") AND (Maharashtra OR Mumbai OR Pune OR Nagpur OR Nashik OR Aurangabad OR Thane) 2026",

            # Topic 6: AI & Technology Updates
            f"(\"AI tool Indian exporter\" OR \"AI market research tool export\" OR \"AI buyer discovery\" OR \"AI export compliance\") {today_str}",
            f"(\"AI tool small business\" OR \"AI productivity tool SME\" OR \"new AI tool launched\" OR \"site:theresanaiforit.com\") {today_str}",
            f"(\"AI market research competitor analysis\" OR \"AI report generation\" OR \"AI consulting tool\") {today_str}"
        ]
        
        all_search_data = []
        
        # Execute Social Queries
        logging.info("Gathering priority handles updates (Social search)...")
        for q in social_queries:
            results = self.search_web_tavily(q) if self.tavily_key else self.search_web_ddg(q)
            if results:
                all_search_data.append(f"=== SOCIAL HANDLE SEARCH: {q} ===\n{results}")
            time.sleep(1)
            
        # Execute Broad Topic Queries
        logging.info("Gathering broad market intelligence...")
        for q in topic_queries:
            results = self.search_web_tavily(q) if self.tavily_key else self.search_web_ddg(q)
            if results:
                all_search_data.append(f"=== INTEL SEARCH: {q} ===\n{results}")
            time.sleep(1)
            
        return "\n\n".join(all_search_data)

    # ==========================================
    # AI COMPILATION & GENERATION (Gemini API)
    # ==========================================
    def generate_digest_ai(self, raw_crawled, raw_searched):
        """Calls OpenAI or Gemini API to compile and generate the final Daily Digest."""
        today_date_str = datetime.date.today().strftime("%A, %d %B %Y")
        
        # Load and filter active events
        active_events = []
        if os.path.exists(self.active_events_file):
            try:
                with open(self.active_events_file, 'r', encoding='utf-8') as f:
                    active_events = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load active events: {e}")
        
        # Filter out expired events
        today_str = datetime.date.today().isoformat()
        active_events = [e for e in active_events if e.get("end_date", "") >= today_str]
        
        # Format active events to pass to the LLM
        active_events_str = ""
        if active_events:
            for e in active_events:
                active_events_str += f"- Name: {e['name']}\n"
                active_events_str += f"  Type: {e.get('type', 'domestic')}\n"
                active_events_str += f"  Date: {e.get('start_date')} to {e.get('end_date')} | Venue: {e.get('venue')}\n"
                active_events_str += f"  Why it matters: {e.get('why_it_matters')}\n"
                active_events_str += f"  Register/Info: {e.get('link')}\n\n"
        else:
            active_events_str = "No currently active events in the tracker.\n"

        # Load shown tools
        shown_tools = []
        if os.path.exists(self.shown_tools_file):
            try:
                with open(self.shown_tools_file, 'r', encoding='utf-8') as f:
                    shown_tools = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load shown tools: {e}")
                
        shown_tools_list = ", ".join(shown_tools) if shown_tools else "None"

        # Load shown market intel URLs
        shown_intel = []
        if os.path.exists(self.shown_intel_file):
            try:
                with open(self.shown_intel_file, 'r', encoding='utf-8') as f:
                    shown_intel = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load shown intel: {e}")
                
        shown_intel_list = ", ".join(shown_intel) if shown_intel else "None"
        
        # System Prompt and Instructions (Your exact prompt template with dynamic dates)
        system_prompt = f"""You are an export business intelligence assistant for
Yogesh Badgujar, founder of two businesses:

SUPAB EXPORTS (Kalyan West, Maharashtra)
Agricultural export business specializing in
dehydrated vegetables and spices. Core products:
dehydrated onion (flakes, chopped, minced, granules,
powder), dehydrated garlic (flakes, granules, powder,
minced), turmeric (fingers, bulbs, powder — 2% to 7%
curcumin grades), banana powder (food grade and
pharmaceutical grade). Open to opportunities in ALL
dehydrated foods — dehydrated tomato, potato, ginger,
beetroot, spinach, mushroom, sweet corn, any other
dehydrated vegetable or fruit powder where a strong
buyer demand or market gap exists.

SUPAB DIGITAL
AI-powered export consultancy for Indian SMEs and
exporters. Services: custom market research reports
(basic to McKinsey-level depth), go-to-market strategy
reports for Indian SMEs entering international markets,
buyer outreach systems, LinkedIn optimization, export
compliance guidance. Recent work: go-to-market strategy
for Vaince Cosmetics for EU market entry. Clients are
Indian small businesses and exporters wanting to go
international for the first time or expand into new
markets.

==========================================
RELEVANCE FILTER - STRICTLY ENFORCED
==========================================
INCLUDE:
✅ Export policy changes, schemes, incentives
✅ Any dehydrated food product with export demand signal — not just current products
✅ Buyer country import regulations, compliance changes, market access updates
✅ Demand signals from importers or buyer country trade bodies
✅ Trade fairs, expos, buyer-seller meets, APEDA/Spice Board events and training
✅ AI tools useful for: export business, market research, report writing, buyer outreach, compliance, daily SME operations, client-facing consulting work
✅ Commodity prices: onion, garlic, turmeric, tomato, any dehydrated food
✅ Freight rates, logistics, container costs
✅ Rupee movement vs USD/EUR/AED/GBP
✅ Competitor country signals (China, Egypt, Peru, Turkey) in dehydrated vegetables
✅ Any new product opportunity in dehydrated foods with emerging buyer demand

EXCLUDE:
❌ Political news unrelated to trade
❌ Cricket, entertainment, celebrity
❌ AI hype with no clear practical use case
❌ Tools clearly designed only for large enterprise — not applicable to SMEs
❌ Duplicate items already in the report
❌ Anything older than 24 hours
❌ Generic startup/VC funding news with no relevance to export or SME operations

==========================================
CRITICAL QUALITY RULES - NON-NEGOTIABLE
==========================================
1. 🚫 NO PLACEHOLDERS OR INCOMPLETE DATA (EXCEPT FOR CRITICAL UPCOMING EVENTS):
   - If any standard news item is missing crucial details (e.g. source, link, or exact price), DO NOT include it in the digest.
   - Never write placeholders like 'Date: Not specified', 'Venue: Not specified', 'likely India', or 'context implies'.
   - **CRITICAL EXCEPTION FOR UPCOMING B2B EVENTS / TRAINING:** You must **never miss** any upcoming B2B trade fairs, export expos, buyer-seller meets, training programs, or seminars organized by an EPC (like APEDA, Spices Board, FIEO, Spice Board India) or DGFT that are relevant to agricultural/spice exports. Include them even if some registration links or exact venues are still pending, stating the available details clearly so Yogesh is kept informed of upcoming opportunities.
2. 🚫 FUTURE EVENTS ONLY:
   - Today is {today_date_str}. Only include events and opportunities scheduled to take place on or after {today_date_str}. Omit past events completely.
3. 🚫 NO GENERAL/IRRELEVANT NOISE:
   - Avoid including general trade news (like generic bilateral protocols) unless it directly impacts the user's specific products (onion, garlic, turmeric, banana powder, rice, mango pulp). For example, a protocol between India and Ethiopia is noise and must be excluded.
4. ⚡ BE EXTREMELY CRISP AND DENSE:
   - Keep descriptions very short (1-2 sentences).
   - "Opportunity for Supab" or "What it means for you" must be exactly one sharp, direct, highly-actionable sentence.
   - Total email length must be under 600 words. A few high-signal, fully complete items are much better than many generic ones.
5. 📊 QUICK NUMBERS:
   - Always search for exchange rates (USD/INR, EUR/INR) and Nashik mandi prices for raw onion and garlic (₹/quintal) in the collected raw search data and list them cleanly.
6. 🏛️ GOVERNMENT & POLICY:
   - Always prioritize and extract specific notification numbers, circular numbers, or public notices (e.g., DGFT Public Notice 15/2026-27 or EU Regulation 2024/2895), exact dates, and official sources. Never write them without these identifiers.
   - Always clarify whether trade updates are (export-side) or (import-side) in the headline.
7. Handle posts AND broad web findings are equally valid — label source clearly.
8. Plain business English — no jargon.
9. 🚫 DO NOT REPEAT SHOWN TOOLS:
   - You must NOT recommend or display any of the following AI tools that have already been shown: {shown_tools_list}.
10. 🚫 DO NOT REPEAT SHOWN MARKET INTELLIGENCE REPORTS:
    - You must NOT include, summarize, or recommend any market intelligence research reports or articles that link to or reference the following already shown URLs: {shown_intel_list}.
"""

        user_instruction = f"""
Here is the raw data collected in the last 24 hours from the target crawled pages and optimized web searches. 
Read the content carefully, apply the strict relevance filters and critical quality rules, and generate the final email report in the exact format specified below.

### RAW CRAWLED DATA (DGFT / CONFIG SITES)
{raw_crawled}

### RAW SEARCH & SOCIAL INTELLIGENCE (TAVILY & DDG)
{raw_searched}

### PERSISTENT ACTIVE B2B EVENTS (Already tracked, keep these in the digest unless concluded)
{active_events_str}

### EMAIL REPORT FORMAT REQUIREMENT
Subject: 🌏 Supab Export Intel — [Today's Date]
Send to: yogeshgujar@gmail.com

---

Good morning Yogesh 🙏
Daily briefing for [Date, Day].

---

📋 WHAT MATTERS TODAY
[Urgent only — policy deadline, major opportunity, breaking news directly affecting Supab Exports or Supab Digital. Max 2 items. Skip entirely if nothing critical.]

---

🏛️ GOVERNMENT & POLICY
[Priority handle posts + broad search findings. Highlight if it's (export-side) or (import-side).]
- [Headline — one plain line]
  What it means for you: [one sentence, specific to Supab Exports or Supab Digital]
  Source: [@handle or publication] | [link]

[Max 4 items. If nothing relevant: "No significant policy updates today."]

---

📦 MARKET & PRODUCT INTELLIGENCE
[Product demand, market signals, buyer country news, commodity prices, competitor countries, new product opportunities in dehydrated foods]
- [Headline — one line]
  Details: [2–3 sentences]
  Opportunity for Supab: [specific product or market angle — be direct about whether this is worth pursuing]
  Source: [link]

[Max 4 items. If nothing relevant: "No significant market updates today."]

---

🇮🇳 DOMESTIC B2B EVENTS & MEETS
[Trade fairs, expos, meets, training, seminars happening in India, particularly Maharashtra (Mumbai, Pune, Nagpur, Nashik, etc.). You must include all the persistent active events listed above if they are domestic, and add any new domestic B2B meets found in today's search. Keep showing domestic events until they are concluded.]
- [Event Name]
  Date: | Venue:
  Why it matters: [one sentence — who attends, what opportunity it creates for Supab]
  Register/Info: [link]

[Max 4 items. If nothing: "No relevant domestic B2B events or meets today."]

---

🌏 INTERNATIONAL B2B EVENTS
[Trade fairs, expos, meets outside India (e.g. GCC, Europe, US) that the user can leverage for leads or digital advertising. You must include all the persistent active events listed above if they are international, and add any new international ones found.]
- [Event Name]
  Date: | Venue:
  Why it matters: [one sentence — who attends, what opportunity it creates for Supab]
  Register/Info: [link]

[Max 3 items. If nothing: "No relevant international B2B events today."]

---

🤖 AI & TOOLS
[Useful for: export operations, market research, report writing, client consulting, buyer outreach, or daily SME tasks. All three angles covered. Remember, DO NOT recommend any tools listed as already shown.]
- [Tool name] — [Export / Operations / Research]
  What it does: [one line]
  How you can use it: [specific use case for Supab Exports or Supab Digital]
  Link: [url]

[Max 3 items. If nothing genuinely useful: "No significant AI tool updates today."]

---

📊 QUICK NUMBERS
[Commodity prices, freight rates, currency — only if found and directly relevant]
- [Data point]: [value] — [one line context]

[Skip entirely if no relevant numbers found]

---

🔒 WATCH THIS WEEK
[Max 3 items worth tracking in next 7 days — deadlines, upcoming events, policy windows]
- [Item]
- [Item]
- [Item]

---
Report generated: [timestamp]
Priority handles: @CimGOI @DoC_GoI @FieoHq @PiyushGoyal @theresanaiforit + broad web intelligence across all platforms.

### STATE UPDATE FORMAT REQUIREMENT (CRITICAL FOR SYSTEM RETENTION)
At the very end of your response, output a JSON block wrapped inside <state_update> and </state_update> tags.
This JSON block must contain:
1. All active events (both the persistent ones list and any new events you discovered from today's search that you decided to add).
2. The names of the AI tools you included in today's digest.
3. The URLs of any market/product intelligence items you included in today's digest.
Example:
<state_update>
{{
  "events": [
    {{
      "name": "FIEO Nagpur Reverse Buyer-Seller Meet (RBSM)",
      "start_date": "2026-07-02",
      "end_date": "2026-07-03",
      "venue": "Nagpur, Maharashtra, India",
      "why_it_matters": "Organized by FIEO in Maharashtra; focused on Agro & Food Processing, and Fresh Vegetables, Fruits & Cereals. High relevance for direct networking with international buyers.",
      "link": "https://www.fieo.org",
      "type": "domestic"
    }}
  ],
  "tools": ["ToolName1"],
  "intel_urls": ["https://www.example.com/report-url"]
}}
</state_update>
"""

        # 1. Attempt OpenAI API (gpt-4o-mini)
        if self.openai_key:
            logging.info("Attempting daily digest compilation using OpenAI (gpt-4o-mini)...")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.openai_key}"
            }
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_instruction}
                ],
                "temperature": 0.2
            }
            try:
                response = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=60)
                response.raise_for_status()
                result_text = response.json()["choices"][0]["message"]["content"]
                logging.info("OpenAI compilation successful!")
                return result_text
            except Exception as e:
                logging.warning(f"OpenAI API call failed: {e}. Falling back to Google Gemini...")
        
        # 2. Fallback to Google Gemini
        if self.gemini_key:
            logging.info("Attempting daily digest compilation using Google Gemini...")
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.gemini_key)
                model = genai.GenerativeModel(
                    model_name="gemini-2.5-flash",
                    system_instruction=system_prompt
                )
                response = model.generate_content(
                    user_instruction,
                    generation_config={"temperature": 0.2}
                )
                
                result_text = response.text
                if self.openai_key:
                    # Add fallback notice if OpenAI key was present but failed
                    result_text += "\n\n*(Compiled using Gemini backup engine)*"
                logging.info("Gemini compilation successful!")
                return result_text
            except Exception as e:
                import traceback
                logging.error(f"Gemini API call failed: {e}")
                logging.error(traceback.format_exc())
                
        # 3. Final failure if neither works
        if self.dry_run:
            return "## [DRY RUN] API output placeholder. (Please configure valid API keys to generate actual digest)"
        sys.exit("Error: Both OpenAI and Gemini API calls failed, or keys are missing.")

    # ==========================================
    # EMAIL SENDING SYSTEM (Gmail SMTP)
    # ==========================================
    def format_email_body_html(self, text_content):
        """Converts generated plain markdown digest text into a stunning, responsive HSL-styled HTML layout."""
        # Simple parser to format markdown headers and blocks into clean styled cards
        lines = text_content.split('\n')
        html_sections = []
        current_section = []
        
        def commit_section():
            if current_section:
                html_sections.append("\n".join(current_section))
                current_section.clear()

        # Parse markdown lines into formatted blocks
        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                current_section.append("<br/>")
                continue
                
            # Headers
            if line_strip.startswith("📋") or "WHAT MATTERS TODAY" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card urgent">')
                current_section.append(f'<h2 class="section-title">🚨 WHAT MATTERS TODAY</h2>')
            elif line_strip.startswith("🏛️") or "GOVERNMENT & POLICY" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🏛️ GOVERNMENT & POLICY</h2>')
            elif line_strip.startswith("📦") or "MARKET & PRODUCT" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">📦 MARKET & PRODUCT INTEL</h2>')
            elif "DOMESTIC B2B EVENTS" in line_strip.upper() or "DOMESTIC EVENTS" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🇮🇳 DOMESTIC B2B EVENTS & MEETS</h2>')
            elif "INTERNATIONAL B2B EVENTS" in line_strip.upper() or "INTERNATIONAL EVENTS" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🌏 INTERNATIONAL B2B EVENTS</h2>')
            elif line_strip.startswith("🤝") or "EVENTS & OPPORTUNITIES" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🤝 EVENTS & OPPORTUNITIES</h2>')
            elif line_strip.startswith("🤖") or "AI & TOOLS" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🤖 AI & TOOLS</h2>')
            elif line_strip.startswith("📊") or "QUICK NUMBERS" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card highlight">')
                current_section.append(f'<h2 class="section-title">📊 QUICK NUMBERS</h2>')
            elif line_strip.startswith("🔜") or line_strip.startswith("🔒") or "WATCH THIS WEEK" in line_strip.upper():
                commit_section()
                current_section.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🔒 WATCH THIS WEEK</h2>')
            elif line_strip == "---":
                commit_section()
                # Simple divider or end card
                html_sections.append('</div>')
            else:
                # Format bullets, lists and bold items
                formatted_line = line_strip
                # Format links
                formatted_line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" class="link">\1</a>', formatted_line)
                # Format bold text
                formatted_line = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', formatted_line)
                
                if formatted_line.startswith("-"):
                    current_section.append(f'<li class="bullet-item">{formatted_line[1:].strip()}</li>')
                else:
                    current_section.append(f'<p class="para-text">{formatted_line}</p>')
                    
        commit_section()
        body_content = "\n".join(html_sections)
        
        # Sleek Premium styling utilizing tailored colors and vibrant headers
        html_template = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Supab Export Daily Intel</title>
<style>
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background-color: #f4f6f8;
        color: #2c3e50;
        margin: 0;
        padding: 0;
        line-height: 1.6;
    }}
    .email-container {{
        max-width: 650px;
        margin: 20px auto;
        background: #ffffff;
        border-radius: 12px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.05);
        overflow: hidden;
        border: 1px solid #e1e8ed;
    }}
    .header {{
        background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
        color: #ffffff;
        padding: 30px 20px;
        text-align: center;
    }}
    .header h1 {{
        margin: 0;
        font-size: 24px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }}
    .header p {{
        margin: 5px 0 0 0;
        font-size: 14px;
        opacity: 0.9;
    }}
    .content {{
        padding: 25px 20px;
    }}
    .section-card {{
        background: #ffffff;
        border-left: 4px solid #2a5298;
        padding: 15px 20px;
        margin-bottom: 25px;
        border-radius: 0 8px 8px 0;
        box-shadow: 0 2px 5px rgba(0,0,0,0.02);
        border-top: 1px solid #f0f4f8;
        border-right: 1px solid #f0f4f8;
        border-bottom: 1px solid #f0f4f8;
    }}
    .section-card.urgent {{
        border-left-color: #e74c3c;
        background-color: #fdf2f2;
    }}
    .section-card.highlight {{
        border-left-color: #f39c12;
        background-color: #fef9eb;
    }}
    .section-title {{
        font-size: 16px;
        font-weight: 700;
        color: #1e3c72;
        margin-top: 0;
        margin-bottom: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    .urgent .section-title {{
        color: #c0392b;
    }}
    .para-text {{
        font-size: 14px;
        margin: 8px 0;
    }}
    .bullet-item {{
        font-size: 14px;
        margin: 8px 0;
        list-style-type: none;
        position: relative;
        padding-left: 15px;
    }}
    .bullet-item::before {{
        content: "•";
        color: #2a5298;
        font-weight: bold;
        position: absolute;
        left: 0;
    }}
    .link {{
        color: #2a5298;
        text-decoration: none;
        font-weight: 500;
    }}
    .link:hover {{
        text-decoration: underline;
    }}
    .footer {{
        background: #f8fafc;
        padding: 20px;
        text-align: center;
        font-size: 12px;
        color: #7f8c8d;
        border-top: 1px solid #ecf0f1;
    }}
    .footer a {{
        color: #7f8c8d;
    }}
</style>
</head>
<body>
<div class="email-container">
    <div class="header">
        <h1>🌏 SUPAB EXPORT DAILY INTEL</h1>
        <p>Strategic Business Intelligence Briefing</p>
    </div>
    <div class="content">
        {body_content}
    </div>
    <div class="footer">
        <p>Report dynamically generated on {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p>Priority Handles Checked: @CimGOI @DoC_GoI @FieoHq @PiyushGoyal @theresanaiforit</p>
        <p>Supab Exports &amp; Supab Digital &copy; 2026. All rights reserved.</p>
    </div>
</div>
</body>
</html>
"""
        return html_template

    def send_email(self, text_content):
        """Sends the generated digest via SMTP (Gmail App Password)."""
        if not self.smtp_password:
            logging.error("SMTP_PASSWORD environment variable is not set. Skipping email delivery.")
            if self.dry_run or self.email_test:
                logging.info("[DUMMY EMAIL SUCCESS] Saved local plain-text output in daily_digest_output.md due to missing password.")
                with open("daily_digest_output.md", "w", encoding="utf-8") as f:
                    f.write(text_content)
                return
            sys.exit("Error: SMTP_PASSWORD is required to email the digest.")

        today_str = datetime.date.today().strftime("%d-%b-%Y (%A)")
        subject = f"🌏 Supab Export Intel — {today_str}"
        recipient = self.smtp_email
        
        logging.info(f"Sending daily intelligence email to {recipient}...")
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.smtp_email
        msg['To'] = recipient
        
        # Attach plain text version
        part1 = MIMEText(text_content, 'plain')
        msg.attach(part1)
        
        # Attach styled HTML version
        html_body = self.format_email_body_html(text_content)
        part2 = MIMEText(html_body, 'html')
        msg.attach(part2)
        
        try:
            # Connect to Gmail SMTP server using SSL on port 465
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
            server.login(self.smtp_email, self.smtp_password)
            server.sendmail(self.smtp_email, [recipient], msg.as_string())
            server.close()
            logging.info("Email delivered successfully!")
        except Exception as e:
            logging.error(f"Failed to deliver email over SMTP: {e}")
            sys.exit(f"SMTP Error: {e}")

    def parse_state_update(self, text_content):
        """Extracts the <state_update> block from LLM text, parses the JSON, updates state files, and returns clean text."""
        pattern = re.compile(r'<state_update>(.*?)</state_update>', re.DOTALL)
        match = pattern.search(text_content)
        if not match:
            return text_content, {}

        state_json_str = match.group(1).strip()
        
        # Clean markdown code fences if present
        if state_json_str.startswith("```"):
            lines = state_json_str.split('\n')
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            state_json_str = "\n".join(lines).strip()

        try:
            state_data = json.loads(state_json_str)
            # Remove the state_update tag from the text
            clean_text = pattern.sub('', text_content).strip()
            return clean_text, state_data
        except Exception as e:
            logging.error(f"Failed to parse state_update JSON: {e}")
            # Still clean the text so it isn't emailed to the user
            clean_text = pattern.sub('', text_content).strip()
            return clean_text, {}

    def update_state_files(self, state_data):
        """Updates active_events.json, shown_tools.json, and shown_intel.json with the new state data."""
        # 1. Update events
        new_events = state_data.get("events", [])
        if isinstance(new_events, list):
            # Load existing
            active_events = []
            if os.path.exists(self.active_events_file):
                try:
                    with open(self.active_events_file, 'r', encoding='utf-8') as f:
                        active_events = json.load(f)
                except Exception as e:
                    logging.error(f"Error reading active_events.json: {e}")
            
            # Merge case-insensitively by event name
            event_map = {e["name"].lower().strip(): e for e in active_events if "name" in e}
            for ne in new_events:
                if not isinstance(ne, dict) or "name" not in ne:
                    continue
                name_key = ne["name"].lower().strip()
                # Ensure type is domestic or international
                ne["type"] = ne.get("type", "domestic").lower()
                if ne["type"] not in ["domestic", "international"]:
                    ne["type"] = "domestic"
                
                # Check date formats
                for dkey in ["start_date", "end_date"]:
                    val = ne.get(dkey, "")
                    if not isinstance(val, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', val):
                        ne[dkey] = datetime.date.today().isoformat()
                
                event_map[name_key] = ne
                
            # Filter expired events
            today_str = datetime.date.today().isoformat()
            updated_events = [e for e in event_map.values() if e.get("end_date", "") >= today_str]
            
            try:
                # Ensure data folder exists
                os.makedirs(os.path.dirname(self.active_events_file), exist_ok=True)
                with open(self.active_events_file, 'w', encoding='utf-8') as f:
                    json.dump(updated_events, f, indent=4, ensure_ascii=False)
                logging.info(f"Updated active_events.json with {len(updated_events)} active events.")
            except Exception as e:
                logging.error(f"Error writing active_events.json: {e}")

        # 2. Update shown tools
        new_tools = state_data.get("tools", [])
        if isinstance(new_tools, list) and new_tools:
            shown_tools = []
            if os.path.exists(self.shown_tools_file):
                try:
                    with open(self.shown_tools_file, 'r', encoding='utf-8') as f:
                        shown_tools = json.load(f)
                except Exception as e:
                    logging.error(f"Error reading shown_tools.json: {e}")
                    
            existing_tools_lower = {t.lower().strip() for t in shown_tools if isinstance(t, str)}
            for nt in new_tools:
                if isinstance(nt, str) and nt.strip():
                    nt_strip = nt.strip()
                    if nt_strip.lower() not in existing_tools_lower:
                        shown_tools.append(nt_strip)
                        existing_tools_lower.add(nt_strip.lower())
                        
            try:
                # Ensure data folder exists
                os.makedirs(os.path.dirname(self.shown_tools_file), exist_ok=True)
                with open(self.shown_tools_file, 'w', encoding='utf-8') as f:
                    json.dump(shown_tools, f, indent=4, ensure_ascii=False)
                logging.info(f"Updated shown_tools.json. Total shown tools: {len(shown_tools)}")
            except Exception as e:
                logging.error(f"Error writing shown_tools.json: {e}")

        # 3. Update shown market intel URLs
        new_intel = state_data.get("intel_urls", [])
        if isinstance(new_intel, list) and new_intel:
            shown_intel = []
            if os.path.exists(self.shown_intel_file):
                try:
                    with open(self.shown_intel_file, 'r', encoding='utf-8') as f:
                        shown_intel = json.load(f)
                except Exception as e:
                    logging.error(f"Error reading shown_intel.json: {e}")
                    
            existing_intel_lower = {i.lower().strip() for i in shown_intel if isinstance(i, str)}
            for ni in new_intel:
                if isinstance(ni, str) and ni.strip():
                    ni_strip = ni.strip()
                    if ni_strip.lower() not in existing_intel_lower:
                        shown_intel.append(ni_strip)
                        existing_intel_lower.add(ni_strip.lower())
                        
            try:
                # Ensure data folder exists
                os.makedirs(os.path.dirname(self.shown_intel_file), exist_ok=True)
                with open(self.shown_intel_file, 'w', encoding='utf-8') as f:
                    json.dump(shown_intel, f, indent=4, ensure_ascii=False)
                logging.info(f"Updated shown_intel.json. Total shown intel URLs: {len(shown_intel)}")
            except Exception as e:
                logging.error(f"Error writing shown_intel.json: {e}")

    # ==========================================
    # CORE PIPELINE EXECUTION
    # ==========================================
    def run_pipeline(self):
        logging.info("=" * 60)
        logging.info("SUPAB EXPORTS - CLOUD DAILY DIGEST PIPELINE STARTED")
        logging.info("=" * 60)
        
        # 1. Scrape configured target sites (DGFT, etc.)
        logging.info("[Step 1/4] Deep Scanning Target Regulatory Sites...")
        crawled_data = self.crawl_target_sites()
        logging.info(f"Scraped {len(crawled_data)} chars of raw text from regulatory targets.")
        
        # 2. Execute broad web search & handle checking
        logging.info("[Step 2/4] Conducting 24-Hour Web Intelligence Searches...")
        searched_data = self.run_broad_search()
        logging.info(f"Retrieved {len(searched_data)} chars of broad web search results.")
        
        # Save aggregated raw files for historical tracking (just like monitor.py did)
        today = datetime.date.today().isoformat()
        daily_dir = os.path.join(self.base_dir, 'data', today)
        os.makedirs(daily_dir, exist_ok=True)
        
        with open(os.path.join(daily_dir, "raw_scraped_targets.txt"), "w", encoding="utf-8") as f:
            f.write(crawled_data)
        with open(os.path.join(daily_dir, "raw_searched_intel.txt"), "w", encoding="utf-8") as f:
            f.write(searched_data)
        
        # 3. Call AI to synthesize and filter
        logging.info("[Step 3/4] Synthesizing Intelligence with Gemini API...")
        digest_text = self.generate_digest_ai(crawled_data, searched_data)
        
        # Extract and update state
        clean_digest, state_data = self.parse_state_update(digest_text)
        if state_data:
            self.update_state_files(state_data)
        
        # Save generated digest markdown file
        digest_file = os.path.join(daily_dir, "digest.md")
        with open(digest_file, "w", encoding="utf-8") as f:
            f.write(clean_digest)
        logging.info(f"Saved completed markdown digest to {digest_file}")
        
        # 4. Email report
        logging.info("[Step 4/4] Delivering Digest Briefing Email...")
        self.send_email(clean_digest)
        
        logging.info("=" * 60)
        logging.info("SUPAB EXPORTS - CLOUD DAILY DIGEST PIPELINE COMPLETED")
        logging.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Supab Exports Daily Digest Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Compile prompt and save files locally without calling Gemini/SMTP")
    parser.add_argument("--email-test", action="store_true", help="Test SMTP email delivery with a test body")
    args = parser.parse_args()
    
    if args.email_test:
        # SMTP validation routine
        pipeline = DailyDigestPipeline(email_test=True)
        test_body = """
Good morning Yogesh 🙏
This is a test notification to verify SMTP email setup.

📋 WHAT MATTERS TODAY
- Verification Test: SMTP configuration working properly.
  Why it matters: Confirms that email triggers correctly from the cloud.
  Priority: Important
"""
        pipeline.send_email(test_body)
    else:
        pipeline = DailyDigestPipeline(dry_run=args.dry_run)
        pipeline.run_pipeline()
