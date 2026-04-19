import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple

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
ITEM_NUMBER_KEYS = ["item number", "item #", "item_no", "item no", "sku"]
PACK_SIZE_KEYS = ["pack size", "pack", "size", "uom"]


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


# Build a simple cleaned description for matching similar products across files.
# Rules: lowercase, trim spaces, collapse multiple spaces, remove common punctuation.
def clean_description_for_match(description: str) -> str:
    text = (description or "").lower().strip()
    text = re.sub(r"[,\.\-/()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Some uploads are messy and may parse poorly with the first attempt.
# We try to detect delimiter first so csv.DictReader can use the right separator.
def detect_delimiter(csv_text: str) -> str:
    sample = (csv_text or "")[:2048]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
        return sniffed.delimiter
    except csv.Error:
        # Fallback to comma (most common case).
        return ","


# Check if a parsed row likely contains an entire CSV line in one field.
# We only trigger fallback when there is one non-empty value with 3+ commas,
# which helps avoid breaking correctly formatted rows.
def row_looks_like_single_column_csv(row: Dict[str, Optional[str]]) -> bool:
    non_empty_values = [((value or "").strip()) for value in row.values() if (value or "").strip()]
    return len(non_empty_values) == 1 and non_empty_values[0].count(",") >= 3


# Manual recovery path for rows that landed in one field.
# We map the split values to description, item_number, pack_size, price.
def split_single_column_row(single_value: str) -> Dict[str, str]:
    parts = [part.strip() for part in (single_value or "").split(",", 3)]
    while len(parts) < 4:
        parts.append("")
    return {
        "description": parts[0],
        "item_number": parts[1],
        "pack_size": parts[2],
        "price": parts[3],
    }


# Read one vendor CSV file and return parsed rows + friendly errors + debug details.
def parse_vendor_csv(
    vendor_name: str, uploaded_file
) -> Tuple[List[Dict[str, Optional[float]]], List[str], Dict[str, Any]]:
    debug_info: Dict[str, Any] = {
        "vendor": vendor_name,
        "uploaded": uploaded_file is not None and uploaded_file.filename != "",
        "headers": [],
        "sample_rows": [],
        "parser_path": "not used",
        "delimiter": "",
    }

    if uploaded_file is None or uploaded_file.filename == "":
        return [], [], debug_info

    errors: List[str] = []
    rows: List[Dict[str, Optional[float]]] = []

    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: Please upload a UTF-8 CSV file."], debug_info

    if not text.strip():
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: The file is empty. Please upload a CSV with data."], debug_info

    delimiter = detect_delimiter(text)
    debug_info["delimiter"] = repr(delimiter)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    if not reader.fieldnames:
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: Could not read column headers. Please check your CSV file."], debug_info

    debug_info["headers"] = list(reader.fieldnames)

    # We only require description + price for this comparison table.
    desc_col = pick_column(reader.fieldnames, DESCRIPTION_KEYS)
    price_col = pick_column(reader.fieldnames, PRICE_KEYS)
    item_col = pick_column(reader.fieldnames, ITEM_NUMBER_KEYS)
    pack_col = pick_column(reader.fieldnames, PACK_SIZE_KEYS)

    if not desc_col:
        errors.append(
            f"{vendor_name}: Missing a description column. Try 'description' or 'product description'."
        )
    if not price_col:
        errors.append(f"{vendor_name}: Missing a price column. Try 'price' or 'unit price'.")

    if errors:
        debug_info["parser_path"] = "normal"
        return [], errors, debug_info

    used_fallback = False
    for row in reader:
        description = (row.get(desc_col) or "").strip()
        raw_price = (row.get(price_col) or "").strip()
        item_number = (row.get(item_col) or "").strip() if item_col else ""
        pack_size = (row.get(pack_col) or "").strip() if pack_col else ""

        # Safe fallback for malformed rows that were parsed into one field.
        if row_looks_like_single_column_csv(row):
            used_fallback = True
            single_value = next(
                ((value or "").strip() for value in row.values() if (value or "").strip()),
                "",
            )
            recovered = split_single_column_row(single_value)
            description = recovered["description"]
            item_number = recovered["item_number"]
            pack_size = recovered["pack_size"]
            raw_price = recovered["price"]

        # Skip fully empty lines.
        if not description and not raw_price and not item_number and not pack_size:
            continue

        parsed_row = {
            "vendor": vendor_name,
            "description": description,
            "item_number": item_number,
            "pack_size": pack_size,
            "price": parse_price_to_float(raw_price),
        }

        # Save first 3 parsed rows so users can debug parsing behavior easily.
        if len(debug_info["sample_rows"]) < 3:
            debug_info["sample_rows"].append(
                {
                    "description": description,
                    "item_number": item_number,
                    "pack_size": pack_size,
                    "raw_price": raw_price,
                    "parsed_price": parsed_row["price"],
                }
            )

        # Keep rows even if price is blank/unreadable, so user still sees the product.
        rows.append(parsed_row)

    debug_info["parser_path"] = "fallback used" if used_fallback else "normal"
    return rows, [], debug_info


# Combine rows from all vendors using a cleaned description key.
def build_comparison_rows(rows: List[Dict[str, Optional[float]]]) -> List[Dict[str, str]]:
    combined: Dict[str, Dict[str, Optional[float]]] = {}

    for row in rows:
        original_description = (row.get("description") or "").strip()
        if original_description == "":
            original_description = "(No description)"

        # This cleaned key lets "Chicken-Breast", "chicken breast", and
        # "Chicken Breast" match into one row.
        match_key = clean_description_for_match(original_description)
        if match_key == "":
            match_key = "(no description)"

        # Create one combined row the first time we see this match key.
        # We keep the first readable/original description for the table display.
        if match_key not in combined:
            combined[match_key] = {
                "display_description": original_description,
                "sysco": None,
                "us_foods": None,
                "pfg": None,
            }

        vendor = row.get("vendor")
        price = row.get("price")

        # If duplicate products exist in one vendor file, keep the lowest price for simplicity.
        if vendor == "Sysco":
            current = combined[match_key]["sysco"]
            combined[match_key]["sysco"] = (
                price if current is None or (price is not None and price < current) else current
            )
        elif vendor == "US Foods":
            current = combined[match_key]["us_foods"]
            combined[match_key]["us_foods"] = (
                price if current is None or (price is not None and price < current) else current
            )
        elif vendor == "PFG":
            current = combined[match_key]["pfg"]
            combined[match_key]["pfg"] = (
                price if current is None or (price is not None and price < current) else current
            )

    output: List[Dict[str, str]] = []

    for _, prices in sorted(
        combined.items(), key=lambda item: item[1]["display_description"].lower()
    ):
        vendor_prices = {
            "Sysco": prices["sysco"],
            "US Foods": prices["us_foods"],
            "PFG": prices["pfg"],
        }

        available = {vendor: value for vendor, value in vendor_prices.items() if value is not None}
        cheapest_vendor = min(available, key=available.get) if available else ""

        output.append(
            {
                "description": str(prices["display_description"]),
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
    debug_details: List[Dict[str, Any]] = []
    upload_debug: Dict[str, Any] = {
        "request_method": request.method,
        "files_keys": [],
        "received": {
            "Sysco": False,
            "US Foods": False,
            "PFG": False,
        },
    }

    if request.method == "POST":
        all_vendor_rows: List[Dict[str, Optional[float]]] = []

        # File uploads require multipart/form-data in the HTML form.
        # If the form is not multipart, request.files will be empty.
        upload_debug["files_keys"] = list(request.files.keys())

        # IMPORTANT: request.files names must exactly match the HTML input name attributes.
        # Example: <input name="sysco_file"> must be read with request.files.get("sysco_file").
        sysco_file = request.files.get("sysco_file")
        usfoods_file = request.files.get("usfoods_file")
        pfg_file = request.files.get("pfg_file")

        upload_debug["received"]["Sysco"] = bool(sysco_file and sysco_file.filename)
        upload_debug["received"]["US Foods"] = bool(usfoods_file and usfoods_file.filename)
        upload_debug["received"]["PFG"] = bool(pfg_file and pfg_file.filename)

        vendors = [
            ("Sysco", sysco_file),
            ("US Foods", usfoods_file),
            ("PFG", pfg_file),
        ]

        for vendor_name, file_obj in vendors:
            rows, file_errors, debug_info = parse_vendor_csv(vendor_name, file_obj)
            all_vendor_rows.extend(rows)
            errors.extend(file_errors)
            debug_details.append(debug_info)

        if not all_vendor_rows and not errors:
            errors.append("Please upload at least one CSV file.")

        if all_vendor_rows:
            comparison_rows = build_comparison_rows(all_vendor_rows)

    return render_template(
        "index.html",
        rows=comparison_rows,
        errors=errors,
        debug_details=debug_details,
        upload_debug=upload_debug,
    )


if __name__ == "__main__":
    app.run(debug=True)
