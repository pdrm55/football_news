import requests
import json
import time
import unicodedata
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

API_KEY = "2fc8d2dbcc2a3b6deef3780fe1ec8c1e"
HEADERS = {
    'x-rapidapi-key': API_KEY,
    'x-rapidapi-host': 'v3.football.api-sports.io'
}
BASE_URL = "https://v3.football.api-sports.io"
KEYWORDS_FILE = "team_keywords.json"

TEAM_IDS = {
    'Arsenal': 42,
    'Liverpool': 40,
    'Inter': 505
}

def remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def clean_name(name):
    """Cleans a name by removing accents and converting to lowercase."""
    if not name:
        return ""
    return remove_accents(name).strip().lower()

def extract_keywords_from_name(full_name):
    """Extracts search keywords (full name, last name, individual parts) from a player/coach name."""
    cleaned = clean_name(full_name)
    parts = cleaned.split()
    
    keywords = {cleaned}  # Add full name
    
    # Add individual name parts (excluding short initials or prefixes)
    for part in parts:
        part = part.replace("'", "").replace("-", "")
        if len(part) >= 3 and part not in ['the', 'der', 'van', 'del', 'dos', 'das', 'dei']:
            keywords.add(part)
            
    return keywords

def fetch_squad_and_coach(team_name, team_id):
    logger.info(f"Fetching squad for {team_name} (ID: {team_id})...")
    keywords = set()
    
    # 1. Fetch Squad
    squad_url = f"{BASE_URL}/players/squads?team={team_id}"
    response = requests.get(squad_url, headers=HEADERS)
    if response.status_code == 200:
        data = response.json()
        for item in data.get('response', []):
            for p in item.get('players', []):
                p_name = p.get('name')
                keywords.update(extract_keywords_from_name(p_name))
    else:
        logger.error(f"Failed to fetch squad for {team_name}: {response.status_code} - {response.text}")
        
    # Rate limit safety: Sleep for 6.5 seconds between requests to avoid API-Football 10 req/min limit
    time.sleep(6.5)
    
    # 2. Fetch Coaches
    logger.info(f"Fetching coaches for {team_name} (ID: {team_id})...")
    coach_url = f"{BASE_URL}/coachs?team={team_id}"
    response = requests.get(coach_url, headers=HEADERS)
    if response.status_code == 200:
        data = response.json()
        for item in data.get('response', []):
            c_name = item.get('name')
            keywords.update(extract_keywords_from_name(c_name))
    else:
        logger.error(f"Failed to fetch coaches for {team_name}: {response.status_code} - {response.text}")
        
    # Rate limit safety sleep
    time.sleep(6.5)
    
    return list(keywords)

def run_update():
    logger.info("Starting team keywords update from API-Football...")
    all_keywords = {}
    
    # Load existing keywords first to preserve team names/stadium names
    if os.path.exists(KEYWORDS_FILE):
        try:
            with open(KEYWORDS_FILE, 'r', encoding='utf-8') as f:
                all_keywords = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load existing keywords file: {e}")
            
    # Base/default keywords that must always exist
    base_keywords = {
        'Arsenal': ['arsenal', 'gunners', 'emirates', 'london colney'],
        'Liverpool': ['liverpool', 'reds', 'anfield', 'melwood', 'axxa'],
        'Inter': ['inter milan', 'nerazzurri', 'san siro', 'appiano gentile']
    }
    
    for team_name, team_id in TEAM_IDS.items():
        try:
            squad_keywords = fetch_squad_and_coach(team_name, team_id)
            
            # Combine base keywords, existing manual keywords, and API fetched keywords
            current_team_keywords = set(base_keywords.get(team_name, []))
            if team_name in all_keywords:
                current_team_keywords.update(all_keywords[team_name])
            current_team_keywords.update(squad_keywords)
            
            all_keywords[team_name] = sorted(list(current_team_keywords))
            logger.info(f"Successfully compiled {len(all_keywords[team_name])} keywords for {team_name}.")
        except Exception as e:
            logger.error(f"Error updating keywords for {team_name}: {e}")
            
    # Save back to JSON file
    try:
        with open(KEYWORDS_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_keywords, f, indent=4, ensure_ascii=False)
        logger.info(f"Keywords successfully saved to {KEYWORDS_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to save keywords to file: {e}")
        return False

if __name__ == "__main__":
    run_update()
