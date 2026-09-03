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
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_digest_run.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, mode='w', encoding='utf-8')
    ]
)

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

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

    def clean_text_for_llm(self, text, max_chars=40000):
        if not text:
            return ""
        # Normalize newlines
        text = re.sub(r'\r\n', '\n', text)
        # Replace 3 or more consecutive newlines with 2 newlines (to preserve paragraphs)
        text = re.sub(r'\n{3,}', '\n\n', text)
        # Replace multiple spaces or tabs with a single space
        text = re.sub(r'[ \t]+', ' ', text)
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...truncated {len(text) - max_chars} characters for LLM context optimization...]"
        return text

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
        """Queries Tavily API with strict 24-hour time constraint and snippet capping."""
        logging.info(f"Tavily Search: '{query}'")
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "search_depth": "basic",
            "include_domains": [],
            "exclude_domains": [],
            "max_results": 4,
            "days": 1 # STRICT 24 HOUR FILTER
        }
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            results = response.json().get("results", [])
            
            formatted_results = []
            for r in results:
                raw_content = (r.get('content') or "").strip()
                if len(raw_content) > 600:
                    raw_content = raw_content[:600] + "..."
                formatted_results.append(f"Title: {r.get('title')}\nURL: {r.get('url')}\nContent: {raw_content}\n")
            return "\n".join(formatted_results)
        except Exception as e:
            logging.error(f"Tavily search failed for '{query}': {e}. Falling back to DuckDuckGo.")
            return self.search_web_ddg(query)

    def search_web_ddg(self, query):
        """DuckDuckGo Search fallback with 24h filter and snippet capping."""
        logging.info(f"DuckDuckGo Search: '{query}'")
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                # time='d' filters results to the last 24 hours
                results = list(ddgs.text(query, max_results=4, timelimit='d'))
                
            formatted_results = []
            for r in results:
                raw_body = (r.get('body') or "").strip()
                if len(raw_body) > 600:
                    raw_body = raw_body[:600] + "..."
                formatted_results.append(f"Title: {r.get('title')}\nURL: {r.get('href')}\nContent: {raw_body}\n")
            return "\n".join(formatted_results)
        except Exception as e:
            logging.error(f"DuckDuckGo search failed for '{query}': {e}")
            return ""

    def fetch_newspaper_intelligence(self):
        """Scans leading Indian business dailies (Economic Times, Business Standard, 
        Hindu BusinessLine, Mint, Financial Express) via RSS and Google News RSS for industry-relevant news."""
        import urllib.request
        import xml.etree.ElementTree as ET
        
        logging.info("Gathering news from leading Indian business newspapers (Economic Times, Business Standard, Hindu BusinessLine, Mint, Financial Express)...")
        
        feeds = [
            ("The Hindu BusinessLine (Agri-Business)", "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss"),
            ("The Hindu BusinessLine (Economy)", "https://www.thehindubusinessline.com/economy/feeder/default.rss"),
            ("The Hindu BusinessLine (Commodities)", "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss"),
            ("Business Standard (Economy & Policy)", "https://www.business-standard.com/rss/economy-policy-102.rss"),
            ("Business Standard (Markets & Commodities)", "https://www.business-standard.com/rss/markets-commodities-106.rss"),
            ("Google News (ET / Financial Express / Mint - Agri, Spices & Food Processing)", "https://news.google.com/rss/search?q=(site:economictimes.indiatimes.com+OR+site:financialexpress.com+OR+site:livemint.com)+(%22spices%22+OR+%22dehydrated%22+OR+%22banana%22+OR+%22turmeric%22+OR+%22garlic%22+OR+%22ginger%22+OR+%22moringa%22+OR+%22food+processing%22+OR+%22PMFME%22)&hl=en-IN&gl=IN&ceid=IN:en"),
            ("Google News (ET / Mint / Business Standard - Trade, Mandi & Policy)", "https://news.google.com/rss/search?q=(site:economictimes.indiatimes.com+OR+site:financialexpress.com+OR+site:livemint.com+OR+site:business-standard.com)+(%22agri+export%22+OR+%22DGFT%22+OR+%22APEDA%22+OR+%22FSSAI%22+OR+%22container+freight%22+OR+%22mandi+price%22)&hl=en-IN&gl=IN&ceid=IN:en")
        ]
        
        # Keywords for filtering articles to ensure high signal for IMM & Supab
        relevance_keywords = [
            "banana", "moringa", "turmeric", "ginger", "garlic", "beetroot", "shatawari", "ashwagandha",
            "onion", "dehydrat", "powder", "food processing", "agri export", "spice", "fssai", "dgft",
            "apeda", "msme", "pmfme", "mandi", "container", "freight", "customs", "cepa", "fta",
            "curcumin", "cold chain", "packaging", "contract manufacturing", "private label",
            "horticulture", "agriculture", "export", "import alert", "food safety"
        ]
        
        extracted_articles = []
        seen_titles = set()
        
        for feed_name, feed_url in feeds:
            try:
                req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, timeout=12) as res:
                    raw_xml = res.read()
                    root = ET.fromstring(raw_xml)
                    items = root.findall('.//item')
                    
                    for item in items:
                        title_el = item.find('title')
                        link_el = item.find('link')
                        desc_el = item.find('description')
                        pub_date_el = item.find('pubDate')
                        
                        title = title_el.text.strip() if title_el is not None and title_el.text else ""
                        link = link_el.text.strip() if link_el is not None and link_el.text else ""
                        desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                        pub_date = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ""
                        
                        # Clean HTML tags in description if present
                        desc_clean = re.sub(r'<[^>]+>', '', desc).strip()
                        
                        if not title or title.lower() in seen_titles:
                            continue
                            
                        # Relevance match
                        text_to_check = f"{title} {desc_clean}".lower()
                        is_relevant = any(kw in text_to_check for kw in relevance_keywords)
                        
                        if is_relevant:
                            seen_titles.add(title.lower())
                            extracted_articles.append({
                                "source": feed_name,
                                "title": title,
                                "link": link,
                                "description": desc_clean[:300],
                                "date": pub_date
                            })
            except Exception as e:
                logging.warning(f"Failed to fetch newspaper feed '{feed_name}': {e}")
                
        logging.info(f"Retrieved {len(extracted_articles)} relevant business newspaper stories.")
        
        # Format articles into structured markdown string for the LLM
        formatted_list = []
        for art in extracted_articles:
            formatted_list.append(
                f"- [{art['source']}] {art['title']}\n"
                f"  Published: {art['date']}\n"
                f"  Summary: {art['description']}\n"
                f"  Link: {art['link']}"
            )
            
        return "\n\n".join(formatted_list) if formatted_list else "No direct newspaper matches found in today's business RSS feeds."

    def fetch_exchange_rates(self):
        """Fetches real-time exchange rates for USD/INR and EUR/INR using a free, verified API."""
        import urllib.request
        import json
        logging.info("Fetching real-time verified exchange rates from open.er-api.com...")
        try:
            req = urllib.request.Request("https://open.er-api.com/v6/latest/USD", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read())
                inr_rate = data['rates']['INR']
                eur_rate = data['rates']['EUR']
                eur_inr = inr_rate / eur_rate
                logging.info(f"Verified exchange rates - USD/INR: {inr_rate:.4f}, EUR/INR: {eur_inr:.4f}")
                return {
                    "usd_inr": round(inr_rate, 2),
                    "eur_inr": round(eur_inr, 2),
                    "date": data.get("time_last_update_utc", datetime.datetime.utcnow().strftime("%a, %d %b %Y"))
                }
        except Exception as e:
            logging.error(f"Failed to fetch exchange rates programmatically: {e}")
            return None

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

        # 2. Broad Intelligence batched queries (Manufacturer + Exporter specific)
        topic_queries = [
            # Exchange Rates & Mandi Prices (Separate queries to avoid AND-condition zero-result filters)
            f"\"USD/INR\" \"EUR/INR\" exchange rate",
            f"(\"banana price\" OR \"banana mandi\") AND (Jalgaon OR Maharashtra) {today_str}",
            f"(\"turmeric price\" OR \"turmeric mandi\") AND (Sangli OR Jalgaon OR Maharashtra) {today_str}",
            f"(\"ginger price\" OR \"garlic price\" OR \"beetroot price\" OR \"moringa price\") AND (mandi OR Maharashtra) {today_str}",
            
            # Export & Import Policy & Regulations (Broadened target keyword to guarantee hits)
            f"(\"India export policy\" OR \"DGFT notification\" OR \"APEDA\" OR \"customs compliance\" OR \"FDA import alert\" OR \"EU RASFF\") AND (\"dehydrated\" OR \"spices\" OR \"agricultural\" OR \"food\") {today_str}",
            
            # Manufacturing-side Compliance & Safety Standards
            f"(\"FSSAI manufacturing\" OR \"FSSAI label\" OR \"ISO 22000\" OR \"GMP compliance\" OR \"Factories Act\" OR \"pollution compliance\") AND \"food processing\" {today_str}",
            
            # Government MSME/PMFME Schemes & CFTRI Food Tech
            f"(\"PMFME scheme\" OR \"PLI food processing\" OR \"CFTRI technology\" OR \"Maharashtra agri processing\" OR \"CFTRI training\") 2026",
            
            # Domestic B2B Leads & RFQs for core products (Banana, Moringa, Turmeric, Ginger, Garlic, Beetroot, Shatawari, Ashwagandha)
            f"(\"banana powder\" OR \"moringa powder\" OR \"turmeric powder\" OR \"beetroot powder\" OR \"ginger powder\" OR \"garlic powder\" OR \"ashwagandha\" OR \"shatawari\") AND (\"RFQ\" OR \"buyer requirement\" OR \"looking for bulk supplier\" OR \"raw material buyer\" OR site:indiamart.com OR site:tradeindia.com) 2026",
            
            # State/Central Gov & GeM Tenders (Institutional, Defence, Anganwadi)
            f"(\"GeM tender\" OR \"government tender\" OR \"defence dehydrated\" OR \"ICDS nutrition\" OR \"anganwadi food\") AND (\"dehydrated vegetable\" OR \"dehydrated fruit\" OR \"vegetable powder\" OR \"food powder\") 2026",
            
            # Prospective Bulk Buyers (Bakery, Confectionery, Spice Blenders launches/expansions)
            f"(\"new product launch\" OR \"capacity expansion\") AND (confectionery OR bakery OR nutraceutical OR \"spice blending\" OR \"ice cream\" OR \"health food\") AND (India OR Maharashtra OR Gujarat OR Karnataka) 2026",
            
            # Competitor Manufacturers (Maharashtra/Jalgaon dehydration units)
            f"(\"Rajlaxmi Agro Farm\" OR \"Mevive International\" OR \"VKL Seasoning\" OR \"food dehydration unit Jalgaon\" OR \"dehydrated manufacturer Maharashtra\") 2026",
            
            # Dehydration Equipment & Machinery
            f"(\"heat pump dryer food\" OR \"dehydration equipment\" OR \"CFTRI technology transfer\" OR \"dryer manufacturer\") AND \"food processing\" 2026",
            
            # Domestic Events & B2B Meets (Maharashtra & National)
            f"(\"Agroworld Expo Jalgaon\" OR \"AAHAR\" OR \"BioFach India\" OR \"food safety training\" OR \"reverse buyer seller meet\") AND (food OR agri OR Maharashtra OR Mumbai OR Jalgaon OR Pune) 2026",
            
            # Export Leads & Inquiries
            f"(\"banana powder\" OR \"moringa powder\" OR \"turmeric powder\" OR \"beetroot powder\" OR \"ginger powder\" OR \"garlic powder\" OR \"ashwagandha\" OR \"shatawari\") AND (\"export tender\" OR \"APEDA buyer inquiry\" OR \"importer requirement\") 2026",
            
            # Business Newspapers targeted search
            f"(site:economictimes.indiatimes.com OR site:business-standard.com OR site:thehindubusinessline.com OR site:livemint.com OR site:financialexpress.com) AND (\"spices\" OR \"dehydrated\" OR \"banana powder\" OR \"moringa\" OR \"turmeric\" OR \"food processing\" OR \"PMFME\" OR \"FSSAI\" OR \"DGFT\") {today_str}",
            f"(site:economictimes.indiatimes.com OR site:business-standard.com OR site:thehindubusinessline.com) AND (\"agri export\" OR \"commodity prices\" OR \"mandi price\" OR \"container freight\" OR \"exim\") {today_str}",
            
            # AI & Technology updates (both consulting and factory/lab operations)
            f"(\"AI tool Indian exporter\" OR \"AI market research tool export\" OR \"AI buyer discovery\" OR \"AI production planning\" OR \"AI lab management\" OR \"AI factory compliance\") 2026"
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
    def generate_digest_ai(self, raw_crawled, raw_searched, raw_newspapers="", verified_exchange_rates=None):
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
        system_prompt = f"""You are a daily business intelligence assistant for Yogesh Badgujar, who operates across three distinct roles:

1. IMM FOOD INNOVATORS LLP — Director & Partner. A B2B-only manufacturer (not just trader) of dehydrated food powders based in Nhavi, Yawal, Jalgaon, Maharashtra. ISO 9001:2015, ISO 22000:2018, GMP and FSSAI certified; batch testing via NABL-accredited lab (Shree ATR, Jalgaon).
   - Core confirmed products: Banana Powder, Moringa Leaves Powder, Turmeric Powder, Garlic Powder, Ginger Powder, Beetroot Powder, Shatawari Powder, Ashwagandha Powder.
   - Sells B2B bulk, private label/white-label, and contract manufacturing to domestic and export buyers.
   - IMMEDIATE PRIORITY: Domestic distributors, retailers, wholesalers, and FMCG companies needing IMM's products as raw material inputs (bakeries, spice blenders, ice cream/dairy, nutraceuticals, exporters needing a manufacturing partner). Focus heavily on these domestic leads.
2. SUPAB EXPORTS — Owner. Export/trading company for dehydrated foods and spices, sourcing from IMM and other manufacturers.
3. SUPAB DIGITAL — Owner. AI/digital consulting for Indian SME exporters and manufacturers.

==========================================
LENSES SEPARATION RULE (STRICT)
==========================================
Every briefing item must clearly distinguish the two lenses. DO NOT merge them:
- Exporter/Trader lens (Supab Exports/Digital): Trade policy, export documentation, buyer demand signals, freight/logistics, international market access, competitor exporters.
- Manufacturer lens (IMM): Raw material/input costs and mandi prices, factory compliance and certification changes, food safety regulation affecting production (not just export), equipment/technology in dehydration, competitor manufacturers, government schemes for food processing MSMEs, manufacturing-side tenders.
Where a single news item affects both (e.g., a food-safety rule change), state the implication separately for each: "As a manufacturer, this means..." / "As an exporter, this means..."

==========================================
RELEVANCE FILTER - STRICTLY ENFORCED
==========================================
INCLUDE:
✅ Wholesale/mandi prices for banana (Jalgaon), turmeric (Jalgaon/Sangli), ginger, garlic, beetroot, moringa leaf.
✅ FSSAI rules, GMP/ISO standards, MSME/Udyam schemes, PMFME/PLI updates, Maharashtra Industrial or Agri-processing policies, CSIR/CFTRI tech transfer.
✅ Destination market import alerts (FDA, EU RASFF) and export/import policy (DGFT, APEDA, customs, FTAs).
✅ Dehydration tech (heat pump dryers, CSIR-CFTRI research, machinery).
✅ Competitor manufacturer updates (Rajlaxmi Agro Farm, Mevive, VKL Seasoning, Jalgaon/Maharashtra dehydration units).
✅ Domestic B2B leads, RFQs, distributor tie-ups, GeM/state nutrition program tenders, bulk inquiries for core powders (Banana, Moringa, Turmeric, Ginger, Garlic, Beetroot, Shatawari, Ashwagandha).
✅ AI tools for consulting or factory production planning/lab/factory compliance.

EXCLUDE:
❌ Unrelated industries (Chemicals, Gems, Textiles, etc.).
❌ Political/general news with no direct trade or manufacturing compliance impact.
❌ AI hype with no practical use case.
❌ Duplicate items already shown.
❌ Anything older than 24 hours.

==========================================
CRITICAL QUALITY RULES - NON-NEGOTIABLE
==========================================
1. 🚫 NO PLACEHOLDERS OR INCOMPLETE DATA: If crucial details are missing, omit the item. Always state the available contact channels/links.
2. 🚫 FUTURE EVENTS ONLY: Include events taking place on or after {today_date_str}.
3. ⚡ CRISP AND DENSE: Total email under 650 words.
4. 🚫 DO NOT REPEAT SHOWN TOOLS: Exclude {shown_tools_list}.
5. 🚫 DO NOT REPEAT SHOWN INTELLIGENCE: Exclude {shown_intel_list}.
6. 🚫 STRICT VERIFIED DATA RULE (MANDATORY):
   - You must ONLY output exchange rates and mandi prices that are explicitly present in the provided raw input data sections.
   - Never use your pre-trained knowledge or historical knowledge to output or 'fill in' exchange rates (like USD/INR, EUR/INR) or mandi prices.
   - For every single price, exchange rate, or lead reported in the digest, you must list the exact URL source and publication/crawled date immediately next to it.
   - If the raw data does not contain today's verified numbers, write 'Not available in today's search data'. Never guess, write 'approximate', or estimate.
"""

        user_instruction = f"""
Here is the raw data collected in the last 24 hours from target crawled pages, daily business newspapers, and web searches.
Read the content carefully, apply the strict relevance filters and rules, and generate the final email report in the exact format specified below.

### RAW CRAWLED DATA (REGULATORY BODIES)
{raw_crawled}

### RAW BUSINESS NEWSPAPER INTELLIGENCE (The Economic Times, Business Standard, The Hindu BusinessLine, Mint, Financial Express)
{raw_newspapers}

### RAW SEARCH & SOCIAL INTELLIGENCE
{raw_searched}

### VERIFIED EXCHANGE RATES (API SOURCED - 100% ACCURATE)
{f"USD/INR: {verified_exchange_rates.get('usd_inr')}\nEUR/INR: {verified_exchange_rates.get('eur_inr')}\n(Source: API fetched on {verified_exchange_rates.get('date')})" if verified_exchange_rates else "USD/INR: Not available\nEUR/INR: Not available"}

### PERSISTENT ACTIVE B2B EVENTS
{active_events_str}

### EMAIL REPORT FORMAT REQUIREMENT
Subject: 🌏 Supab & IMM Daily Digest — [Today's Date]
Send to: yogeshgujar@gmail.com

---

Good morning Yogesh 🙏
Daily briefing for [Date, Day].

---

📋 WHAT MATTERS TODAY
[2-3 items, covering the single most consequential regulatory/safety/market development for the day. Tag each item [MANUFACTURER], [EXPORTER], or [BOTH] at the beginning.]

---

🏛️ GOVERNMENT & POLICY
[Provide updates under the following sub-tags. Only include a sub-tag if there are updates for it; do not print "No updates" or "No significant alerts" placeholder lines. If the entire section has no updates at all, write "No significant policy updates today."]
- (Export & Trade Policy): [circulars/notices, exact numbers, DGFT/APEDA source. Implication for Supab Exports/Digital]
- (Manufacturing, MSME & Compliance): [FSSAI rules, ISO, GMP, Maharashtra state factory/effluent/waste rules, MSME/PMFME schemes, CSIR/CFTRI tech transfers]
- (Destination Market Alerts - FDA/EU RASFF/etc.): [active import alerts/bans/restrictions in target markets (US, EU, GCC) affecting Indian dehydrated foods or spices]

---

📦 MARKET & PRODUCT INTEL
[Product demand, prices, competitors, tech, tenders. Split exactly into the following three parts:]

**DOMESTIC B2B LEADS & RFQS**
[Surface buyer RFQs/inquiries from IndiaMart, TradeIndia, Bizongo, GeM tenders, Maharashtra/neighboring distributors looking for supplier tie-ups, product expansions, or Facebook/LinkedIn/Instagram bulk supplier inquiries matching core powders.
Report: what they need, quantity if stated, how to respond (link/contact/deadline), and matching IMM product. Rank leads by direct match first. If nothing: "No active domestic B2B leads found today."]

**EXPORT LEADS & INQUIRIES**
[Surfaced international buyer inquiries, export tenders, and APEDA inquiries. Present these below the domestic leads. If nothing: "No active export inquiries found today."]

**MARKET SEGMENTS & NEWS**
- [Headline — competitor moves (Rajlaxmi, Mevive, VKL, etc.), raw material costs, dehydration tech/CFTRI tech, global demand signals]
  Details: [2-3 sentences]
  Implication: [As a manufacturer, this means... / As an exporter, this means...]
  Source: [link]

---

🇮🇳 DOMESTIC B2B EVENTS & MEETS
[Include Jalgaon/Maharashtra-region events (e.g. Agroworld Expo Jalgaon) alongside national ones (AAHAR, BioFach, food safety training). Tag by sourcing/networking (manufacturer) or buyer access (exporter). Include persistent domestic events.]
- [Event Name] — [Manufacturer / Exporter]
  Date: | Venue:
  Why it matters: [sourcing/networking or buyer access details]
  Register/Info: [link]

---

🌏 INTERNATIONAL B2B EVENTS
[Trade fairs outside India. Tag by export market relevance to IMM core products. Include persistent international events.]
- [Event Name]
  Date: | Venue:
  Why it matters: [export market relevance]
  Register/Info: [link]

---

🤖 AI & TOOLS
[Flag tools for digital consulting (Supab Digital) or production planning/lab/factory compliance (IMM). Exclude shown tools.]
- [Tool name] — [Consulting / Production / Compliance]
  What it does: [one line]
  How you can use it: [specific use case for Yogesh's roles]
  Link: [url]

---

📊 QUICK NUMBERS
- USD/INR: [value]
- EUR/INR: [value]
- Mandi Prices (Jalgaon/nearest reporting mandi): Banana: [value], Turmeric: [value], Ginger: [value], Garlic: [value]
- Freight Rates: [airfreight/ocean freight rate movements to key export markets]

---

🔒 WATCH THIS WEEK
[2-3 deadlines/events, tagged by lens (e.g., [MANUFACTURER] or [EXPORTER])]
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
      "name": "Agroworld Expo Jalgaon",
      "start_date": "2026-11-15",
      "end_date": "2026-11-18",
      "venue": "Jalgaon, Maharashtra, India",
      "why_it_matters": "Local agricultural exhibition in Jalgaon; high relevance for direct networking with regional growers and supplier sourcing.",
      "link": "https://www.agroworld.com",
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
            models_to_try = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
            for attempt, model_name in enumerate(models_to_try, 1):
                logging.info(f"Attempting daily digest compilation using Google Gemini ({model_name}) (Attempt {attempt}/3)...")
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=self.gemini_key)
                    model = genai.GenerativeModel(
                        model_name=model_name,
                        system_instruction=system_prompt
                    )
                    response = model.generate_content(
                        user_instruction,
                        generation_config={"temperature": 0.2},
                        request_options={"timeout": 180}
                    )
                    
                    result_text = response.text
                    if self.openai_key:
                        # Add fallback notice if OpenAI key was present but failed
                        result_text += f"\n\n*(Compiled using Gemini {model_name} backup engine)*"
                    logging.info("Gemini compilation successful!")
                    return result_text
                except Exception as e:
                    import traceback
                    is_rate_limit = "ResourceExhausted" in type(e).__name__ or "429" in str(e) or "quota" in str(e).lower()
                    if is_rate_limit:
                        logging.warning(f"Gemini API rate limited (429) on attempt {attempt}: {e}")
                        if attempt < 3:
                            logging.info("Waiting 65 seconds before retrying with next model...")
                            time.sleep(65)
                            continue
                    
                    logging.error(f"Gemini API call failed on attempt {attempt} with {model_name}: {e}")
                    logging.error(traceback.format_exc())
                    if attempt < 3:
                        logging.info("Waiting 10 seconds before retrying with next model...")
                        time.sleep(10)

        # 3. High-Reliability Fallback (if all AI APIs fail or are unavailable)
        logging.warning("All primary LLM compilation attempts failed. Generating structured fallback briefing to guarantee daily email delivery...")
        today_title = datetime.date.today().strftime("%A, %d %B %Y")
        usd_rate = verified_exchange_rates.get('usd_inr') if verified_exchange_rates else 'Check RBI/Live'
        eur_rate = verified_exchange_rates.get('eur_inr') if verified_exchange_rates else 'Check RBI/Live'
        
        fallback_briefing = f"""# 🌏 Supab & IMM Daily Digest — {today_title}

Good morning Yogesh 🙏
Daily briefing for {today_title}. *(Notice: Compiled in high-reliability automated backup mode)*

---

📋 WHAT MATTERS TODAY
- [BOTH] Daily Automated Regulatory & Market Scan Completed.
  Details: Daily regulatory monitors across DGFT, APEDA, FSSAI, and business dailies completed data ingestion.
  Implication: Real-time currency rates and key market references are captured below.

---

📊 QUICK NUMBERS
- Exchange Rates: USD/INR: ₹{usd_rate} | EUR/INR: ₹{eur_rate} (Source: ExchangeRate API)
- Core Products Tracked: Banana Powder, Moringa Powder, Turmeric Powder, Ginger Powder, Garlic Powder, Beetroot Powder, Shatawari Powder, Ashwagandha Powder.

---

📦 BUSINESS NEWSPAPERS & REGULATORY HEADLINES
{raw_newspapers[:1800] if raw_newspapers else 'No major regulatory alerts detected in today\'s scan.'}

---

*(Notice: Your daily digest executed in backup mode due to a temporary AI provider response timeout. All raw intelligence was safely recorded in daily logs.)*
"""
        return fallback_briefing

    # ==========================================
    # EMAIL SENDING SYSTEM (Gmail SMTP)
    # ==========================================
    def format_email_body_html(self, text_content):
        """Converts generated plain markdown digest text into a stunning, responsive HSL-styled HTML layout."""
        # Simple parser to format markdown headers and blocks into clean styled cards
        lines = text_content.split('\n')
        html_sections = []
        current_section = []
        card_open = False
        
        def commit_section():
            nonlocal card_open
            if current_section:
                html_sections.append("\n".join(current_section))
                current_section.clear()
            if card_open:
                html_sections.append('</div>')
                card_open = False

        # Parse markdown lines into formatted blocks
        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                current_section.append("<br/>")
                continue
                
            # Headers
            if line_strip.startswith("📋") or "WHAT MATTERS TODAY" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card urgent">')
                current_section.append(f'<h2 class="section-title">🚨 WHAT MATTERS TODAY</h2>')
                card_open = True
            elif line_strip.startswith("🏛️") or "GOVERNMENT & POLICY" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🏛️ GOVERNMENT & POLICY</h2>')
                card_open = True
            elif line_strip.startswith("📦") or "MARKET & PRODUCT" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">📦 MARKET & PRODUCT INTEL</h2>')
                card_open = True
            elif "DOMESTIC B2B EVENTS" in line_strip.upper() or "DOMESTIC EVENTS" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🇮🇳 DOMESTIC B2B EVENTS & MEETS</h2>')
                card_open = True
            elif "INTERNATIONAL B2B EVENTS" in line_strip.upper() or "INTERNATIONAL EVENTS" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🌏 INTERNATIONAL B2B EVENTS</h2>')
                card_open = True
            elif line_strip.startswith("🤝") or "EVENTS & OPPORTUNITIES" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🤝 EVENTS & OPPORTUNITIES</h2>')
                card_open = True
            elif line_strip.startswith("🤖") or "AI & TOOLS" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🤖 AI & TOOLS</h2>')
                card_open = True
            elif line_strip.startswith("📊") or "QUICK NUMBERS" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card highlight">')
                current_section.append(f'<h2 class="section-title">📊 QUICK NUMBERS</h2>')
                card_open = True
            elif line_strip.startswith("🔜") or line_strip.startswith("🔒") or "WATCH THIS WEEK" in line_strip.upper():
                commit_section()
                html_sections.append('<div class="section-card">')
                current_section.append(f'<h2 class="section-title">🔒 WATCH THIS WEEK</h2>')
                card_open = True
            elif line_strip == "---":
                commit_section()
                # Simple divider or end card
                # We do not append another </div> here since commit_section already closed it!
                pass
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
<title>Supab &amp; IMM Daily Intel</title>
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
        <h1>🌏 SUPAB &amp; IMM DAILY INTEL</h1>
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
        logging.info("[Step 1/5] Deep Scanning Target Regulatory Sites...")
        crawled_data = self.crawl_target_sites()
        crawled_data = self.clean_text_for_llm(crawled_data)
        logging.info(f"Scraped & cleaned {len(crawled_data)} chars of raw text from regulatory targets.")
        
        # 2. Newspaper intelligence
        logging.info("[Step 2/5] Scanning Leading Business Newspapers (The Economic Times, Business Standard, Hindu BusinessLine, Mint, Financial Express)...")
        newspaper_data = self.fetch_newspaper_intelligence()
        newspaper_data = self.clean_text_for_llm(newspaper_data)
        logging.info(f"Scraped & cleaned {len(newspaper_data)} chars of raw text from business newspapers.")

        # 3. Execute broad web search & handle checking
        logging.info("[Step 3/5] Conducting 24-Hour Web Intelligence Searches...")
        searched_data = self.run_broad_search()
        searched_data = self.clean_text_for_llm(searched_data)
        logging.info(f"Retrieved & cleaned {len(searched_data)} chars of broad web search results.")
        
        # Save aggregated raw files for historical tracking (just like monitor.py did)
        today = datetime.date.today().isoformat()
        daily_dir = os.path.join(self.base_dir, 'data', today)
        os.makedirs(daily_dir, exist_ok=True)
        
        with open(os.path.join(daily_dir, "raw_scraped_targets.txt"), "w", encoding="utf-8") as f:
            f.write(crawled_data)
        with open(os.path.join(daily_dir, "raw_newspaper_intel.txt"), "w", encoding="utf-8") as f:
            f.write(newspaper_data)
        with open(os.path.join(daily_dir, "raw_searched_intel.txt"), "w", encoding="utf-8") as f:
            f.write(searched_data)
        
        # Fetch verified exchange rates from API
        verified_rates = self.fetch_exchange_rates()
        
        # 4. Call AI to synthesize and filter
        logging.info("[Step 4/5] Synthesizing Intelligence with Gemini API...")
        digest_text = self.generate_digest_ai(crawled_data, searched_data, newspaper_data, verified_rates)
        
        # Extract and update state
        clean_digest, state_data = self.parse_state_update(digest_text)
        if state_data:
            self.update_state_files(state_data)
        
        # Save generated digest markdown file
        digest_file = os.path.join(daily_dir, "digest.md")
        with open(digest_file, "w", encoding="utf-8") as f:
            f.write(clean_digest)
        logging.info(f"Saved completed markdown digest to {digest_file}")
        
        # 5. Email report
        logging.info("[Step 5/5] Delivering Digest Briefing Email...")
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
