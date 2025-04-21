import sqlite3
import os
import time
import logging
import streamlit as st
import pandas as pd
import requests
import re
from datetime import datetime
import base64
from dotenv import load_dotenv
import folium
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
import phonenumbers  # For phone number validation

# Load environment variables
load_dotenv()

# RapidAPI key for Local Business Data API
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "4028e8ecb3mshc7917ff39380476p12eeefjsn1f86bf9f2996")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Set page config to ensure consistent theme
st.set_page_config(page_title="Business Scraper", page_icon="🗺️", layout="wide")

# Set up logging
logging.basicConfig(
    filename=os.path.join('/tmp' if os.getenv('RENDER') else '.', 'app.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Initialize SQLite database for search history
def init_db():
    db_path = os.path.join('/tmp' if os.getenv('RENDER') else '.', 'search_history.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY AUTOINCREMENT, search_term TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

# Add a search term to the history
def add_search_to_history(search_term):
    db_path = os.path.join('/tmp' if os.getenv('RENDER') else '.', 'search_history.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-d %H:%M:%S")
    c.execute("INSERT INTO searches (search_term, timestamp) VALUES (?, ?)", (search_term, timestamp))
    conn.commit()
    conn.close()

# Get the last 10 search terms
def get_search_history():
    try:
        db_path = os.path.join('/tmp' if os.getenv('RENDER') else '.', 'search_history.db')
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT search_term, timestamp FROM searches ORDER BY timestamp DESC LIMIT 10")
        history = c.fetchall()
        conn.close()
        return history
    except sqlite3.Error:
        return []  # Return empty list if database access fails

# Clear search history
def clear_search_history():
    db_path = os.path.join('/tmp' if os.getenv('RENDER') else '.', 'search_history.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("DELETE FROM searches")
    conn.commit()
    conn.close()

# Export search history as CSV
def export_search_history():
    history = get_search_history()
    if history:
        df = pd.DataFrame(history, columns=["Search Term", "Timestamp"])
        csv = df.to_csv(index=False)
        return csv
    return None

# Clean up old files (older than 1 hour)
def cleanup_old_files():
    current_time = time.time()
    for filename in os.listdir('.'):
        if filename.startswith('business_data_') and (filename.endswith('.csv') or filename.endswith('.xlsx')):
            file_path = os.path.join('.', filename)
            file_age = current_time - os.path.getmtime(file_path)
            if file_age > 3600:  # 1 hour in seconds
                try:
                    os.remove(file_path)
                    logging.info(f"Deleted old file: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting file {file_path}: {e}")

# Rate limiting for Nominatim API (1 request per second)
if "last_nominatim_request" not in st.session_state:
    st.session_state.last_nominatim_request = 0
NOMINATIM_COOLDOWN = 1  # 1 second cooldown

# Rate limiting for website scraping (1 request per second)
if "last_website_request" not in st.session_state:
    st.session_state.last_website_request = 0
WEBSITE_COOLDOWN = 1  # 1 second cooldown

# Rate limiting for Local Business Data API (1 request per second)
if "last_local_business_request" not in st.session_state:
    st.session_state.last_local_business_request = 0
LOCAL_BUSINESS_COOLDOWN = 1  # 1 second cooldown

# Validate phone number using phonenumbers module
def validate_phone_number(phone):
    if phone == "N/A":
        return "N/A"
    try:
        parsed_number = phonenumbers.parse(phone, "PK")
        if phonenumbers.is_valid_number(parsed_number):
            return phonenumbers.format_number(parsed_number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        else:
            return "Invalid"
    except phonenumbers.NumberParseException:
        return "Invalid"

# Hardcoded coordinates for major Pakistani cities as a fallback
CITY_COORDINATES = {
    "karachi": {"center": [24.8607, 67.0011], "bbox": "24.5,66.8,25.2,67.2"},
    "lahore": {"center": [31.5497, 74.3436], "bbox": "31.2,74.1,31.8,74.5"},
    "islamabad": {"center": [33.6844, 73.0479], "bbox": "33.5,72.8,33.8,73.2"},
    "sukkur": {"center": [27.7052, 68.8574], "bbox": "27.5,68.6,27.9,69.0"}
}

# Get bounding box and center for a city using Nominatim API
def get_city_bbox(city_name):
    # Check if city is in hardcoded coordinates
    city_key = city_name.lower()
    if city_key in CITY_COORDINATES:
        logging.info(f"Using hardcoded coordinates for {city_name}")
        return CITY_COORDINATES[city_key]["bbox"], CITY_COORDINATES[city_key]["center"]

    current_time = time.time()
    if current_time - st.session_state.last_nominatim_request < NOMINATIM_COOLDOWN:
        time.sleep(NOMINATIM_COOLDOWN - (current_time - st.session_state.last_nominatim_request))
    
    nominatim_url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "BusinessScraperApp/1.0"}
    
    # List of queries to try
    queries = [
        city_name,                    # e.g., "sukkur"
        f"{city_name}, Pakistan",     # e.g., "sukkur, Pakistan"
        f"{city_name}, Sindh"         # e.g., "sukkur, Sindh"
    ]
    
    for i, query in enumerate(queries):
        params = {
            "q": query,
            "format": "json",
            "bounded": "1",
            "limit": "1",
        }
        
        try:
            response = requests.get(nominatim_url, params=params, headers=headers, timeout=5)
            st.session_state.last_nominatim_request = time.time()
            response.raise_for_status()
            data = response.json()
            
            logging.info(f"Nominatim API response for query '{query}' (attempt {i+1}): {data}")
            
            if not data or not isinstance(data, list):
                logging.warning(f"No valid data returned from Nominatim for query '{query}'")
                continue
            
            # Find a result that matches the city name and is in Pakistan
            for entry in data:
                display_name = entry.get("display_name", "").lower()
                # Check if the city name is in the display name and it's in Pakistan
                if city_name.lower() in display_name and "pakistan" in display_name:
                    bbox = entry.get("boundingbox")
                    if bbox:
                        south, north, west, east = map(float, bbox)
                        bbox_str = f"{south},{west},{north},{east}"
                        center = [float(entry.get("lat")), float(entry.get("lon"))]
                        logging.info(f"Found city {city_name} with display_name: {display_name}")
                        return bbox_str, center
                    else:
                        logging.warning(f"No bounding box found for city {city_name} in entry: {entry}")
            
            logging.info(f"No matching city found for query '{query}'")
        
        except requests.RequestException as e:
            logging.error(f"Error fetching city bbox from Nominatim for query '{query}': {str(e)}")
            st.session_state.last_nominatim_request = time.time()
            continue
    
    # If all Nominatim queries fail, fall back to hardcoded coordinates
    logging.warning(f"All Nominatim queries failed for {city_name}. Falling back to hardcoded coordinates.")
    if city_key in CITY_COORDINATES:
        logging.info(f"Using hardcoded coordinates for {city_name} as final fallback")
        return CITY_COORDINATES[city_key]["bbox"], CITY_COORDINATES[city_key]["center"]
    
    logging.error(f"City {city_name} not found after all attempts and no hardcoded coordinates available.")
    return None, None

# Search for businesses using Local Business Data API
def search_local_business(business_name, business_type, city):
    current_time = time.time()
    if current_time - st.session_state.last_local_business_request < LOCAL_BUSINESS_COOLDOWN:
        time.sleep(LOCAL_BUSINESS_COOLDOWN - (current_time - st.session_state.last_local_business_request))
    
    url = "https://local-business-data.p.rapidapi.com/search"
    # Ensure the query is specific to hospitals in the specified city
    query = f"{business_name} {business_type} {city}" if business_type else f"{business_name} {city}"
    querystring = {
        "query": query,
        "limit": "1",
        "language": "en"
    }
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "local-business-data.p.rapidapi.com"
    }
    
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=5)
        st.session_state.last_local_business_request = time.time()
        if response.status_code == 429:
            logging.error("RapidAPI quota exceeded for Local Business Data API.")
            st.error("API quota exceeded. Please wait for the quota to reset or upgrade to a paid plan on RapidAPI.")
            return None, None, None, None, None
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"Local Business Data API search response for {business_name} in {city}: {data}")
        
        if not data.get("data") or not isinstance(data["data"], list):
            logging.warning(f"No valid data returned from Local Business Data API for {business_name} in {city}")
            return None, "N/A", "N/A", "N/A", "N/A"
        
        if not data["data"]:
            logging.warning(f"Empty data list returned from Local Business Data API for {business_name} in {city}")
            return None, "N/A", "N/A", "N/A", "N/A"
        
        business = data["data"][0]
        if not isinstance(business, dict):
            logging.warning(f"Invalid business entry (not a dictionary) for {business_name} in {city}: {business}")
            return None, "N/A", "N/A", "N/A", "N/A"
        
        # Verify that the result is a hospital in the specified city
        name = business.get("name", "").lower()
        address = business.get("address", "").lower()
        if business_type == "hospitals" and "hospital" not in name and "hospital" not in address:
            logging.warning(f"Business {name} does not appear to be a hospital: {business}")
            return None, "N/A", "N/A", "N/A", "N/A"
        if city.lower() not in address:
            logging.warning(f"Business {name} is not in {city}: {address}")
            return None, "N/A", "N/A", "N/A", "N/A"
        
        business_id = business.get("business_id")
        phone = business.get("phone_number", "N/A")
        email = business.get("email", "N/A")
        opening_hours = business.get("business_hours", "N/A")
        if opening_hours != "N/A" and isinstance(opening_hours, dict):
            hours_str = []
            for day, times in opening_hours.items():
                hours_str.append(f"{day}: {times}")
            opening_hours = "; ".join(hours_str)
        website = business.get("website", "N/A")
        
        phone = validate_phone_number(phone)
        return business_id, phone, email, opening_hours, website
    
    except requests.RequestException as e:
        logging.error(f"Error searching Local Business Data API for {business_name} in {city}: {str(e)}")
        st.session_state.last_local_business_request = time.time()
        return None, "N/A", "N/A", "N/A", "N/A"

# Fetch business details using Local Business Data API
def fetch_local_business_details(business_id):
    if not business_id:
        return "N/A", "N/A", "N/A", "N/A"
    
    current_time = time.time()
    if current_time - st.session_state.last_local_business_request < LOCAL_BUSINESS_COOLDOWN:
        time.sleep(LOCAL_BUSINESS_COOLDOWN - (current_time - st.session_state.last_local_business_request))
    
    url = "https://local-business-data.p.rapidapi.com/business-details"
    querystring = {
        "business_id": business_id,
        "extract_emails_and_contacts": "true",
        "extract_share_link": "false",
        "language": "en"
    }
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "local-business-data.p.rapidapi.com"
    }
    
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=5)
        st.session_state.last_local_business_request = time.time()
        if response.status_code == 429:
            logging.error("RapidAPI quota exceeded for Local Business Data API.")
            st.error("API quota exceeded. Please wait for the quota to reset or upgrade to a paid plan on RapidAPI.")
            return "N/A", "N/A", "N/A", "N/A"
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"Local Business Data API response for business_id {business_id}: {data}")
        
        if not data.get("data"):
            logging.warning(f"No business details found for business_id {business_id}")
            return "N/A", "N/A", "N/A", "N/A"
        
        business = data["data"]
        if not isinstance(business, dict):
            logging.warning(f"Invalid business details (not a dictionary) for business_id {business_id}: {business}")
            return "N/A", "N/A", "N/A", "N/A"
        
        phone = business.get("phone_number", "N/A")
        email = business.get("email", "N/A")
        opening_hours = business.get("business_hours", "N/A")
        if opening_hours != "N/A" and isinstance(opening_hours, dict):
            hours_str = []
            for day, times in opening_hours.items():
                hours_str.append(f"{day}: {times}")
            opening_hours = "; ".join(hours_str)
        website = business.get("website", "N/A")
        
        phone = validate_phone_number(phone)
        return phone, email, opening_hours, website
    
    except requests.RequestException as e:
        logging.error(f"Error fetching Local Business Data API details for business_id {business_id}: {str(e)}")
        st.session_state.last_local_business_request = time.time()
        return "N/A", "N/A", "N/A", "N/A"

