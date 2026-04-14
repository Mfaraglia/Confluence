import csv
import io
from typing import Dict, List, Optional, Tuple

from flask import Flask, render_template, request

app = Flask(__name__)

# These lists hold common header names we might see in vendor CSV files.
DESCRIPTION_KEYS = [
    "description",
    "product description",
    "item description",
    "product",
    "name",
]
PRICE_KEYS = ["price", "unit price", "cost", "net price"]


# Convert text to lowercase and trim spaces so header matching is easier.
def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


# Pick the first matching column name from a list of possible names.
def pick_column(fieldnames: List[str], possible_names: List[str]) -> Optional[str]:
    normalized_map = {normalize(name): name for name in fieldnames}
    for key in possible_names:
        if key in normalized_map:
            return normalized_map[key]
    return None


# Try to convert text like "$12.50" into a number. Return None if conversion fails.
def parse_price_to_float(price_text: str) -> Optional[float]:
    cleaned = (price_text or "").replace("$", "").replace(",", "").strip()
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# Read one vendor CSV file and return rows (description + price only) plus friendly errors.
def parse_vendor_csv(vendor_name: str, uploaded_file) -> Tuple[List[Dict[str, Optional[float]]], List[str]]:
    if uploaded_file is None or uploaded_file.filename == "":
        return [], []

    errors: List[str] = []
    rows: List[Dict[str, Optional[float]]] = []

    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [f"{vendor_name}: Please upload a UTF-8 CSV file."]

    if not text.strip():
        return [], [f"{vendor_name}: The file is empty. Please upload a CSV with data."]

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return [], [f"{vendor_name}: Could not read column headers. Please check your CSV file."]

    # We only require description + price for this comparison table.
    desc_col = pick_column(reader.fieldnames, DESCRIPTION_KEYS)
    price_col = pick_column(reader.fieldnames, PRICE_KEYS)

    if not desc_col:
        errors.append(
            f"{vendor_name}: Missing a description column. Try 'description' or 'product description'."
        )
    if not price_col:
        errors.append(f"{vendor_name}: Missing a price column. Try 'price' or 'unit price'.")

    if errors:
        return [], errors

    for row in reader:
        description = (row.get(desc_col) or "").strip()
        raw_price = (row.get(price_col) or "").strip()

        # Skip fully empty lines.
        if not description and not raw_price:
            continue

        # Keep rows even if price is blank/unreadable, so user still sees the product.
        rows.append(
            {
                "vendor": vendor_name,
                "description": description,
                "price": parse_price_to_float(raw_price),
            }
        )

    return rows, []


# Combine rows from all vendors by product description only.
def build_comparison_rows(rows: List[Dict[str, Optional[float]]]) -> List[Dict[str, str]]:
    combined: Dict[str, Dict[str, Optional[float]]] = {}

    for row in rows:
        description = (row.get("description") or "").strip()
        if description == "":
            description = "(No description)"

        # Create a new comparison row the first time we see this description.
        if description not in combined:
            combined[description] = {
                "sysco": None,
                "us_foods": None,
                "pfg": None,
            }

        vendor = row.get("vendor")
        price = row.get("price")

        # If duplicate products exist in one vendor file, keep the lowest price for simplicity.
        if vendor == "Sysco":
            current = combined[description]["sysco"]
            combined[description]["sysco"] = price if current is None or (price is not None and price < current) else current
        elif vendor == "US Foods":
            current = combined[description]["us_foods"]
            combined[description]["us_foods"] = price if current is None or (price is not None and price < current) else current
        elif vendor == "PFG":
            current = combined[description]["pfg"]
            combined[description]["pfg"] = price if current is None or (price is not None and price < current) else current

    output: List[Dict[str, str]] = []

    for description, prices in sorted(combined.items(), key=lambda x: x[0].lower()):
        vendor_prices = {
            "Sysco": prices["sysco"],
            "US Foods": prices["us_foods"],
            "PFG": prices["pfg"],
        }

        available = {vendor: value for vendor, value in vendor_prices.items() if value is not None}
        cheapest_vendor = min(available, key=available.get) if available else ""

        output.append(
            {
                "description": description,
                "sysco": f"${prices['sysco']:.2f}" if prices["sysco"] is not None else "",
                "us_foods": f"${prices['us_foods']:.2f}" if prices["us_foods"] is not None else "",
                "pfg": f"${prices['pfg']:.2f}" if prices["pfg"] is not None else "",
                "cheapest_vendor": cheapest_vendor,
            }
        )

    return output


@app.route("/", methods=["GET", "POST"])
def index():
    comparison_rows: List[Dict[str, str]] = []
    errors: List[str] = []

    if request.method == "POST":
        all_vendor_rows: List[Dict[str, Optional[float]]] = []

        vendors = [
            ("Sysco", request.files.get("sysco_file")),
            ("US Foods", request.files.get("usfoods_file")),
            ("PFG", request.files.get("pfg_file")),
        ]

        for vendor_name, file_obj in vendors:
            rows, file_errors = parse_vendor_csv(vendor_name, file_obj)
            all_vendor_rows.extend(rows)
            errors.extend(file_errors)

        if not all_vendor_rows and not errors:
            errors.append("Please upload at least one CSV file.")

        if all_vendor_rows:
            comparison_rows = build_comparison_rows(all_vendor_rows)

    return render_template("index.html", rows=comparison_rows, errors=errors)


if __name__ == "__main__":
    app.run(debug=True)
