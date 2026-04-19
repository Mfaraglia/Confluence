import csv
import io
import re
import uuid
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, render_template, request, session

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"

# Simple in-memory cache for uploaded CSV text between two submits:
# 1) upload file(s)
# 2) apply manual column mapping
# This keeps the app beginner-friendly without adding a database.
UPLOAD_CACHE: Dict[str, Dict[str, str]] = {}

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
PRODUCT_NUMBER_KEYS = ["product number", "item number", "item #", "sku", "product #"]
HEADER_HINT_KEYS = [
    "product description",
    "description",
    "product number",
    "item number",
    "pack size",
    "price",
    "product price",
]

# Simple replacement rules for common foodservice abbreviations/variants.
TERM_REPLACEMENTS = {
    "chk": "chicken",
    "chkn": "chicken",
    "brst": "breast",
    "bnlss": "boneless",
    "grnd": "ground",
    "bf": "beef",
    "mozz": "mozzarella",
    "shrd": "shredded",
    "frz": "frozen",
    "ff": "french fries",
}
WEAK_TOKENS = {"lb", "lbs", "fresh", "frozen", "pack", "size"}


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
    cleaned = (price_text or "").replace("$", "").replace(",", "").replace(" ", "").strip()
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# Build a simple cleaned description for matching similar products across files.
# Rules:
# 1) lowercase + trim
# 2) convert common abbreviations to full words
# 3) remove punctuation + normalize spaces
# 4) apply a few small word-order fixes
def clean_description_for_match(description: str) -> str:
    text = (description or "").lower().strip()

    # Convert pound shorthand (#) into lb before punctuation cleanup.
    text = re.sub(r"#", " lb ", text)

    # Keep current punctuation/space cleanup behavior.
    text = re.sub(r"[,\.\-/()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Expand common abbreviations token-by-token.
    expanded_tokens: List[str] = []
    for token in text.split():
        replacement = TERM_REPLACEMENTS.get(token, token)
        expanded_tokens.extend(replacement.split())
    text = " ".join(expanded_tokens)

    # Small phrase/order fixes for common wording variations.
    text = text.replace("fries french", "french fries")
    text = text.replace("frozen french fries", "french fries")
    text = text.replace("french fries frozen", "french fries")
    text = text.replace("mozzarella shredded cheese", "mozzarella cheese shredded")
    text = re.sub(r"\s+", " ", text).strip()

    return text


# Turn normalized description into meaningful tokens for matching.
# We remove weak words that do not help identify the core product.
def build_meaningful_tokens(normalized_description: str) -> List[str]:
    tokens = [token for token in (normalized_description or "").split() if token not in WEAK_TOKENS]
    # Remove duplicates but keep order for easier debug reading.
    unique_tokens: List[str] = []
    for token in tokens:
        if token not in unique_tokens:
            unique_tokens.append(token)
    return unique_tokens


def token_overlap_score(tokens_a: List[str], tokens_b: List[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    overlap = len(set_a & set_b)
    return overlap / max(len(set_a), len(set_b))


def split_core_and_size_tokens(tokens: List[str]) -> Tuple[List[str], List[str]]:
    core_tokens: List[str] = []
    size_tokens: List[str] = []

    for token in tokens:
        # Treat quantity/size-like values as lower-priority signals.
        if token in WEAK_TOKENS or token.isdigit():
            size_tokens.append(token)
        else:
            core_tokens.append(token)

    return core_tokens, size_tokens


def choose_clearer_description(current: str, candidate: str) -> str:
    # Simple readability rule:
    # keep whichever description is longer (usually less abbreviated).
    return candidate if len((candidate or "").strip()) > len((current or "").strip()) else current


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


def find_header_row(raw_rows: List[List[str]]) -> int:
    for index, row in enumerate(raw_rows):
        normalized_cells = [normalize(cell) for cell in row if normalize(cell)]
        if not normalized_cells:
            continue

        has_description = any(cell in {"description", "product description", "item description"} for cell in normalized_cells)
        has_price = any(cell in {"price", "product price", "unit price", "net price"} for cell in normalized_cells)
        hint_matches = sum(1 for cell in normalized_cells if cell in HEADER_HINT_KEYS)

        # A likely header has at least 2 known header hints and includes description + price style columns.
        if hint_matches >= 2 and has_description and has_price:
            return index

    return 0


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


# Parse one vendor CSV text and return parsed rows + friendly errors + debug details.
# mapping lets the user choose columns manually when headers vary across exports.
def parse_vendor_text(
    vendor_name: str, text: str, mapping: Optional[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Optional[float]]], List[str], Dict[str, Any]]:
    debug_info: Dict[str, Any] = {
        "vendor": vendor_name,
        "uploaded": bool(text.strip()),
        "headers": [],
        "sample_rows": [],
        "parser_path": "not used",
        "delimiter": "",
        "selected_columns": {},
        "mapping_needed": False,
        "header_row_index": 0,
        "skipped_intro_rows": 0,
    }

    errors: List[str] = []
    rows: List[Dict[str, Optional[float]]] = []

    if not text.strip():
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: The file is empty. Please upload a CSV with data."], debug_info

    delimiter = detect_delimiter(text)
    debug_info["delimiter"] = repr(delimiter)

    raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not raw_rows:
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: Could not read column headers. Please check your CSV file."], debug_info

    header_row_index = find_header_row(raw_rows)
    debug_info["header_row_index"] = header_row_index + 1  # 1-based for easier reading
    debug_info["skipped_intro_rows"] = header_row_index

    header_row = raw_rows[header_row_index] if header_row_index < len(raw_rows) else []
    fieldnames = [cell.strip() for cell in header_row]
    if not any(fieldnames):
        debug_info["parser_path"] = "normal"
        return [], [f"{vendor_name}: Could not read column headers. Please check your CSV file."], debug_info

    debug_info["headers"] = fieldnames

    # We only require description + price for this comparison table.
    desc_col = pick_column(fieldnames, DESCRIPTION_KEYS)
    price_col = pick_column(fieldnames, PRICE_KEYS)
    item_col = pick_column(fieldnames, ITEM_NUMBER_KEYS + PRODUCT_NUMBER_KEYS)
    pack_col = pick_column(fieldnames, PACK_SIZE_KEYS)

    # If user provided manual mapping, use those selections.
    # If not, use automatic guesses.
    if mapping:
        desc_col = mapping.get("description") or desc_col
        item_col = mapping.get("item_number") or item_col
        pack_col = mapping.get("pack_size") or pack_col
        price_col = mapping.get("price") or price_col

    debug_info["selected_columns"] = {
        "description": desc_col or "",
        "item_number": item_col or "",
        "pack_size": pack_col or "",
        "price": price_col or "",
    }

    if not desc_col:
        errors.append(f"{vendor_name}: Please choose a Product Description column.")
    if not price_col:
        errors.append(f"{vendor_name}: Please choose a Price column.")

    if errors:
        debug_info["parser_path"] = "normal"
        debug_info["mapping_needed"] = True
        return [], errors, debug_info

    used_fallback = False
    data_rows = raw_rows[header_row_index + 1 :]
    for values in data_rows:
        # Ignore fully blank lines in exports.
        if not any((cell or "").strip() for cell in values):
            continue

        padded_values = values + [""] * max(0, len(fieldnames) - len(values))
        row = {fieldnames[i]: padded_values[i] if i < len(padded_values) else "" for i in range(len(fieldnames))}

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
            "normalized_description": clean_description_for_match(description),
            "final_tokens": build_meaningful_tokens(clean_description_for_match(description)),
            "core_tokens": [],
            "size_tokens": [],
            "final_group_key": "",
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
                    "normalized_description": parsed_row["normalized_description"],
                    "final_tokens": parsed_row["final_tokens"],
                    "core_tokens": parsed_row["core_tokens"],
                    "size_tokens": parsed_row["size_tokens"],
                    "final_group_key": parsed_row["final_group_key"],
                }
            )

        # Keep rows even if price is blank/unreadable, so user still sees the product.
        rows.append(parsed_row)

    debug_info["parser_path"] = "fallback used" if used_fallback else "normal"
    return rows, [], debug_info