# Fetch reviews using Google Places API (optional, requires GOOGLE_API_KEY)
def fetch_google_reviews(business_name, city):
    if not GOOGLE_API_KEY:
        logging.info("Google API key not found. Skipping reviews.")
        return "N/A"

    if "last_google_request" not in st.session_state:
        st.session_state.last_google_request = 0
    GOOGLE_COOLDOWN = 1

    current_time = time.time()
    if current_time - st.session_state.last_google_request < GOOGLE_COOLDOWN:
        time.sleep(GOOGLE_COOLDOWN - (current_time - st.session_state.last_google_request))

    search_url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    search_params = {
        "input": f"{business_name} {city}",
        "inputtype": "textquery",
        "fields": "place_id",
        "key": GOOGLE_API_KEY
    }

    try:
        response = requests.get(search_url, params=search_params, timeout=5)
        st.session_state.last_google_request = time.time()
        response.raise_for_status()
        search_data = response.json()

        logging.info(f"Google Places search response for {business_name} in {city}: {search_data}")

        if search_data.get("status") != "OK" or not search_data.get("candidates"):
            logging.warning(f"No place found for {business_name} in {city} using Google Places API")
            return "N/A"

        place_id = search_data["candidates"][0]["place_id"]

        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        details_params = {
            "place_id": place_id,
            "fields": "reviews",
            "key": GOOGLE_API_KEY
        }

        response = requests.get(details_url, params=details_params, timeout=5)
        st.session_state.last_google_request = time.time()
        response.raise_for_status()
        details_data = response.json()

        logging.info(f"Google Places details response for place_id {place_id}: {details_data}")

        if details_data.get("status") != "OK" or not details_data.get("result"):
            logging.warning(f"No details found for place_id {place_id} using Google Places API")
            return "N/A"

        reviews = details_data["result"].get("reviews", [])
        if not reviews:
            return "No reviews available"

        review_texts = []
        for review in reviews[:3]:
            author = review.get("author_name", "Anonymous")
            rating = review.get("rating", "N/A")
            text = review.get("text", "No comment")
            review_texts.append(f"{author} (Rating: {rating}/5): {text}")
        
        return "; ".join(review_texts)

    except requests.RequestException as e:
        logging.error(f"Error fetching Google Places data for {business_name} in {city}: {str(e)}")
        st.session_state.last_google_request = time.time()
        return "N/A"

