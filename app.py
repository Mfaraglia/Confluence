import csv
import io
from typing import Dict, List, Optional, Tuple

from flask import Flask, render_template, request

app = Flask(__name__)


DESCRIPTION_KEYS = [
    "description",
    "product description",
    "item description",
    "product",
    "name",
]
PRICE_KEYS = ["price", "unit price", "cost", "net price"]
ITEM_KEYS = ["item number", "item #", "item", "sku", "item_no"]
PACK_KEYS = ["pack size", "pack", "size", "uom"]


def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def pick_column(fieldnames: List[str], possible_names: List[str]) -> Optional[str]:
    normalized_map = {normalize(name): name for name in fieldnames}
    for key in possible_names:
        if key in normalized_map:
            return normalized_map[key]
    return None


def parse_vendor_csv(vendor_name: str, uploaded_file) -> Tuple[List[Dict[str, str]], List[str]]:
    """Read one vendor CSV file and return cleaned rows and friendly error messages."""
    if uploaded_file is None or uploaded_file.filename == "":
        return [], []

    errors = []
    rows = []

    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [f"{vendor_name}: Please upload a UTF-8 CSV file."]

    if not text.strip():
        return [], [f"{vendor_name}: The file is empty. Please upload a CSV with data."]

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return [], [f"{vendor_name}: Could not read column headers. Please check your CSV file."]

    desc_col = pick_column(reader.fieldnames, DESCRIPTION_KEYS)
    price_col = pick_column(reader.fieldnames, PRICE_KEYS)
    item_col = pick_column(reader.fieldnames, ITEM_KEYS)
    pack_col = pick_column(reader.fieldnames, PACK_KEYS)

    if not desc_col:
        errors.append(
            f"{vendor_name}: Missing a description column. Try a column named 'description' or 'product description'."
        )
    if not price_col:
        errors.append(
            f"{vendor_name}: Missing a price column. Try a column named 'price' or 'unit price'."
        )

    if errors:
        return [], errors

    for row in reader:
        description = (row.get(desc_col) or "").strip()
        price = (row.get(price_col) or "").strip()

        if not description and not price:
            continue

        rows.append(
            {
                "vendor": vendor_name,
                "description": description or "(No description)",
                "item_number": ((row.get(item_col) or "").strip() if item_col else ""),
                "pack_size": ((row.get(pack_col) or "").strip() if pack_col else ""),
                "price": price or "(No price)",
            }
        )

    return rows, []


@app.route("/", methods=["GET", "POST"])
def index():
    uploaded_rows: List[Dict[str, str]] = []
    errors: List[str] = []

    if request.method == "POST":
        vendors = [
            ("Sysco", request.files.get("sysco_file")),
            ("US Foods", request.files.get("usfoods_file")),
            ("PFG", request.files.get("pfg_file")),
        ]

        for vendor_name, file_obj in vendors:
            rows, file_errors = parse_vendor_csv(vendor_name, file_obj)
            uploaded_rows.extend(rows)
            errors.extend(file_errors)

        if not uploaded_rows and not errors:
            errors.append("Please upload at least one CSV file.")

    return render_template("index.html", rows=uploaded_rows, errors=errors)


if __name__ == "__main__":
    app.run(debug=True)
