import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import urllib3
from dotenv import load_dotenv
from pathlib import Path

# Load .env from the same folder as this script, regardless of where the
# terminal's current working directory happens to be.
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# If your network does SSL inspection (common on corporate laptops) and
# pip-system-certs isn't an option, set this to False as a fallback.
# This disables certificate verification for requests to MyFCD only.
VERIFY_SSL = True
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://myfcd.moh.gov.my/myfcdcurrent/index.php/site/detail_product/{code}/0/10/-1/0/0/"

# ---- Apps Script Web App config ----
# Loaded from .env (APPS_SCRIPT_URL=...) instead of hardcoded here.
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# Units that mark a row as an actual nutrient row (vs a section header like "Minerals")
KNOWN_UNITS = {"g", "mg", "µg", "μg", "kcal"}


def load_codes_from_listing_json(path: str) -> list[dict]:
    """
    Load codes from the listing JSON you captured via DevTools, e.g.:
    {"data": [["R101061", "BISCUIT, COCONUT", "1.01", "myfcdcurrent"], ...]}
    Returns [{"code": "R101061", "name": "BISCUIT, COCONUT"}, ...]
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rows = raw["data"] if isinstance(raw, dict) and "data" in raw else raw
    return [{"code": row[0], "name": row[1]} for row in rows]


def scrape_food(code: str) -> dict:
    """Scrape nutrition data for a single food code, e.g. 'R101070'."""
    url = BASE_URL.format(code=code)
    resp = requests.get(url, headers=HEADERS, timeout=15, verify=VERIFY_SSL)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title looks like "BISCUIT, MARIE  R101070" inside an <h3>
    name = None
    title_tag = soup.find("h3")
    if title_tag:
        title_text = title_tag.get_text(" ", strip=True)
        # Strip the trailing code (matches the code we requested) off the name
        name = re.sub(rf"\s*{re.escape(code)}\s*$", "", title_text).strip()

    # Serving size, e.g. "1 piece [ 13.02 g ]" — pulled from the table header
    serving_label = None
    serving_grams = None

    nutrients = {}
    # The page has multiple <table> elements (e.g. a small Source/Published
    # Date info table appears before the real nutrient table), so pick the
    # one whose header row actually contains "Nutrient".
    table = None
    for candidate in soup.find_all("table"):
        header_cells = [c.get_text(strip=True) for c in candidate.find_all(["td", "th"], limit=5)]
        if any(cell == "Nutrient" for cell in header_cells):
            table = candidate
            break
    if table:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if not cells:
                continue

            # Header row containing the serving size column, e.g.:
            # ["Nutrient", "Unit", "Value per 100g", "1 piece [ 13.02 g ]"]
            if cells[0] == "Nutrient" and len(cells) >= 4:
                m = re.search(r"\[\s*([\d.]+)\s*g\s*\]", cells[3])
                serving_label = cells[3]
                serving_grams = float(m.group(1)) if m else None
                continue

            # Nutrient data row: needs a recognizable unit in cells[1]
            if len(cells) >= 3 and cells[1].lower() in KNOWN_UNITS:
                nutrient_name, unit, value_100g = cells[0], cells[1], cells[2]
                per_serving = cells[3] if len(cells) >= 4 and cells[3] else None
                nutrients[nutrient_name] = {
                    "unit": unit,
                    "per_100g": value_100g,
                    "per_serving": per_serving,
                }
            # else: section header row (e.g. "Proximates") or blank — skip

    return {
        "code": code,
        "name": name,
        "serving_label": serving_label,
        "serving_grams": serving_grams,
        "nutrients": nutrients,
        "source_url": url,
    }


def _get_nutrient_value(nutrients: dict, target_name: str):
    """Case-insensitive lookup for a nutrient's per_100g value, e.g. 'Energy', 'Protein'."""
    for nutrient_name, vals in nutrients.items():
        if nutrient_name.strip().lower() == target_name.strip().lower():
            return vals.get("per_100g")
    return None


def write_to_google_sheet(data: list[dict]) -> None:
    """
    Send each scraped food record to the Apps Script Web App (doPost endpoint),
    which writes/updates the row in Google Sheets on its side.

    Requires APPS_SCRIPT_URL to be set to your deployed Web App URL (ends in /exec).
    The Apps Script side only records: id, name, energy, protein.
    """
    if not APPS_SCRIPT_URL:
        print("APPS_SCRIPT_URL not set — skipping Google Sheets write.")
        return

    success_count = 0
    for food in data:
        energy = _get_nutrient_value(food["nutrients"], "Energy")
        protein = _get_nutrient_value(food["nutrients"], "Protein")

        payload = {
            "id": food["code"],
            "name": food["name"],
            "energy": energy or "",
            "protein": protein or "",
        }

        try:
            # Mirrors the JS fetch pattern: body is a JSON string, sent as
            # text/plain (Apps Script Web Apps commonly expect this to avoid
            # a CORS preflight on the POST).
            resp = requests.post(
                APPS_SCRIPT_URL,
                data=json.dumps(payload),
                headers={"Content-Type": "text/plain;charset=utf-8"},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            print(f"  -> {food['code']}: {result.get('status')}")
            if result.get("status") in ("inserted", "updated"):
                success_count += 1
        except Exception as e:
            print(f"  -> {food['code']}: FAILED ({e})")

        time.sleep(0.5)  # be polite to the Apps Script endpoint too

    print(f"Wrote {success_count}/{len(data)} records to Google Sheet via Apps Script.")


def scrape_many(codes: list[str], delay_seconds: float = 1.0) -> list[dict]:
    """Scrape multiple codes with a polite delay between requests."""
    results = []
    for i, code in enumerate(codes):
        try:
            data = scrape_food(code)
            results.append(data)
            print(f"[{i+1}/{len(codes)}] OK: {code} -> {data['name']}")
        except Exception as e:
            print(f"[{i+1}/{len(codes)}] FAILED: {code} -> {e}")
        time.sleep(delay_seconds)  # be polite to a government server
    return results


if __name__ == "__main__":
    # Option A: hardcode codes for a quick test
    test_codes = ["R106113", "R106073","R106051","R106036","R106035"]  # BISCUIT, MARIE / BISCUIT, COCONUT

    # Option B: once you've saved your DevTools listing JSON as codes.json,
    # swap in this instead:
    # entries = load_codes_from_listing_json("codes.json")
    # test_codes = [e["code"] for e in entries]

    data = scrape_many(test_codes)

    with open("myfcd_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(data)} records to myfcd_data.json")

    write_to_google_sheet(data)