# Scrape email, phone, and opening hours from a website
def scrape_website(website_url):
    if not website_url or website_url == "N/A":
        return "N/A", "N/A", "N/A"
    
    current_time = time.time()
    if current_time - st.session_state.last_website_request < WEBSITE_COOLDOWN:
        time.sleep(WEBSITE_COOLDOWN - (current_time - st.session_state.last_website_request))
    
    try:
        headers = {"User-Agent": "BusinessScraperApp/1.0 (Mozilla/5.0; compatible)"}
        response = requests.get(website_url, headers=headers, timeout=5)
        st.session_state.last_website_request = time.time()
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text()
        
        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?"
        emails = re.findall(email_pattern, text)
        email = "N/A"
        if emails:
            for e in emails:
                if "contact" in e.lower() or "info" in e.lower() or "support" in e.lower():
                    email = e
                    break
            if email == "N/A":
                email = emails[0]
        
        phone_pattern = r"(\+\d{1,3}\s?\d{1,4}\s?\d{6,10}|\d{3}-\d{3}-\d{4}|\d{10,12}|0\d{2,3}-\d{7,8})"
        phones = re.findall(phone_pattern, text)
        phone = validate_phone_number(phones[0]) if phones else "N/A"
        
        hours_pattern = r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Mo-Fr|Mo-Su)\s*(?:-)?\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})|(?:Open\s*\d{1,2}\s*(?:AM|PM)\s*to\s*\d{1,2}\s*(?:AM|PM))"
        hours = re.search(hours_pattern, text, re.IGNORECASE)
        opening_hours = hours.group(0) if hours else "N/A"
        
        return email, phone, opening_hours
    
    except requests.RequestException as e:
        logging.error(f"Error scraping website {website_url}: {str(e)}")
        st.session_state.last_website_request = time.time()
        return "N/A", "N/A", "N/A"

