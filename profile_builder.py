import trafilatura
import json
import re
import logging
from collections import Counter
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def extract_hs_codes(text):
    """
    Attempts to find patterns resembling HS Codes (4-8 digits).
    This is a heuristic and might need refinement.
    """
    # Look for 4 to 8 digit numbers that might be HS codes
    # Often formatted as 1234.56.78 or just 123456
    patterns = [
        r'\b\d{4}\.\d{2}\.\d{2}\b', # 1234.56.78
        r'\b\d{4}\s\d{2}\s\d{2}\b', # 1234 56 78
        r'\b\d{6,8}\b'              # 123456 or 12345678
    ]
    
    codes = []
    for p in patterns:
        found = re.findall(p, text)
        codes.extend(found)
    
    return list(set(codes))

def extract_key_terms(text, top_n=20):
    """
    Extracts frequent nouns/phrases to identify products.
    """
    # Simple stop-word filtering (expand as needed)
    stop_words = set(['the', 'and', 'for', 'with', 'from', 'our', 'what', 'where', 'this', 'that', 'products', 'export', 'services', 'contact', 'home', 'about', 'us'])
    
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    filtered_words = [w for w in words if w not in stop_words]
    
    # Get most common words as a proxy for products/business focus
    counter = Counter(filtered_words)
    return [word for word, count in counter.most_common(top_n)]

def build_profile(url):
    logging.info(f"Scanning {url} to build business profile...")
    
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            logging.error("Failed to download website.")
            return None
            
        text = trafilatura.extract(downloaded)
        if not text:
            logging.error("Failed to extract text.")
            return None
            
        logging.info("Website content extracted. Analyzing...")
        
        hs_codes = extract_hs_codes(text)
        key_terms = extract_key_terms(text)
        
        profile = {
            "source_url": url,
            "detected_hs_codes": hs_codes,
            "detected_keywords": key_terms,
            "raw_summary": text[:500] + "..." # Snippet for context
        }
        
        logging.info(f"Profile built! Found {len(hs_codes)} HS codes and {len(key_terms)} keywords.")
        return profile

    except Exception as e:
        logging.error(f"Error building profile: {e}")
        return None

if __name__ == "__main__":
    user_url = "https://www.supabexports.com"
    profile = build_profile(user_url)
    
    if profile:
        with open("user_context.json", "w", encoding='utf-8') as f:
            json.dump(profile, f, indent=4)
        print("Business profile saved to user_context.json")
    else:
        print("Failed to build profile.")
