import os
import datetime
import requests
import logging
import time
import argparse
import schedule
import json
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import trafilatura

# --- Logging Setup ---
def setup_logging(log_file='monitor.log'):
    """Configures logging to both console and file."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

class DailyMonitor:
    def __init__(self, sites_file='sites.txt', data_dir='data', template_file='DAILY_DIGEST_PROMPT_TEMPLATE.md', context_file='user_context.json'):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sites_file = os.path.join(self.base_dir, sites_file)
        self.data_dir = os.path.join(self.base_dir, data_dir)
        self.template_file = os.path.join(self.base_dir, template_file)
        self.context_file = os.path.join(self.base_dir, context_file)
        
        # Load user context if available
        self.user_context = {}
        if os.path.exists(self.context_file):
            with open(self.context_file, 'r', encoding='utf-8') as f:
                self.user_context = json.load(f)
        
        # Session with retries
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        self.session.mount('http://', HTTPAdapter(max_retries=retries))
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Keywords to identify "Update" pages vs "Static" pages
        self.update_keywords = ['notification', 'circular', 'public notice', 'press release', 'news', 'update', 'latest']
        self.ignore_keywords = ['login', 'register', 'signup', 'apply', 'contact', 'about', 'help', 'search', 'lang=', 'javascript:']

    def get_today_dir(self):
        """Returns the directory for today's data, creating it if needed."""
        today = datetime.date.today().isoformat()
        daily_dir = os.path.join(self.data_dir, today)
        os.makedirs(daily_dir, exist_ok=True)
        return daily_dir, today

    def load_sites(self):
        """Reads URLs from the configuration file."""
        if not os.path.exists(self.sites_file):
            logging.error(f"{self.sites_file} not found.")
            return []
            
        with open(self.sites_file, 'r') as f:
            lines = f.readlines()
        
        sites = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
        return sites

    def can_fetch(self, url):
        """Checks robots.txt for the given URL."""
        parsed_url = urlparse(url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        robots_url = f"{base_url}/robots.txt"
        
        rp = RobotFileParser()
        try:
            rp.set_url(robots_url)
            rp.read()
            return rp.can_fetch(self.session.headers['User-Agent'], url)
        except Exception as e:
            logging.warning(f"Could not check robots.txt for {url}: {e}. Assuming allowed.")
            return True

    def is_link_interesting(self, text, url):
        """Decides if a link is worth following based on keywords."""
        text = text.lower()
        url = url.lower()
        
        # Must match at least one update keyword
        if not any(k in text or k in url for k in self.update_keywords):
            return False
            
        # Must NOT match any ignore keyword
        if any(k in text or k in url for k in self.ignore_keywords):
            return False
            
        return True

    def find_sub_links(self, url):
        """
        Scans values landing page for links to 'Notifications', 'Circulars', etc.
        Returns a list of high-value URLs to scrape.
        """
        if not self.can_fetch(url):
            return []

        logging.info(f"Scanning {url} for sub-sections...")
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            interesting_links = set()
            
            # Add the main page itself first
            interesting_links.add(url)

            for a in soup.find_all('a', href=True):
                href = a['href']
                text = a.get_text(" ", strip=True)
                full_url = urljoin(url, href)
                
                # Filter internal/relevant links
                if self.is_link_interesting(text, full_url):
                    interesting_links.add(full_url)
            
            # Limit to avoiding crawling the whole web
            # Return top 5 most promising links + main page
            return list(interesting_links)[:6] 

        except Exception as e:
            logging.error(f"Failed to scan links for {url}: {e}")
            return [url] # Fallback to just the main URL

    def scrape_url_content(self, url):
        """Fetches and extracts main text using Trafilatura."""
        if not self.can_fetch(url):
            return None

        logging.info(f"Deep scraping: {url}...")
        try:
            time.sleep(2) # Politeness delay
            downloaded = trafilatura.fetch_url(url)
            
            if downloaded:
                text = trafilatura.extract(downloaded)
                return text
            return None
        except Exception as e:
            logging.error(f"Failed to scrape {url}: {e}")
            return None

    def save_content(self, base_url, content_map, daily_dir, today):
        """Saves scraped content from multiple related pages to a single file."""
        domain = urlparse(base_url).netloc.replace('www.', '')
        filename = f"{domain}.txt"
        filepath = os.path.join(daily_dir, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"Source Scanned: {base_url}\n")
                f.write(f"Date: {today}\n")
                
                # Add Context Info if available
                if self.user_context:
                    f.write(f"User Context Match: {', '.join(self.user_context.get('detected_keywords', []))}\n")
                
                f.write("=" * 50 + "\n\n")
                
                for link, text in content_map.items():
                    f.write(f"--- SUB-PAGE: {link} ---\n")
                    f.write(text[:50] + "..." + "\n") # Log preview? No, write full text
                    f.write(text)
                    f.write("\n\n" + "-" * 30 + "\n\n")
                    
            logging.info(f"Saved combined content to {filename}")
        except Exception as e:
            logging.error(f"Failed to save content for {base_url}: {e}")

    def generate_summary_prompt(self, daily_dir):
        """Aggregates all daily files into a single prompt for an LLM."""
        prompt_path = os.path.join(daily_dir, 'daily_digest_prompt.txt')
        
        try:
            with open(prompt_path, 'w', encoding='utf-8') as outfile:
                # Load Template
                if os.path.exists(self.template_file):
                    with open(self.template_file, 'r', encoding='utf-8') as tf:
                        outfile.write(tf.read())
                        
                    # Inject User Context into the prompt dynamically
                    if self.user_context:
                        outfile.write("\n\n### Business Context (Auto-Detected)\n")
                        outfile.write(f"The user operates 'Supab Exports'. Detected focus areas from their website:\n")
                        outfile.write(f"- Keywords: {', '.join(self.user_context.get('detected_keywords', [])[:10])}\n")
                        outfile.write(f"- HS Codes found: {', '.join(self.user_context.get('detected_hs_codes', []))}\n")
                        outfile.write("Use this context to prioritize updates related to these products/codes.\n")

                    outfile.write("\n\n" + "=" * 50 + "\n\n")
                else:
                    outfile.write("Summary Request...\n")
                
                # Append Content
                for filename in os.listdir(daily_dir):
                    if filename.endswith('.txt') and filename != 'daily_digest_prompt.txt':
                        filepath = os.path.join(daily_dir, filename)
                        with open(filepath, 'r', encoding='utf-8') as infile:
                            outfile.write(f"--- START OF CONTENT FROM {filename} ---\n")
                            outfile.write(infile.read())
                            outfile.write(f"\n--- END OF CONTENT FROM {filename} ---\n\n")
            
            logging.info(f"Daily summary prompt generated: {prompt_path}")
            
        except Exception as e:
            logging.error(f"Failed to generate summary prompt: {e}")

    def run(self):
        """Runs the monitor a single time."""
        daily_dir, today = self.get_today_dir()
        sites = self.load_sites()
        
        if not sites:
            logging.warning("No sites found to monitor.")
            return

        logging.info(f"Starting deep monitor for {len(sites)} base sites...")
        
        for base_url in sites:
            # 1. Find sub-links
            links_to_scrape = self.find_sub_links(base_url)
            logging.info(f"Found {len(links_to_scrape)} relevant pages to scrape for {base_url}")
            
            # 2. Scrape each link
            content_map = {}
            for link in links_to_scrape:
                text = self.scrape_url_content(link)
                if text:
                    # Basic smart filter: ignore if it looks like a login page or too short
                    if len(text) < 100 or "login" in text.lower().split()[:20]:
                         logging.info(f"Skipping {link}: Content too short or looks like login.")
                         continue
                    content_map[link] = text
            
            # 3. Save combined content
            if content_map:
                self.save_content(base_url, content_map, daily_dir, today)
        
        self.generate_summary_prompt(daily_dir)

def job():
    monitor = DailyMonitor()
    monitor.run()

if __name__ == "__main__":
    setup_logging()
    
    parser = argparse.ArgumentParser(description='Daily Business Intelligence Monitor')
    parser.add_argument('--interval', type=int, help='Interval in minutes to run the monitor (runs continuously if set)')
    args = parser.parse_args()

    if args.interval:
        logging.info(f"Starting scheduled mode. Running every {args.interval} minutes.")
        schedule.every(args.interval).minutes.do(job)
        job()
        while True:
            schedule.run_pending()
            time.sleep(1)
    else:
        job()