# Fetch business data using OpenStreetMap Overpass API for a specific city
def fetch_osm_businesses(search_term, num_to_fetch):
    overpass_url = "http://overpass-api.de/api/interpreter"
    terms = search_term.lower().split()
    if len(terms) < 2:
        return [], None
    city = terms[0]  # e.g., "sukkur"
    business_type = terms[-1]  # e.g., "hospitals"
    
    bbox, center = get_city_bbox(city)
    if not bbox or not center:
        return [], None
    
    osm_tags = {
        "schools": 'node["amenity"="school"]',
        "restaurants": 'node["amenity"="restaurant"]',
        "hospitals": 'node["amenity"="hospital"]'
    }
    tag = osm_tags.get(business_type, 'node["amenity"]')
    query = f"""
    [out:json][timeout:30];
    (
      {tag}({bbox});
    );
    out body;
    """
    try:
        response = requests.get(overpass_url, params={'data': query}, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"Overpass API response: {data}")
        
        if 'elements' not in data or not isinstance(data['elements'], list):
            logging.error("Overpass API response does not contain 'elements' or 'elements' is not a list")
            return [], center
        
        businesses = []
        for element in data['elements'][:num_to_fetch]:
            if not isinstance(element, dict):
                logging.warning(f"Skipping invalid element (not a dictionary): {element}")
                continue
            
            tags = element.get('tags', {})
            logging.info(f"Raw OSM tags for {tags.get('name', 'Unknown')}: {tags}")
            
            name = tags.get('name', 'Unknown').lower()
            lat = element.get('lat', 'N/A')
            lon = element.get('lon', 'N/A')
            phone = tags.get('phone', 'N/A')
            email = tags.get('email', 'N/A')
            opening_hours = tags.get('opening_hours', 'N/A')
            website = tags.get('website', 'N/A')
            
            # Strict filtering to ensure correct business type
            actual_type = tags.get('amenity', '').lower()
            if business_type == "hospitals" and actual_type != "hospital":
                logging.info(f"Skipping {name} - not a hospital (amenity: {actual_type})")
                continue
            if business_type == "restaurants" and actual_type != "restaurant":
                logging.info(f"Skipping {name} - not a restaurant (amenity: {actual_type})")
                continue
            if business_type == "schools" and actual_type != "school":
                logging.info(f"Skipping {name} - not a school (amenity: {actual_type})")
                continue
            
            # Additional check to ensure the business is a hospital based on name
            if business_type == "hospitals" and "hospital" not in name:
                logging.info(f"Skipping {name} - name does not contain 'hospital'")
                continue
            
            # Use Local Business Data API to fetch phone, email, opening hours, and website
            if phone == "N/A" or email == "N/A" or opening_hours == "N/A" or website == "N/A":
                business_id, local_phone, local_email, local_hours, local_website = search_local_business(name, business_type, city)
                if business_id is None:
                    st.warning(f"Could not find {name} in {city} using the Local Business Data API. Trying a simpler query...")
                    business_id, local_phone, local_email, local_hours, local_website = search_local_business(name, "", city)
                
                phone = local_phone if phone == "N/A" and local_phone != "N/A" else phone
                email = local_email if email == "N/A" and local_email != "N/A" else email
                opening_hours = local_hours if opening_hours == "N/A" and local_hours != "N/A" else opening_hours
                website = local_website if website == "N/A" and local_website != "N/A" else website
                
                if (phone == "N/A" or email == "N/A" or opening_hours == "N/A" or website == "N/A") and business_id:
                    local_phone, local_email, local_hours, local_website = fetch_local_business_details(business_id)
                    phone = local_phone if phone == "N/A" and local_phone != "N/A" else phone
                    email = local_email if email == "N/A" and local_email != "N/A" else email
                    opening_hours = local_hours if opening_hours == "N/A" and local_hours != "N/A" else opening_hours
                    website = local_website if website == "N/A" and local_website != "N/A" else website
                else:
                    st.warning(f"No additional data found for {name} in {city} using the Local Business Data API. Falling back to website scraping or assumed hours.")
            
            if (email == "N/A" or phone == "N/A" or opening_hours == "N/A") and website != "N/A":
                scraped_email, scraped_phone, scraped_hours = scrape_website(website)
                email = scraped_email if email == "N/A" and scraped_email != "N/A" else email
                phone = scraped_phone if phone == "N/A" and scraped_phone != "N/A" else phone
                opening_hours = scraped_hours if opening_hours == "N/A" and scraped_hours != "N/A" else opening_hours
            
            if opening_hours == "N/A":
                if business_type == "hospitals":
                    opening_hours = "9:00 AM - 5:00 PM (assumed, please verify)"
                elif business_type == "restaurants":
                    opening_hours = "11:00 AM - 11:00 PM (assumed, please verify)"
            
            reviews_comments = fetch_google_reviews(name, city)
            
            businesses.append({
                'name': name,
                'latitude': lat,
                'longitude': lon,
                'phone': phone,
                'email': email,
                'opening_hours': opening_hours,
                'website': website,
                'reviews_comments': reviews_comments
            })
        return businesses, center
    except requests.RequestException as e:
        logging.error(f"Error fetching OSM data: {str(e)}")
        return [], center

