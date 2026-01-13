import os
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

class DailyMonitor:
    def __init__(self, sites_file='sites.txt', data_dir='data'):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sites_file = os.path.join(self.base_dir, sites_file)
        self.data_dir = os.path.join(self.base_dir, data_dir)
        self.today = datetime.date.today().isoformat()
        
        self.daily_dir = os.path.join(self.data_dir, self.today)
        os.makedirs(self.daily_dir, exist_ok=True)

    def load_sites(self):
        """Reads URLs from the configuration file."""
        if not os.path.exists(self.sites_file):
            print(f"Error: {self.sites_file} not found.")
            return []
            
        with open(self.sites_file, 'r') as f:
            lines = f.readlines()
        
        # Filter out comments and empty lines
        sites = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
        return sites

    def scrape_site(self, url):
        """Fetches and extracts main text from a URL."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            print(f"Fetching {url}...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')

            # Clean up the soup
            for element in soup(["script", "style", "nav", "footer", "iframe", "noscript"]):
                element.decompose()

            # Attempt to find the main content to avoid clutter
            main_content = soup.find('main') or soup.find('article') or soup.find('body')
            
            text = main_content.get_text(separator='\n') if main_content else soup.get_text(separator='\n')
            
            # Clean up whitespace
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            clean_text = '\n'.join(lines)
            
            return clean_text
            
        except Exception as e:
            print(f"Failed to scrape {url}: {e}")
            return None

    def save_content(self, url, content):
        """Saves scraped content to a file."""
        domain = urlparse(url).netloc.replace('www.', '')
        filename = f"{domain}.txt"
        filepath = os.path.join(self.daily_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"Source: {url}\n")
            f.write(f"Date: {self.today}\n")
            f.write("-" * 50 + "\n\n")
            f.write(content)
            
        return filepath

    def generate_summary_prompt(self):
        """Aggregates all daily files into a single prompt for an LLM."""
        prompt_path = os.path.join(self.daily_dir, 'daily_digest_prompt.txt')
        
        with open(prompt_path, 'w', encoding='utf-8') as outfile:
            outfile.write("I have collected updates from several business websites today. ")
            outfile.write("Please summarize the key updates, news, and changes found in the text below.\n")
            outfile.write("Group the summary by website/source.\n\n")
            outfile.write("=" * 50 + "\n\n")
            
            for filename in os.listdir(self.daily_dir):
                if filename.endswith('.txt') and filename != 'daily_digest_prompt.txt':
                    filepath = os.path.join(self.daily_dir, filename)
                    with open(filepath, 'r', encoding='utf-8') as infile:
                        outfile.write(f"--- START OF CONTENT FROM {filename} ---\n")
                        outfile.write(infile.read())
                        outfile.write(f"\n--- END OF CONTENT FROM {filename} ---\n\n")
        
        print(f"\nSuccess! Daily data and summary prompt saved to:\n{self.daily_dir}")
        print(f"You can now copy the content of '{os.path.basename(prompt_path)}' into ChatGPT/Gemini/etc.")

    def run(self):
        sites = self.load_sites()
        if not sites:
            print("No sites found to monitor. Please add URLs to sites.txt")
            return

        print(f"Starting daily monitor for {len(sites)} sites...")
        
        for url in sites:
            content = self.scrape_site(url)
            if content:
                self.save_content(url, content)
        
        self.generate_summary_prompt()

if __name__ == "__main__":
    monitor = DailyMonitor()
    monitor.run()