# Read one uploaded file and route into parse_vendor_text.
def parse_vendor_csv(
    vendor_name: str, uploaded_file, mapping: Optional[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Optional[float]]], List[str], Dict[str, Any], str]:
    if uploaded_file is None or uploaded_file.filename == "":
        debug_info = {
            "vendor": vendor_name,
            "uploaded": False,
            "headers": [],
            "sample_rows": [],
            "parser_path": "not used",
            "delimiter": "",
            "selected_columns": {},
            "mapping_needed": False,
            "header_row_index": 0,
            "skipped_intro_rows": 0,
        }
        return [], [], debug_info, ""

    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        debug_info = {
            "vendor": vendor_name,
            "uploaded": True,
            "headers": [],
            "sample_rows": [],
            "parser_path": "normal",
            "delimiter": "",
            "selected_columns": {},
            "mapping_needed": False,
            "header_row_index": 0,
            "skipped_intro_rows": 0,
        }
        return [], [f"{vendor_name}: Please upload a UTF-8 CSV file."], debug_info, ""

    rows, errors, debug_info = parse_vendor_text(vendor_name, text, mapping)
    return rows, errors, debug_info, text


# Combine rows from all vendors using token-overlap + similarity matching.
def build_comparison_rows(
    rows: List[Dict[str, Optional[float]]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    combined: Dict[str, Dict[str, Any]] = {}
    match_debug_rows: List[Dict[str, str]] = []

    for row in rows:
        original_description = (row.get("description") or "").strip()
        if original_description == "":
            original_description = "(No description)"

        normalized_description = str(
            row.get("normalized_description") or clean_description_for_match(original_description)
        )
        tokens = build_meaningful_tokens(normalized_description)
        core_tokens, size_tokens = split_core_and_size_tokens(tokens)

        # Find best existing group using:
        # - core token overlap (primary signal)
        # - size token overlap (secondary signal)
        # - text similarity score (backup signal)
        best_key = ""
        best_score = 0.0
        best_core_overlap = 0.0
        best_size_overlap = 0.0
        best_similarity = 0.0

        for group_key, group_data in combined.items():
            group_core_tokens = list(group_data["core_tokens"])
            group_size_tokens = list(group_data["size_tokens"])
            core_overlap = token_overlap_score(core_tokens, group_core_tokens)
            size_overlap = token_overlap_score(size_tokens, group_size_tokens)
            similarity = SequenceMatcher(None, normalized_description, group_data["normalized"]).ratio()
            score = (0.75 * core_overlap) + (0.10 * size_overlap) + (0.15 * similarity)

            if score > best_score:
                best_score = score
                best_key = group_key
                best_core_overlap = core_overlap
                best_size_overlap = size_overlap
                best_similarity = similarity

        # Match when products are "close enough" by simple thresholds.
        # Core words drive grouping; size words are only secondary.
        should_match_existing = (
            best_key != ""
            and (
                best_core_overlap >= 0.60
                or (best_core_overlap >= 0.40 and best_similarity >= 0.72)
                or (best_core_overlap == 0 and best_similarity >= 0.90)
                or (best_core_overlap >= 0.50 and best_size_overlap >= 0.20)
            )
        )

        default_group_key = " ".join(core_tokens) if core_tokens else normalized_description
        final_group_key = best_key if should_match_existing else default_group_key
        if final_group_key == "":
            final_group_key = "(no description)"

        if final_group_key not in combined:
            combined[final_group_key] = {
                "display_description": original_description,
                "normalized": normalized_description,
                "core_tokens": set(core_tokens),
                "size_tokens": set(size_tokens),
                "sysco": None,
                "us_foods": None,
                "pfg": None,
            }
        else:
            # Keep the clearest human-readable label among matched rows.
            combined[final_group_key]["display_description"] = choose_clearer_description(
                str(combined[final_group_key]["display_description"]), original_description
            )
            # Grow token sets with tokens seen in matched descriptions.
            combined[final_group_key]["core_tokens"].update(core_tokens)
            combined[final_group_key]["size_tokens"].update(size_tokens)

        row["core_tokens"] = core_tokens
        row["size_tokens"] = size_tokens
        row["final_group_key"] = final_group_key
        row["final_tokens"] = tokens
        match_debug_rows.append(
            {
                "description": original_description,
                "normalized_description": normalized_description,
                "core_tokens": ", ".join(core_tokens),
                "size_tokens": ", ".join(size_tokens),
                "final_group_key": final_group_key,
            }
        )

        vendor = row.get("vendor")
        price = row.get("price")

        # If duplicate products exist in one vendor file, keep the lowest price for simplicity.
        if vendor == "Sysco":
            current = combined[final_group_key]["sysco"]
            combined[final_group_key]["sysco"] = (
                price if current is None or (price is not None and price < current) else current
            )
        elif vendor == "US Foods":
            current = combined[final_group_key]["us_foods"]
            combined[final_group_key]["us_foods"] = (
                price if current is None or (price is not None and price < current) else current
            )
        elif vendor == "PFG":
            current = combined[final_group_key]["pfg"]
            combined[final_group_key]["pfg"] = (
                price if current is None or (price is not None and price < current) else current
            )

    output: List[Dict[str, str]] = []

    for _, prices in sorted(combined.items(), key=lambda item: item[1]["display_description"].lower()):
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

    return output, match_debug_rows


@app.route("/", methods=["GET", "POST"])
def index():
    comparison_rows: List[Dict[str, str]] = []
    match_debug_rows: List[Dict[str, str]] = []
    errors: List[str] = []
    debug_details: List[Dict[str, Any]] = []
    mapping_options: Dict[str, Any] = {}
    show_mapping_form = False
    upload_id = ""
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
        action = request.form.get("action", "upload")
        upload_id = request.form.get("upload_id", "")

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

        vendor_keys = [
            ("Sysco", "sysco_file", "sysco"),
            ("US Foods", "usfoods_file", "usfoods"),
            ("PFG", "pfg_file", "pfg"),
        ]

        # Step 1: user uploads files. We parse headers and store raw text for optional mapping step.
        if action == "upload":
            upload_id = str(uuid.uuid4())
            UPLOAD_CACHE[upload_id] = {}
            session["last_upload_id"] = upload_id

            vendors = [
                ("Sysco", sysco_file),
                ("US Foods", usfoods_file),
                ("PFG", pfg_file),
            ]

            for vendor_name, file_obj in vendors:
                rows, file_errors, debug_info, file_text = parse_vendor_csv(vendor_name, file_obj)
                all_vendor_rows.extend(rows)
                errors.extend(file_errors)
                debug_details.append(debug_info)

                if file_text.strip():
                    UPLOAD_CACHE[upload_id][vendor_name] = file_text

                if debug_info.get("uploaded"):
                    missing_required = not debug_info["selected_columns"].get("description") or not debug_info[
                        "selected_columns"
                    ].get("price")
                    if missing_required:
                        show_mapping_form = True
                        vendor_key = next((key for name, _, key in vendor_keys if name == vendor_name), "")
                        mapping_options[vendor_key] = {
                            "vendor_name": vendor_name,
                            "headers": debug_info.get("headers", []),
                            "selected": debug_info.get("selected_columns", {}),
                        }

        # Step 2: user applies manual mapping. We parse from cached CSV text.
        elif action == "apply_mapping":
            if not upload_id:
                upload_id = session.get("last_upload_id", "")
            cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})

            for vendor_name, _, vendor_key in vendor_keys:
                file_text = cached_vendor_files.get(vendor_name, "")
                if not file_text:
                    debug_details.append(
                        {
                            "vendor": vendor_name,
                            "uploaded": False,
                            "headers": [],
                            "sample_rows": [],
                            "parser_path": "not used",
                            "delimiter": "",
                            "selected_columns": {},
                            "mapping_needed": False,
                            "header_row_index": 0,
                            "skipped_intro_rows": 0,
                        }
                    )
                    continue

                mapping = {
                    "description": request.form.get(f"{vendor_key}_description", ""),
                    "item_number": request.form.get(f"{vendor_key}_item_number", ""),
                    "pack_size": request.form.get(f"{vendor_key}_pack_size", ""),
                    "price": request.form.get(f"{vendor_key}_price", ""),
                }
                rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, mapping)
                all_vendor_rows.extend(rows)
                errors.extend(file_errors)
                debug_details.append(debug_info)

                if debug_info.get("mapping_needed"):
                    show_mapping_form = True
                    mapping_options[vendor_key] = {
                        "vendor_name": vendor_name,
                        "headers": debug_info.get("headers", []),
                        "selected": debug_info.get("selected_columns", {}),
                    }

        if not all_vendor_rows and not errors and action == "upload":
            errors.append("Please upload at least one CSV file.")

        if all_vendor_rows and not show_mapping_form:
            comparison_rows, match_debug_rows = build_comparison_rows(all_vendor_rows)
        elif show_mapping_form and not errors:
            errors.append("Please choose manual column mappings, then click Apply Column Mapping.")

    return render_template(
        "index.html",
        rows=comparison_rows,
        errors=errors,
        debug_details=debug_details,
        match_debug_rows=match_debug_rows,
        upload_debug=upload_debug,
        show_mapping_form=show_mapping_form,
        mapping_options=mapping_options,
        upload_id=upload_id,
    )


if __name__ == "__main__":
    app.run(debug=True)