# Get a static map image using OpenStreetMap and folium
def get_static_map(search_term, center):
    try:
        if center is None:
            return None
        m = folium.Map(location=center, zoom_start=12)
        folium.Marker(center, popup=search_term).add_to(m)
        
        img_data = m._to_png(5)
        img = Image.open(BytesIO(img_data))
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        logging.error(f"Error generating map with folium: {str(e)}")
        return None

# Load a local image as a fallback if the URL fails
def load_local_image(image_path):
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode()
    return f"data:image/png;base64,{encoded_string}"

# Rate limiting for Overpass API
if "last_scrape_time" not in st.session_state:
    st.session_state.last_scrape_time = 0
SCRAPE_COOLDOWN = 60  # 60 seconds cooldown

# Theme toggle in session state
if "theme" not in st.session_state:
    st.session_state.theme = "light"

# Initialize the database
init_db()

# --- Sidebar ---
with st.sidebar:
    st.header("Navigation")
    try:
        st.image("https://cdn-icons-png.flaticon.com/512/149/149071.png", width=50)
    except:
        st.image(load_local_image("user_icon.png"), width=50)
    st.write("Business Scraper App")
    
    theme = st.selectbox("Theme", ["Light", "Dark"], index=0 if st.session_state.theme == "light" else 1)
    if theme.lower() != st.session_state.theme:
        st.session_state.theme = theme.lower()
        st.rerun()
    
    search_history = get_search_history()
    if search_history:
        st.subheader("Recent Searches")
        for term, timestamp in search_history:
            st.write(f"📍 {term} ({timestamp})")
        
        if st.button("Clear Search History"):
            clear_search_history()
            st.success("Search history cleared!")
            st.rerun()
        
        csv_data = export_search_history()
        if csv_data:
            st.download_button(
                label="Export Search History as CSV",
                data=csv_data,
                file_name="search_history.csv",
                mime="text/csv"
            )

    with st.expander("How to Use"):
        st.write("""
        1. Enter a search term (e.g., "karachi hospitals").
        2. Specify the number of businesses to fetch.
        3. Click 'Fetch Data' to get results for that city.
        4. View details like phone, email, and opening hours.
        5. Download the results as CSV or Excel.
        6. View a map centered on the city.
        """)

