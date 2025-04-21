"""This script serves as an example on how to use Python
   & Playwright to scrape/extract data from Google Maps"""

from playwright.sync_api import sync_playwright
from dataclasses import dataclass, asdict, field
import pandas as pd
import time
from datetime import datetime


@dataclass
class Business:
    """holds business data"""
    name: str = None
    address: str = None
    website: str = None
    phone_number: str = None
    reviews_count: int = None
    reviews_average: float = None


@dataclass
class BusinessList:
    """holds list of Business objects,
    and save to both excel and csv
    """
    business_list: list[Business] = field(default_factory=list)

    def dataframe(self):
        """transform business_list to pandas dataframe

        Returns: pandas dataframe
        """
        data = list(asdict(business) for business in self.business_list)
        return pd.json_normalize(data, sep="_")

    def save_to_excel(self, filename, append=False):
        """saves pandas dataframe to excel (xlsx) file"""
        try:
            if not self.business_list:
                print("No data to save to Excel.")
                return
            df = self.dataframe()
            if append:
                try:
                    existing_df = pd.read_excel(f"{filename}.xlsx")
                    df = pd.concat([existing_df, df], ignore_index=True)
                except FileNotFoundError:
                    pass
            df.to_excel(f"{filename}.xlsx", index=False)
            print(f"Successfully saved to {filename}.xlsx")
        except Exception as e:
            print(f"Error saving to Excel: {e}")

    def save_to_csv(self, filename, append=False):
        """saves pandas dataframe to csv file"""
        try:
            if not self.business_list:
                print("No data to save to CSV.")
                return
            df = self.dataframe()
            mode = 'a' if append else 'w'
            header = not append
            df.to_csv(f"{filename}.csv", mode=mode, header=header, index=False)
            print(f"Successfully saved to {filename}.csv")
        except Exception as e:
            print(f"Error saving to CSV: {e}")


def load_listings(page, search_for, listing_xpath, max_listings=20):
    """Loads the list of businesses by navigating to the search URL and scrolling"""
    print(f"Loading listings for: {search_for}")
    page.goto(f"https://www.google.com/maps/search/{search_for}", timeout=60000)
    page.wait_for_timeout(5000)

    captcha_selector = 'div[aria-label="CAPTCHA"]'
    if page.query_selector(captcha_selector):
        raise Exception("CAPTCHA detected. Please solve the CAPTCHA manually or consider using the Google Maps API.")

    page.hover(listing_xpath)
    previously_counted = 0
    while True:
        page.mouse.wheel(0, 10000)
        page.wait_for_timeout(3000)

        current_count = page.locator(listing_xpath).count()
        if current_count >= max_listings:
            print(f"Reached maximum listings to load: {max_listings}")
            break
        if current_count == previously_counted:
            print(f"Arrived at all available listings: {current_count}")
            break
        previously_counted = current_count
        print(f"Currently loaded: {previously_counted}")


def scrape_businesses(page, listing_xpath, start_index, num_to_scrape):
    """Scrapes a specified number of businesses starting from start_index"""
    business_list = BusinessList()
    scraped_count = 0
    total_scraped = 0

    listings = page.locator(listing_xpath).all()
    print(f"Available listings: {len(listings)}")

    while total_scraped < num_to_scrape:
        if start_index + scraped_count >= len(listings):
            print(f"No more listings to scrape. Total Scraped in this batch: {total_scraped}")
            break

        listing = listings[start_index + scraped_count]
        business = Business()

        try:
            print(f"Clicking listing {start_index + scraped_count + 1}...")
            for attempt in range(3):
                try:
                    listing.click(timeout=60000)
                    break
                except Exception as e:
                    print(f"Attempt {attempt + 1} failed: {e}")
                    if attempt == 2:
                        raise e
                    time.sleep(5)

            page.wait_for_timeout(5000)
            time.sleep(2)

            name_selector = 'h1.DUwDvf'
            if page.query_selector(name_selector) is None:
                print(f"Could not load detailed page for business {start_index + scraped_count + 1}")
                scraped_count += 1
                page.go_back()
                page.wait_for_timeout(5000)
                continue

            captcha_selector = 'div[aria-label="CAPTCHA"]'
            if page.query_selector(captcha_selector):
                raise Exception("CAPTCHA detected. Please solve the CAPTCHA manually or consider using the Google Maps API.")

            business.name = page.evaluate('() => document.querySelector("h1.DUwDvf").innerText')
            print(f"Extracted name: {business.name}")

            address_selector = 'div.Io6YTe'
            address_elements = page.query_selector_all(address_selector)
            business.address = ""
            for element in address_elements:
                text = element.inner_text()
                if "Sukkur" in text or "Pakistan" in text:
                    business.address = text
                    print(f"Extracted address: {business.address}")
                    break

            website_selector = 'a[href*="http"][class*="CsEnBe"]'
            website_element = page.query_selector(website_selector)
            business.website = website_element.get_attribute("href") if website_element else ""
            if business.website:
                print(f"Extracted website: {business.website}")

            phone_selector = 'div.Io6YTe'
            phone_elements = page.query_selector_all(phone_selector)
            business.phone_number = ""
            for element in phone_elements:
                text = element.inner_text()
                if text.startswith("+92"):
                    business.phone_number = text
                    print(f"Extracted phone: {business.phone_number}")
                    break

            reviews_selector = 'span.F7nice span[aria-label]'
            reviews_element = page.query_selector(reviews_selector)
            if reviews_element:
                aria_label = reviews_element.get_attribute("aria-label")
                if aria_label:
                    parts = aria_label.split()
                    if len(parts) >= 3:
                        business.reviews_average = float(parts[0].replace(",", ".").strip())
                        business.reviews_count = int(parts[2].strip())
                        print(f"Extracted reviews: {business.reviews_count} reviews, {business.reviews_average} average")
                    else:
                        business.reviews_average = ""
                        business.reviews_count = ""
                else:
                    business.reviews_average = ""
                    business.reviews_count = ""
            else:
                business.reviews_average = ""
                business.reviews_count = ""

        except Exception as e:
            print(f"Error scraping business {start_index + scraped_count + 1}: {e}")
            scraped_count += 1
            page.go_back()
            page.wait_for_timeout(5000)
            continue

        if business.name:
            print(f"Scraped business {start_index + scraped_count + 1}: {business.name}")
            business_list.business_list.append(business)
            total_scraped += 1
        else:
            print(f"Skipping business {start_index + scraped_count + 1}: No name found")

        scraped_count += 1
        page.go_back()
        page.wait_for_timeout(10000)

    return business_list, scraped_count


def scrape(search_for, num_to_scrape=5):
    """Main function to scrape businesses, modified for Flask"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"google_maps_data_{timestamp}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # Headless for web app
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://www.google.com/maps", timeout=60000)
        page.wait_for_timeout(5000)

        listing_xpath = '//a[contains(@href, "https://www.google.com/maps/place")]'
        load_listings(page, search_for, listing_xpath, max_listings=20)

        business_list, _ = scrape_businesses(page, listing_xpath, 0, num_to_scrape)

        # Save to both CSV and Excel
        if business_list.business_list:
            business_list.save_to_csv(output_filename)
            business_list.save_to_excel(output_filename)

        context.close()
        browser.close()

    return business_list, output_filename