# --- Custom CSS for Theme and Styling ---
theme_styles = {
    "light": """
        <style>
            .stApp {
                background-color: #f0f2f6;
                color: #333333;
            }
            h1, h2, h3, h4, h5, h6 {
                color: #1f77b4;
            }
            .stButton>button {
                background-color: #1f77b4;
                color: white;
                border-radius: "5px"
            }
            .stTextInput>div>input {
                border: 1px solid #1f77b4;
                border-radius: 5px;
            }
        </style>
    """,
    "dark": """
        <style>
            .stApp {
                background-color: #1e1e1e;
                color: #d4d4d4;
            }
            h1, h2, h3, h4, h5, h6 {
                color: #66b3ff;
            }
            .stButton>button {
                background-color: #66b3ff;
                color: #1e1e1e;
                border-radius: 5px;
            }
            .stTextInput>div>input {
                border: 1px solid #66b3ff;
                border-radius: 5px;
                background-color: #333333;
                color: #d4d4d4;
            }
        </style>
    """
}
st.markdown(theme_styles[st.session_state.theme], unsafe_allow_html=True)

# --- Main Page ---
try:
    st.image(
        "https://cdn.pixabay.com/photo/2016/11/29/12/56/map-1868590_1280.jpg",
        caption="Explore Businesses with Business Scraper",
        use_container_width=True
    )
except:
    st.image(
        load_local_image("header_map.jpg"),
        caption="Explore Businesses with Business Scraper",
        use_container_width=True
    )

st.title("Business Scraper")
st.markdown("**Fetch business data and display maps for any city in Pakistan using OpenStreetMap!**")

# Input form in a clean layout
col1, col2 = st.columns([3, 1])
with col1:
    search_term = st.text_input("Search Term", value="karachi hospitals", help="Enter a city and business type (e.g., 'karachi hospitals', 'karachi restaurants')")
with col2:
    num_to_fetch = st.number_input("Number of Businesses", min_value=1, max_value=50, value=5)

# Fetch data button with loading animation
if st.button("Fetch Data"):
    # Input validation
    if not search_term:
        st.error("Search term cannot be empty.")
    elif num_to_fetch < 1 or num_to_fetch > 50:
        st.error("Number of businesses to fetch must be between 1 and 50.")
    else:
        # Rate limiting for Overpass API
        current_time = time.time()
        if current_time - st.session_state.last_scrape_time < SCRAPE_COOLDOWN:
            st.error(f"Please wait {int(SCRAPE_COOLDOWN - (current_time - st.session_state.last_scrape_time))} seconds before fetching again.")
        else:
            with st.spinner("Fetching data... Please wait."):
                loading_placeholder = st.empty()
                for i in range(3):
                    loading_placeholder.markdown("🔄 Fetching" + "." * (i + 1))
                    time.sleep(0.5)
                loading_placeholder.empty()

                try:
                    logging.info(f"User searched for: '{search_term}' with {num_to_fetch} businesses")

                    # Fetch business data using OpenStreetMap Overpass API
                    businesses, center = fetch_osm_businesses(search_term, num_to_fetch)

                    # Update last scrape time
                    st.session_state.last_scrape_time = time.time()

                    # Check if city was found and businesses were fetched
                    if center is None:
                        st.error("City not found in Pakistan. Please try a different city (e.g., 'karachi', 'lahore', 'islamabad').")
                        st.stop()
                    if not businesses:
                        st.error("No businesses were fetched. Try a different business type (e.g., 'hospitals', 'restaurants') or city.")
                        st.stop()

                    # Add to search history
                    add_search_to_history(search_term)

                    # Display summary
                    city = search_term.lower().split()[0]
                    st.success(f"Found {len(businesses)} businesses for '{search_term}' in {city.title()}")

                    # Save to CSV and Excel for download
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    csv_filename = f"business_data_{timestamp}"
                    df = pd.DataFrame(businesses)
                    df.to_csv(f"{csv_filename}.csv", index=False)
                    df.to_excel(f"{csv_filename}.xlsx", index=False)

                    # Display results in an expander
                    with st.expander("Business Results", expanded=True):
                        st.subheader("Fetched Businesses")
                        st.dataframe(df, use_container_width=True)

                        # Updated note about data enrichment and free plan limitations
                        st.info("Note: Phone numbers, emails, and opening hours are fetched using the Local Business Data API (via RapidAPI). The free plan has a limited quota (e.g., 500 requests/month) and may not support all features (e.g., email extraction). If the quota is exceeded, upgrade to a paid plan on RapidAPI. Otherwise, the app falls back to website scraping or assumes default hours (e.g., 9:00 AM - 5:00 PM for hospitals—please verify). Phone numbers are validated using the phonenumbers library. Reviews are fetched using the Google Places API if a GOOGLE_API_KEY is provided; otherwise, 'N/A' is shown.")

                        # Download buttons
                        col_dl1, col_dl2 = st.columns(2)
                        with col_dl1:
                            csv_path = f"{csv_filename}.csv"
                            with open(csv_path, "rb") as f:
                                st.download_button("Download CSV", f, file_name=f"{csv_filename}.csv")
                        with col_dl2:
                            excel_path = f"{csv_filename}.xlsx"
                            with open(excel_path, "rb") as f:
                                st.download_button("Download Excel", f, file_name=f"{csv_filename}.xlsx")

                    # Display a static map
                    with st.expander("View Map", expanded=False):
                        st.subheader(f"Map of {city.title()}")
                        map_url = get_static_map(search_term, center)
                        if map_url:
                            st.image(map_url, caption=f"Location: {city.title()}", use_container_width=True)
                        else:
                            st.warning("Map image couldn't be loaded. Please check the logs for errors.")

                    # Clean up old files
                    cleanup_old_files()

                except Exception as e:
                    st.error(f"An error occurred while fetching: {str(e)}. Try a different city or business type.")
                    logging.error(f"Fetching error: {str(e)}")

# About section with an icon
st.markdown("---")
col_about, col_icon = st.columns([5, 1])
with col_about:
    st.subheader("About")
    st.write("This app fetches business data (phone, email, opening hours) and displays maps for any city in Pakistan using OpenStreetMap. Built by Murk Channa .")
    st.write("[Upwork profile](https://www.upwork.com/freelancers/010b61a989dbeb9136)")
with col_icon:
    try:
        st.image("https://cdn-icons-png.flaticon.com/512/281/281764.png", width=50)
    except:
        st.image(load_local_image("google_maps_icon.png"), width=50)