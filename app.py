import csv
from difflib import SequenceMatcher
import io
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, render_template, request, session
from manual_overrides import MANUAL_MATCH_OVERRIDES

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"
MATCH_MEMORY_FILE = "match_memory.json"
# Preview-safe mode: avoid depending on local file writes (can fail on Vercel).
IS_VERCEL = os.getenv("VERCEL", "").lower() in {"1", "true", "yes"}
ENABLE_FILE_PERSISTENCE = (os.getenv("ENABLE_FILE_PERSISTENCE", "false").lower() == "true") and (not IS_VERCEL)
STARTUP_MATCH_MEMORY_STATUS: Dict[str, Any] = {"loaded": False, "confirmed": 0, "rejected": 0}

# Simple in-memory cache for uploaded CSV text between two submits:
# 1) upload file(s)
# 2) apply manual column mapping
# This keeps the app beginner-friendly without adding a database.
UPLOAD_CACHE: Dict[str, Dict[str, str]] = {}
SESSION_REVIEW_MEMORY: Dict[str, Dict[str, Any]] = {}


def load_match_memory() -> Dict[str, Any]:
    try:
        with open(MATCH_MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            memory = {
                "confirmed": data.get("confirmed", {}),
                "rejected": set(data.get("rejected", [])),
                "unit_corrections": data.get("unit_corrections", {}),
            }
            STARTUP_MATCH_MEMORY_STATUS["loaded"] = True
            STARTUP_MATCH_MEMORY_STATUS["confirmed"] = len(memory["confirmed"])
            STARTUP_MATCH_MEMORY_STATUS["rejected"] = len(memory["rejected"])
            return memory
    except Exception:
        STARTUP_MATCH_MEMORY_STATUS["loaded"] = False
        STARTUP_MATCH_MEMORY_STATUS["confirmed"] = 0
        STARTUP_MATCH_MEMORY_STATUS["rejected"] = 0
        return {"confirmed": {}, "rejected": set(), "unit_corrections": {}}


def save_match_memory(memory: Dict[str, Any]) -> None:
    # Vercel deployments use a read-only project filesystem, so writing local files is disabled there.
    if not ENABLE_FILE_PERSISTENCE:
        return
    with open(MATCH_MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "confirmed": memory.get("confirmed", {}),
                "rejected": sorted(list(memory.get("rejected", set()))),
                "unit_corrections": memory.get("unit_corrections", {}),
            },
            f,
            indent=2,
        )


# Load match memory once on startup so the app has durable state immediately.
_ = load_match_memory()


def get_effective_match_memory() -> Dict[str, Any]:
    memory = load_match_memory()
    fallback_memory = session.get("match_memory_fallback", {})
    if isinstance(fallback_memory, dict):
        memory["confirmed"].update(fallback_memory.get("confirmed", {}))
        memory["rejected"].update(set(fallback_memory.get("rejected", [])))
        memory["unit_corrections"].update(fallback_memory.get("unit_corrections", {}))
    session_id = get_session_id()
    session_memory = SESSION_REVIEW_MEMORY.get(
        session_id, {"confirmed": {}, "rejected": set(), "unit_corrections": {}}
    )
    memory["confirmed"].update(session_memory.get("confirmed", {}))
    memory["rejected"].update(set(session_memory.get("rejected", set())))
    memory["unit_corrections"].update(session_memory.get("unit_corrections", {}))
    return memory


def build_basic_comparison_rows(rows: List[Dict[str, Optional[float]]]) -> List[Dict[str, str]]:
    # Emergency fallback if advanced grouping fails.
    combined: Dict[str, Dict[str, Optional[float]]] = {}
    for row in rows:
        description = str((row.get("description") or "").strip() or "(No description)")
        if description not in combined:
            combined[description] = {"sysco": None, "us_foods": None, "pfg": None}

        vendor = row.get("vendor")
        price = row.get("price")
        if vendor == "Sysco":
            combined[description]["sysco"] = price
        elif vendor == "US Foods":
            combined[description]["us_foods"] = price
        elif vendor == "PFG":
            combined[description]["pfg"] = price

    output: List[Dict[str, str]] = []
    for description, prices in combined.items():
        vendor_prices = {"Sysco": prices["sysco"], "US Foods": prices["us_foods"], "PFG": prices["pfg"]}
        available = {k: v for k, v in vendor_prices.items() if v is not None}
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


def build_pair_key(left: str, right: str) -> str:
    a = clean_description_for_match(left)
    b = clean_description_for_match(right)
    return "||".join(sorted([a, b]))


def build_forced_group_assignments(match_memory: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    # Confirmed review pairs are treated as the highest-priority grouping rule.
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a: str, b: str) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    confirmed_pairs = match_memory.get("confirmed", {})
    proposed_by_pair: Dict[str, str] = {}
    for pair_key, proposed in confirmed_pairs.items():
        parts = pair_key.split("||")
        if len(parts) != 2:
            continue
        left = parts[0].strip()
        right = parts[1].strip()
        if not left or not right:
            continue
        union(left, right)
        proposed_by_pair[pair_key] = str(proposed or "").strip()

    components: Dict[str, List[str]] = {}
    for node in list(parent.keys()):
        root = find(node)
        components.setdefault(root, []).append(node)

    description_to_group: Dict[str, str] = {}
    group_to_descriptions: Dict[str, List[str]] = {}
    for root, descriptions in components.items():
        normalized_descriptions = sorted(set(descriptions))
        suggested_group_key = ""
        for pair_key, proposed in proposed_by_pair.items():
            if proposed and any(desc in pair_key for desc in normalized_descriptions):
                suggested_group_key = proposed
                break
        forced_group_key = suggested_group_key or f"forced::{root}"
        group_to_descriptions[forced_group_key] = normalized_descriptions
        for description in normalized_descriptions:
            description_to_group[description] = forced_group_key

    return description_to_group, group_to_descriptions


def get_session_id() -> str:
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return str(session["session_id"])

# These lists hold common header names we might see in vendor CSV files.
DESCRIPTION_KEYS = [
    "description",
    "product description",
    "item description",
    "product",
    "name",
]
PRICE_KEYS = ["price", "product price", "unit price", "net price", "sell price", "cost"]
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
PRICE_HEADER_CANDIDATES = ["price", "product price", "unit price", "net price", "sell price"]

# Simple replacement rules for common foodservice abbreviations/variants.
TERM_REPLACEMENTS = {
    "chk": "chicken",
    "chkn": "chicken",
    "brst": "breast",
    "dbl": "double",
    "bnlss": "boneless",
    "bnls": "boneless",
    "sknls": "skinless",
    "b/s": "boneless skinless",
    "grnd": "ground",
    "bf": "beef",
    "bb": "battered",
    "mozz": "mozzarella",
    "shrd": "shredded",
    "frz": "frozen",
    "fz": "frozen",
    "ff": "french fries",
    "hvy": "heavy",
    "tff": "trans fat free",
    "ntrsbst": "nonthermostabilized",
    "cont": "container",
    "cmpt": "compartment",
    "whi": "white",
    "hngd": "hinged",
    "lg": "large",
    "slvr": "silver source",
    "src": "source",
    "applwd": "applewood",
    "ref": "refrigerated",
    "fc": "fully cooked",
    "slcd": "sliced",
    "shrd": "shredded",
    "lqd": "liquid",
    "blnd": "blend",
    "alt": "alternative",
    "breader": "batter mix tempura",
    "tempura": "tempura batter mix",
    "controlled_vacuum_packed": "cvp",
    "boneless_skinless": "boneless skinless",
    "beer_battered": "battered",
}

# Central alias dictionary for product terms + vendor shorthand.
PRODUCT_ALIASES = {
    "squid": "calamari",
    "calamari": "calamari",
    "aplwd": "applewood",
    "l/o": "laid out",
    "laid out": "laid out",
    "bb": "beer battered",
    "brst": "breast",
    "tndr": "tender",
    "ched": "cheddar",
    "mozz": "mozzarella",
    "whi": "white",
    "hngd": "hinged",
    "cmpt": "compartment",
    "fz": "frozen",
    "ref": "refrigerated",
}
WEAK_TOKENS = {
    "lb",
    "lbs",
    "fresh",
    "frozen",
    "raw",
    "refrigerated",
    "vac",
    "bag",
    "box",
    "pack",
    "count",
    "whole",
    "stage",
    "grade",
    "fancy",
    "premium",
    "natural",
    "fall",
    "target",
    "average",
    "optional",
    "dual",
    "tab",
    "wild",
    "source",
    "west",
    "creek",
    "silver",
    "mark",
    "clear",
    "yellow",
    "blue",
    "vacuum",
    "packed",
}
ATTRIBUTE_TERMS = {
    "boneless",
    "skinless",
    "heavy",
    "whipping",
    "whipped",
    "hinged",
    "white",
    "diced",
    "smoked",
    "double",
    "fully",
    "cooked",
    "sliced",
    "shredded",
    "trans",
    "fat",
    "free",
    "nonthermostabilized",
    "battered",
    "breaded",
    "liquid",
    "blend",
    "alternative",
}
SIZE_UNIT_TOKENS = {"oz", "lb", "lbs", "ct", "count", "percent", "cmpt", "compartment"}


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


def parse_pack_size_for_unit_price(pack_size_text: str) -> Tuple[Optional[float], str, str]:
    text = (pack_size_text or "").strip().lower()
    if not text:
        return None, "", "empty pack size"
    text = re.sub(r"\s+", " ", text)

    unit_map = {
        "ct": "ct",
        "count": "ct",
        "each": "each",
        "ea": "each",
        "lb": "lb",
        "lbs": "lb",
        "gal": "gal",
        "gallon": "gal",
        "qt": "qt",
        "quart": "qt",
        "pt": "pt",
        "pint": "pt",
        "floz": "fl oz",
        "fl": "fl oz",
        "oz": "oz",
        "dozen": "dozen",
        "dz": "dozen",
    }

    pack_match = re.match(r"^(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*([a-z]+)$", text)
    if pack_match:
        outer = float(pack_match.group(1))
        inner = float(pack_match.group(2))
        raw_unit = pack_match.group(3)
        unit = unit_map.get(raw_unit, raw_unit)
        qty = outer * inner
        if unit == "dozen":
            return qty * 12.0, "each", ""
        return qty, unit, ""

    simple_match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", text)
    if simple_match:
        qty = float(simple_match.group(1))
        raw_unit = simple_match.group(2)
        unit = unit_map.get(raw_unit, raw_unit)
        if unit == "dozen":
            return qty * 12.0, "each", ""
        return qty, unit, ""

    return None, "", f"unsupported pack size format: {pack_size_text}"


def normalize_count_unit(unit: str) -> str:
    return "each" if unit in {"ct", "count", "ea", "each"} else unit


def convert_to_preferred_unit(total_quantity: Optional[float], unit_type: str, preferred_unit: str = "") -> Tuple[Optional[float], str, str]:
    if total_quantity is None or total_quantity <= 0:
        return None, "", "missing quantity"
    unit = normalize_count_unit((unit_type or "").strip().lower())
    requested = (preferred_unit or "").strip().lower()
    weight_units = {"lb", "oz"}
    liquid_units = {"gal", "qt", "pt", "fl oz"}
    count_units = {"each"}

    if unit in weight_units:
        target = requested if requested in {"lb", "oz"} else "oz"
        converted = total_quantity * 16.0 if unit == "lb" and target == "oz" else total_quantity / 16.0 if unit == "oz" and target == "lb" else total_quantity
        return converted, target, ""
    if unit in liquid_units:
        to_fl_oz = {"gal": 128.0, "qt": 32.0, "pt": 16.0, "fl oz": 1.0}
        base_fl_oz = total_quantity * to_fl_oz[unit]
        target = requested if requested == "fl oz" else "fl oz"
        return base_fl_oz, target, ""
    if unit in count_units:
        return total_quantity, "each", ""
    return None, "", f"unsupported unit type: {unit_type}"


def is_low_confidence_pack_size(pack_size_text: str, unit_type: str, parse_error: str) -> bool:
    text = (pack_size_text or "").strip().lower()
    if not text:
        return True
    if parse_error:
        return True
    # Simple rule-based confidence: valid parse, but unknown unit is still low confidence.
    known_units = {"ct", "count", "each", "lb", "oz", "gal", "qt", "pt", "fl oz"}
    if unit_type not in known_units:
        return True
    return False


# Build a simple cleaned description for matching similar products across files.
# Rules:
# 1) lowercase + trim
# 2) convert common abbreviations to full words
# 3) remove punctuation + normalize spaces
# 4) apply a few small word-order fixes
def clean_description_for_match(description: str) -> str:
    text = (description or "").lower().strip()

    # Handle known slash-style shorthand before punctuation cleanup.
    text = text.replace("b/s", " boneless skinless ")
    text = text.replace("_", " ")
    text = text.replace("double lobe", "double")
    text = text.replace("l/o", " laid out ")

    # Convert pound shorthand (#) into lb before punctuation cleanup.
    text = re.sub(r"#", " lb ", text)

    # Keep current punctuation/space cleanup behavior.
    text = re.sub(r"[,\.\-/()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Expand common abbreviations token-by-token.
    expanded_tokens: List[str] = []
    for token in text.split():
        replacement = PRODUCT_ALIASES.get(token, token)
        replacement = TERM_REPLACEMENTS.get(replacement, replacement)
        expanded_tokens.extend(replacement.split())
    text = " ".join(expanded_tokens)

    # Small phrase/order fixes for common wording variations.
    text = text.replace("fries french", "french fries")
    text = text.replace("frozen french fries", "french fries")
    text = text.replace("french fries frozen", "french fries")
    text = text.replace("mozzarella shredded cheese", "mozzarella cheese shredded")
    text = text.replace("ground fine beef", "beef ground fine")
    text = text.replace("fine ground beef", "beef ground fine")
    text = text.replace("tempura batter mix", "batter mix tempura")
    text = text.replace("foam container hinged white 1 compartment", "container foam 1 compartment white hinged")
    text = text.replace(
        "chicken breast boneless skinless double 8 oz",
        "chicken breast double boneless skinless 8 oz",
    )
    text = re.sub(r"\s+", " ", text).strip()

    return text


def find_manual_override_group(normalized_description: str) -> str:
    # Manual overrides win first. We compare normalized forms for simple exact matching.
    normalized_input = clean_description_for_match(normalized_description)
    for group_key, phrases in MANUAL_MATCH_OVERRIDES.items():
        normalized_group_key = clean_description_for_match(group_key)
        if normalized_input == normalized_group_key:
            return group_key
        for phrase in phrases:
            if normalized_input == clean_description_for_match(phrase):
                return group_key
    return ""


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


def split_core_attribute_size_tokens(tokens: List[str]) -> Tuple[List[str], List[str], List[str]]:
    core_tokens: List[str] = []
    attribute_tokens: List[str] = []
    size_tokens: List[str] = []

    for token in tokens:
        # Treat quantity/size-like values as lower-priority signals.
        if token.isdigit() or token in SIZE_UNIT_TOKENS or re.search(r"\d", token):
            size_tokens.append(token)
        elif token in ATTRIBUTE_TERMS:
            attribute_tokens.append(token)
        elif token in WEAK_TOKENS:
            continue
        else:
            core_tokens.append(token)

    return core_tokens, attribute_tokens, size_tokens


def detect_family_alias(core_tokens: List[str], attribute_tokens: List[str]) -> str:
    core = set(core_tokens)
    attr = set(attribute_tokens)

    if {"onion", "rings"}.issubset(core):
        return "onion rings"
    if {"avocado", "hass"}.issubset(core):
        return "avocado hass"
    if "bacon" in core and "sliced" in attr:
        return "bacon sliced"
    if {"bacon", "topping", "diced"}.issubset(core | attr):
        return "bacon topping diced"
    if {"batter", "mix", "tempura"}.issubset(core | attr):
        return "tempura batter mix"
    if {"beef", "ground"}.issubset(core | attr):
        return "ground beef"
    if {"vegan", "burger", "patty"}.issubset(core | attr):
        return "vegan burger patty"
    if {"bleach", "germicidal"}.issubset(core | attr):
        return "bleach germicidal"
    if {"brioche", "bun"}.issubset(core | attr):
        return "brioche bun"
    if {"foam", "container"}.issubset(core | attr):
        return "foam container"
    if {"cheddar", "cheese"}.issubset(core | attr):
        return "cheddar cheese"
    if {"mozzarella", "cheese"}.issubset(core | attr):
        return "mozzarella cheese"
    if {"chicken", "breast"}.issubset(core):
        if {"boneless", "skinless"}.issubset(attr):
            return "chicken breast boneless skinless"
        return "chicken breast"
    if {"chicken", "tender"}.issubset(core | attr) and "breaded" in attr:
        return "chicken tender breaded"
    if {"chicken", "wing"}.issubset(core):
        return "chicken wing"
    if "cream" in core and ("heavy" in attr or "whipping" in attr):
        return "heavy whipping cream"
    if {"french", "fries"}.issubset(core):
        return "french fries"
    return ""


def infer_product_family_from_tokens(core_tokens: List[str]) -> str:
    token_set = set(core_tokens)

    if {"chicken", "breast"}.issubset(token_set):
        return "chicken breast"
    if {"ground", "beef"}.issubset(token_set) or {"beef", "ground"}.issubset(token_set):
        return "ground beef"
    if {"bun", "brioche"}.issubset(token_set):
        return "brioche bun"
    if {"bun", "hamburger"}.issubset(token_set):
        return "hamburger bun"
    if "fries" in token_set:
        return "french fries"
    if "avocado" in token_set:
        return "avocado"
    if {"cheese", "mozzarella"}.issubset(token_set):
        return "mozzarella cheese"
    if {"cheese", "cheddar"}.issubset(token_set):
        return "cheddar cheese"

    if core_tokens:
        # Last-resort readable fallback so every row still gets a family key.
        return " ".join(core_tokens[:2]).strip()
    return "unclassified item"


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
        "price_selection_reason": "",
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

    likely_price_columns = [
        name for name in fieldnames if normalize(name) in PRICE_HEADER_CANDIDATES
    ]
    price_selection_reason = ""

    # If user provided manual mapping, use those selections.
    # If not, use automatic guesses.
    if mapping:
        desc_col = mapping.get("description") or desc_col
        item_col = mapping.get("item_number") or item_col
        pack_col = mapping.get("pack_size") or pack_col
        price_col = mapping.get("price") or price_col
        if mapping.get("price"):
            price_selection_reason = "manual mapping selected price column"
    else:
        if price_col:
            price_selection_reason = "auto-detected standard price header"
        elif len(likely_price_columns) == 1:
            price_col = likely_price_columns[0]
            price_selection_reason = "auto-selected because exactly one likely price header exists"
        elif len(likely_price_columns) > 1:
            price_selection_reason = "multiple likely price headers found; manual selection may be needed"
        else:
            price_selection_reason = "no likely price header found"

    debug_info["selected_columns"] = {
        "description": desc_col or "",
        "item_number": item_col or "",
        "pack_size": pack_col or "",
        "price": price_col or "",
    }
    debug_info["price_selection_reason"] = price_selection_reason

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

        parsed_price = parse_price_to_float(raw_price)

        parsed_row = {
            "vendor": vendor_name,
            "description": description,
            "item_number": item_number,
            "pack_size": pack_size,
            "price": parsed_price,
            "normalized_description": clean_description_for_match(description),
            "alias_expanded_description": clean_description_for_match(description),
            "final_tokens": build_meaningful_tokens(clean_description_for_match(description)),
            "core_tokens": [],
            "attribute_tokens": [],
            "size_tokens": [],
            "product_family": "",
            "inferred_product_family": "",
            "override_group_hit": "",
            "match_confidence": 0.0,
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
                    "alias_expanded_description": parsed_row["alias_expanded_description"],
                    "final_tokens": parsed_row["final_tokens"],
                    "core_tokens": parsed_row["core_tokens"],
                    "attribute_tokens": parsed_row["attribute_tokens"],
                    "size_tokens": parsed_row["size_tokens"],
                    "product_family": parsed_row["product_family"],
                    "inferred_product_family": parsed_row["inferred_product_family"],
                    "override_group_hit": parsed_row["override_group_hit"],
                    "match_confidence": parsed_row["match_confidence"],
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
            "price_selection_reason": "",
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
            "price_selection_reason": "",
            "mapping_needed": False,
            "header_row_index": 0,
            "skipped_intro_rows": 0,
        }
        return [], [f"{vendor_name}: Please upload a UTF-8 CSV file."], debug_info, ""

    rows, errors, debug_info = parse_vendor_text(vendor_name, text, mapping)
    return rows, errors, debug_info, text


# Combine rows from all vendors using token-overlap + similarity matching.
def build_comparison_rows(
    rows: List[Dict[str, Optional[float]]], match_memory: Dict[str, Any]
) -> Tuple[
    List[Dict[str, str]],
    List[Dict[str, str]],
    List[Dict[str, str]],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, int],
    List[Dict[str, Any]],
]:
    combined: Dict[str, Dict[str, Any]] = {}
    match_debug_rows: List[Dict[str, str]] = []
    possible_matches: List[Dict[str, str]] = []
    review_stats: Dict[str, Any] = {}
    review_pair_keys: set[str] = set()
    review_debug_reasons: List[str] = []
    parsed_entries: List[Dict[str, Any]] = []
    forced_description_to_group, forced_groups_debug = build_forced_group_assignments(match_memory)

    high_conf_threshold = 0.80
    medium_conf_threshold = 0.60
    stats = {"auto_grouped": 0, "sent_to_review": 0, "left_unmatched": 0}

    for row in rows:
        original_description = (row.get("description") or "").strip()
        if original_description == "":
            original_description = "(No description)"

        normalized_description = str(
            row.get("normalized_description") or clean_description_for_match(original_description)
        )
        alias_expanded_description = str(
            row.get("alias_expanded_description") or normalized_description
        )
        tokens = build_meaningful_tokens(normalized_description)
        core_tokens, attribute_tokens, size_tokens = split_core_attribute_size_tokens(tokens)
        override_group_hit = find_manual_override_group(alias_expanded_description)
        product_family = ""
        inferred_product_family = ""
        forced_group_key = forced_description_to_group.get(clean_description_for_match(original_description), "")

        if override_group_hit:
            product_family = override_group_hit
        else:
            product_family = detect_family_alias(core_tokens, attribute_tokens)
            if not product_family:
                inferred_product_family = infer_product_family_from_tokens(core_tokens)
                product_family = inferred_product_family

        best_group_key = ""
        best_confidence = 0.0
        best_candidate_description = ""
        best_core_overlap = 0.0
        best_size_overlap = 0.0
        best_candidate_alias_expanded = ""
        for group_key, group_data in combined.items():
            core_overlap = token_overlap_score(core_tokens, list(group_data["core_tokens"]))
            attribute_overlap = token_overlap_score(attribute_tokens, list(group_data["attribute_tokens"]))
            size_overlap = token_overlap_score(size_tokens, list(group_data["size_tokens"]))
            family_bonus = 1.0 if product_family and product_family == group_data.get("product_family", "") else 0.0
            confidence = (
                (0.45 * family_bonus)
                + (0.35 * core_overlap)
                + (0.15 * attribute_overlap)
                + (0.05 * size_overlap)
            )
            if confidence > best_confidence:
                best_confidence = confidence
                best_group_key = group_key
                best_candidate_description = str(group_data["display_description"])
                best_core_overlap = core_overlap
                best_size_overlap = size_overlap
                best_candidate_alias_expanded = str(group_data.get("normalized", ""))

        confirmed_group = ""
        if best_group_key:
            pair_key = build_pair_key(alias_expanded_description, best_candidate_description)
            confirmed_group = match_memory.get("confirmed", {}).get(pair_key, "")
            is_rejected = pair_key in match_memory.get("rejected", set())
        else:
            pair_key = ""
            is_rejected = False

        if forced_group_key:
            final_group_key = forced_group_key
            match_confidence = 1.0
            stats["auto_grouped"] += 1
        elif confirmed_group:
            final_group_key = confirmed_group
            match_confidence = 1.0
            stats["auto_grouped"] += 1
        elif best_group_key and (not is_rejected) and best_confidence >= high_conf_threshold:
            final_group_key = best_group_key
            match_confidence = best_confidence
            stats["auto_grouped"] += 1
        elif best_group_key and (not is_rejected):
            review_reasons: List[str] = []
            if best_confidence >= medium_conf_threshold:
                review_reasons.append("medium confidence")
            if product_family and product_family == combined.get(best_group_key, {}).get("product_family", ""):
                review_reasons.append("same product_family")
            if best_core_overlap >= 0.50:
                review_reasons.append("strong core token overlap")
            if best_size_overlap >= 0.40 and best_core_overlap >= 0.20:
                review_reasons.append("similar size/pack")
            if token_overlap_score(
                build_meaningful_tokens(alias_expanded_description),
                build_meaningful_tokens(best_candidate_alias_expanded),
            ) >= 0.45:
                review_reasons.append("alias/synonym overlap")

            left_tokens = original_description.lower().split()
            right_tokens = best_candidate_description.lower().split()
            left_has_shorthand = any("/" in t or "_" in t or len(t) <= 3 for t in left_tokens)
            right_has_shorthand = any("/" in t or "_" in t or len(t) <= 3 for t in right_tokens)
            if left_has_shorthand != right_has_shorthand and best_core_overlap >= 0.20:
                review_reasons.append("shorthand vs long description")

            should_review = len(review_reasons) > 0
            if should_review and pair_key and pair_key not in review_pair_keys:
                review_pair_keys.add(pair_key)
                possible_matches.append(
                    {
                        "review_id": str(uuid.uuid4()),
                        "vendor_1_description": original_description,
                        "vendor_2_description": best_candidate_description,
                        "confidence": f"{best_confidence:.2f}",
                        "pair_key": pair_key,
                        "proposed_group_key": best_group_key,
                        "reasons": ", ".join(review_reasons),
                    }
                )
                review_debug_reasons.append(
                    f"{original_description} <> {best_candidate_description} because: {', '.join(review_reasons)}"
                )
                stats["sent_to_review"] += 1

            final_group_key = f"{product_family}::{normalized_description}"
            match_confidence = best_confidence
            if not should_review:
                stats["left_unmatched"] += 1
        else:
            final_group_key = f"{product_family}::{normalized_description}"
            match_confidence = best_confidence if best_group_key else 1.0
            stats["left_unmatched"] += 1

        if final_group_key not in combined:
            combined[final_group_key] = {
                "display_description": original_description,
                "normalized": normalized_description,
                "core_tokens": set(core_tokens),
                "attribute_tokens": set(attribute_tokens),
                "size_tokens": set(size_tokens),
                "product_family": product_family,
                "sysco": None,
                "us_foods": None,
                "pfg": None,
                "sysco_pack_size": "",
                "us_foods_pack_size": "",
                "pfg_pack_size": "",
            }
        else:
            combined[final_group_key]["display_description"] = choose_clearer_description(
                str(combined[final_group_key]["display_description"]), original_description
            )
            combined[final_group_key]["core_tokens"].update(core_tokens)
            combined[final_group_key]["attribute_tokens"].update(attribute_tokens)
            combined[final_group_key]["size_tokens"].update(size_tokens)

        row["core_tokens"] = core_tokens
        row["attribute_tokens"] = attribute_tokens
        row["size_tokens"] = size_tokens
        row["product_family"] = product_family
        row["inferred_product_family"] = inferred_product_family
        row["override_group_hit"] = override_group_hit
        row["match_confidence"] = round(match_confidence, 2)
        row["final_group_key"] = final_group_key
        row["final_tokens"] = tokens

        match_debug_rows.append(
            {
                "description": original_description,
                "normalized_description": normalized_description,
                "alias_expanded_description": alias_expanded_description,
                "override_group_hit": override_group_hit,
                "product_family": product_family,
                "inferred_product_family": inferred_product_family,
                "core_tokens": ", ".join(core_tokens),
                "attribute_tokens": ", ".join(attribute_tokens),
                "size_tokens": ", ".join(size_tokens),
                "match_confidence": f"{match_confidence:.2f}",
                "final_group_key": final_group_key,
            }
        )

        vendor = str(row.get("vendor") or "")
        parsed_entries.append(
            {
                "vendor": vendor,
                "description": original_description,
                "normalized_description": normalized_description,
                "alias_expanded_description": alias_expanded_description,
                "product_family": product_family,
                "core_tokens": core_tokens,
                "attribute_tokens": attribute_tokens,
                "size_tokens": size_tokens,
                "final_group_key": final_group_key,
            }
        )

        price = row.get("price")
        pack_size_text = str(row.get("pack_size") or "")
        if vendor == "Sysco":
            current = combined[final_group_key]["sysco"]
            if current is None or (price is not None and price < current):
                combined[final_group_key]["sysco"] = price
                combined[final_group_key]["sysco_pack_size"] = pack_size_text
        elif vendor == "US Foods":
            current = combined[final_group_key]["us_foods"]
            if current is None or (price is not None and price < current):
                combined[final_group_key]["us_foods"] = price
                combined[final_group_key]["us_foods_pack_size"] = pack_size_text
        elif vendor == "PFG":
            current = combined[final_group_key]["pfg"]
            if current is None or (price is not None and price < current):
                combined[final_group_key]["pfg"] = price
                combined[final_group_key]["pfg_pack_size"] = pack_size_text

    output: List[Dict[str, str]] = []
    unit_review_items: List[Dict[str, Any]] = []
    unit_price_errors: List[str] = []
    unit_corrections = match_memory.get("unit_corrections", {})
    unit_corrections_applied = 0
    for group_key, prices in sorted(combined.items(), key=lambda item: item[1]["display_description"].lower()):
        vendor_prices = {
            "Sysco": prices["sysco"],
            "US Foods": prices["us_foods"],
            "PFG": prices["pfg"],
        }
        available = {vendor: value for vendor, value in vendor_prices.items() if value is not None}
        cheapest_vendor = min(available, key=available.get) if available else ""

        per_vendor_unit: Dict[str, Dict[str, Any]] = {}
        for vendor_name, vendor_key in [("Sysco", "sysco"), ("US Foods", "us_foods"), ("PFG", "pfg")]:
            case_price = prices[vendor_key]
            pack_size = str(prices.get(f"{vendor_key}_pack_size", "") or "")
            qty, unit_type, err = parse_pack_size_for_unit_price(pack_size)
            correction_key = f"{group_key}::{vendor_key}"
            correction = unit_corrections.get(correction_key, {})
            preferred_comparison_unit = ""
            if isinstance(correction, dict):
                corrected_qty = correction.get("total_unit_quantity")
                corrected_unit_type = correction.get("unit_type")
                preferred_comparison_unit = str(correction.get("preferred_comparison_unit", "")).strip().lower()
                if corrected_qty not in [None, ""] and corrected_unit_type:
                    try:
                        qty = float(corrected_qty)
                        unit_type = str(corrected_unit_type).strip().lower()
                        err = ""
                        unit_corrections_applied += 1
                    except Exception:
                        pass
            converted_qty, converted_unit, conversion_err = convert_to_preferred_unit(qty, unit_type, preferred_comparison_unit)
            unit_price = None
            formula_used = ""
            if case_price is not None and converted_qty and converted_qty > 0:
                unit_price = float(case_price) / float(converted_qty)
                formula_used = f"{case_price} / {converted_qty} {converted_unit}"
            elif case_price is not None and (err or conversion_err):
                unit_price_errors.append(f"{prices['display_description']} [{vendor_name}]: {err}")
            per_vendor_unit[vendor_name] = {"unit_price": unit_price, "unit_type": converted_unit}

            low_confidence = is_low_confidence_pack_size(pack_size, unit_type, err)
            suspicious_unit_price = unit_price is not None and (unit_price <= 0 or unit_price > 1000)
            needs_review = (
                (case_price is not None and unit_price is None)
                or (not pack_size)
                or bool(err)
                or suspicious_unit_price
                or low_confidence
            )
            if needs_review:
                unit_review_items.append(
                    {
                        "review_key": correction_key,
                        "vendor_name": vendor_name,
                        "vendor_key": vendor_key,
                        "group_key": group_key,
                        "description": str(prices["display_description"]),
                        "case_price": f"${case_price:.2f}" if case_price is not None else "",
                        "pack_size_text": pack_size,
                        "parsed_quantity": qty if qty is not None else "",
                        "parsed_unit_type": unit_type,
                        "converted_quantity": converted_qty if converted_qty is not None else "",
                        "converted_unit_type": converted_unit,
                        "calculated_unit_price": f"${unit_price:.2f}" if unit_price is not None else "Needs review",
                        "preferred_comparison_unit": preferred_comparison_unit,
                        "formula_used": formula_used if formula_used else "Needs review",
                        "low_confidence": low_confidence,
                        "note": correction.get("note", "") if isinstance(correction, dict) else "",
                    }
                )

        valid_units = {
            vendor: data
            for vendor, data in per_vendor_unit.items()
            if data["unit_price"] is not None and data["unit_type"]
        }
        if not valid_units:
            cheapest_by_unit = "Needs review"
        else:
            unit_types = {str(data["unit_type"]) for data in valid_units.values()}
            if len(unit_types) != 1:
                cheapest_by_unit = "Unit mismatch — review needed"
            else:
                cheapest_by_unit = min(valid_units, key=lambda v: float(valid_units[v]["unit_price"]))
        output.append(
            {
                "description": str(prices["display_description"]),
                "sysco": f"${prices['sysco']:.2f}" if prices["sysco"] is not None else "",
                "us_foods": f"${prices['us_foods']:.2f}" if prices["us_foods"] is not None else "",
                "pfg": f"${prices['pfg']:.2f}" if prices["pfg"] is not None else "",
                "cheapest_vendor": cheapest_vendor,
                "sysco_unit_price": (
                    f"${per_vendor_unit['Sysco']['unit_price']:.2f} per {per_vendor_unit['Sysco']['unit_type']}"
                    if per_vendor_unit["Sysco"]["unit_price"] is not None and per_vendor_unit["Sysco"]["unit_type"]
                    else "Needs review"
                ),
                "us_foods_unit_price": (
                    f"${per_vendor_unit['US Foods']['unit_price']:.2f} per {per_vendor_unit['US Foods']['unit_type']}"
                    if per_vendor_unit["US Foods"]["unit_price"] is not None and per_vendor_unit["US Foods"]["unit_type"]
                    else "Needs review"
                ),
                "pfg_unit_price": (
                    f"${per_vendor_unit['PFG']['unit_price']:.2f} per {per_vendor_unit['PFG']['unit_type']}"
                    if per_vendor_unit["PFG"]["unit_price"] is not None and per_vendor_unit["PFG"]["unit_type"]
                    else "Needs review"
                ),
                "cheapest_by_unit": cheapest_by_unit,
                "row_group_key": group_key,
            }
        )

    review_stats = {
        "possible_matches_generated": len(possible_matches),
        "review_reasons": review_debug_reasons,
        "auto_grouped": stats["auto_grouped"],
        "sent_to_review": stats["sent_to_review"],
        "left_unmatched": stats["left_unmatched"],
        "forced_group_keys_created": len(forced_groups_debug),
        "forced_group_assignments": forced_groups_debug,
        "matching_completed_before_unit_price": "yes",
        "grouped_rows_before_unit_price": len(combined),
        "unit_price_calculation_errors": unit_price_errors[:20],
        "unit_corrections_loaded_from_memory": len(unit_corrections),
        "unit_corrections_applied": unit_corrections_applied,
        "rows_still_needing_unit_review": len(unit_review_items),
    }

    us_items = [entry for entry in parsed_entries if entry["vendor"] == "US Foods"]
    pfg_items = [entry for entry in parsed_entries if entry["vendor"] == "PFG"]
    total_pair_comparisons = 0

    pair_scores: Dict[str, List[Dict[str, Any]]] = {}
    for us_item in us_items:
        key = f"US Foods::{us_item['description']}"
        pair_scores[key] = []
        for pfg_item in pfg_items:
            pair_key = build_pair_key(str(us_item["description"]), str(pfg_item["description"]))
            if pair_key in match_memory.get("rejected", set()):
                continue
            total_pair_comparisons += 1
            family_match = 1.0 if us_item.get("product_family") and us_item.get("product_family") == pfg_item.get("product_family") else 0.0
            core_overlap = token_overlap_score(list(us_item["core_tokens"]), list(pfg_item["core_tokens"]))
            alias_overlap = token_overlap_score(
                build_meaningful_tokens(str(us_item["alias_expanded_description"])),
                build_meaningful_tokens(str(pfg_item["alias_expanded_description"])),
            )
            description_similarity = SequenceMatcher(
                None,
                str(us_item["normalized_description"]),
                str(pfg_item["normalized_description"]),
            ).ratio()
            size_overlap = token_overlap_score(list(us_item["size_tokens"]), list(pfg_item["size_tokens"]))
            score = (
                (0.35 * family_match)
                + (0.25 * core_overlap)
                + (0.20 * alias_overlap)
                + (0.15 * description_similarity)
                + (0.05 * size_overlap)
            )
            pair_scores[key].append(
                {
                    "review_id": str(uuid.uuid4()),
                    "vendor_1_description": str(us_item["description"]),
                    "vendor_2_description": str(pfg_item["description"]),
                    "pair_key": pair_key,
                    "proposed_group_key": str(pfg_item["final_group_key"]),
                    "score": round(score, 2),
                    "product_family_match": "yes" if family_match else "no",
                    "shared_core_tokens": ", ".join(sorted(set(us_item["core_tokens"]) & set(pfg_item["core_tokens"]))),
                    "alias_overlap": round(alias_overlap, 2),
                    "description_similarity": round(description_similarity, 2),
                    "size_overlap": round(size_overlap, 2),
                }
            )
        pair_scores[key].sort(key=lambda item: item["score"], reverse=True)

    reverse_scores: Dict[str, List[Dict[str, Any]]] = {}
    for pfg_item in pfg_items:
        key = f"PFG::{pfg_item['description']}"
        reverse_scores[key] = []
        for us_item in us_items:
            pair_key = build_pair_key(str(us_item["description"]), str(pfg_item["description"]))
            if pair_key in match_memory.get("rejected", set()):
                continue
            pool_key = f"US Foods::{us_item['description']}"
            candidate = next((item for item in pair_scores.get(pool_key, []) if item["pair_key"] == pair_key), None)
            if candidate:
                reverse_scores[key].append(candidate)
        reverse_scores[key].sort(key=lambda item: item["score"], reverse=True)

    def has_confirmed_candidate(candidates: List[Dict[str, Any]]) -> bool:
        for candidate in candidates[:5]:
            if match_memory.get("confirmed", {}).get(candidate["pair_key"]):
                return True
        return False

    match_review_buckets: Dict[str, Any] = {
        "high_confidence_auto_matches": [],
        "needs_review": [],
        "no_likely_match_found": [],
        "other_vendor_options": {
            "US Foods": [entry["description"] for entry in us_items],
            "PFG": [entry["description"] for entry in pfg_items],
        },
    }

    def add_bucket_entry(source_vendor: str, source_description: str, candidates: List[Dict[str, Any]]) -> None:
        top_five = candidates[:5]
        top_score = top_five[0]["score"] if top_five else 0.0
        entry = {
            "source_vendor": source_vendor,
            "source_description": source_description,
            "top_candidates": top_five,
        }
        if has_confirmed_candidate(top_five) or top_score >= 0.85:
            match_review_buckets["high_confidence_auto_matches"].append(entry)
        elif top_score >= 0.45:
            match_review_buckets["needs_review"].append(entry)
        else:
            match_review_buckets["no_likely_match_found"].append(entry)

    for us_item in us_items:
        source_key = f"US Foods::{us_item['description']}"
        add_bucket_entry("US Foods", str(us_item["description"]), pair_scores.get(source_key, []))

    for pfg_item in pfg_items:
        source_key = f"PFG::{pfg_item['description']}"
        add_bucket_entry("PFG", str(pfg_item["description"]), reverse_scores.get(source_key, []))

    match_matrix_stats = {
        "total_us_foods_items": len(us_items),
        "total_pfg_items": len(pfg_items),
        "total_pair_comparisons_created": total_pair_comparisons,
        "high_confidence_matches": len(match_review_buckets["high_confidence_auto_matches"]),
        "needs_review": len(match_review_buckets["needs_review"]),
        "no_match_found": len(match_review_buckets["no_likely_match_found"]),
    }

    return output, match_debug_rows, possible_matches, review_stats, match_review_buckets, match_matrix_stats, unit_review_items


@app.route("/", methods=["GET", "POST"])
def index():
    comparison_rows: List[Dict[str, str]] = []
    unit_review_items: List[Dict[str, Any]] = []
    match_debug_rows: List[Dict[str, str]] = []
    possible_matches: List[Dict[str, str]] = []
    review_stats: Dict[str, Any] = {}
    errors: List[str] = []
    review_success_messages: List[str] = []
    review_error_messages: List[str] = []
    review_batch_debug: Dict[str, Any] = {}
    match_review_buckets: Dict[str, Any] = {}
    match_matrix_stats: Dict[str, int] = {}
    fatal_error: Optional[Dict[str, str]] = None
    debug_details: List[Dict[str, Any]] = []
    mapping_options: Dict[str, Any] = {}
    show_mapping_form = False
    upload_id = ""
    upload_debug: Dict[str, Any] = {
        "request_method": request.method,
        "files_keys": [],
        "received": {"Sysco": False, "US Foods": False, "PFG": False},
    }
    debug_counters: Dict[str, Any] = {
        "rows_parsed_per_vendor": {"Sysco": 0, "US Foods": 0, "PFG": 0},
        "rows_grouped": 0,
        "review_candidates_generated": 0,
        "confirmed_matches_loaded": 0,
        "forced_matches_applied": 0,
        "review_items_remaining": 0,
        "match_memory_loaded": STARTUP_MATCH_MEMORY_STATUS["loaded"],
        "confirmed_matches_in_file": STARTUP_MATCH_MEMORY_STATUS["confirmed"],
        "rejected_matches_in_file": STARTUP_MATCH_MEMORY_STATUS["rejected"],
        "imported_memory_loaded": False,
        "confirmed_matches_imported": 0,
        "rejected_matches_imported": 0,
        "confirmed_matches_in_session": 0,
        "rejected_matches_in_session": 0,
        "confirmed_matches_in_export": 0,
        "rejected_matches_in_export": 0,
        "unit_corrections_saved": 0,
    }

    if request.method == "POST":
        failed_step = "initialization"
        all_vendor_rows: List[Dict[str, Optional[float]]] = []
        action = request.form.get("action", "upload")
        try:
            upload_id = request.form.get("upload_id", "")
            match_memory = load_match_memory()
            debug_counters["confirmed_matches_loaded"] = len(match_memory.get("confirmed", {}))
            debug_counters["match_memory_loaded"] = bool(
                debug_counters["confirmed_matches_loaded"] or len(match_memory.get("rejected", set()))
            )

            session_id = get_session_id()
            session_memory = SESSION_REVIEW_MEMORY.get(
                session_id, {"confirmed": {}, "rejected": set(), "unit_corrections": {}}
            )
            match_memory["confirmed"].update(session_memory.get("confirmed", {}))
            match_memory["rejected"].update(set(session_memory.get("rejected", set())))
            match_memory["unit_corrections"].update(session_memory.get("unit_corrections", {}))
            fallback_memory = session.get("match_memory_fallback", {})
            if isinstance(fallback_memory, dict):
                match_memory["confirmed"].update(fallback_memory.get("confirmed", {}))
                match_memory["rejected"].update(set(fallback_memory.get("rejected", [])))
                match_memory["unit_corrections"].update(fallback_memory.get("unit_corrections", {}))
            debug_counters["confirmed_matches_in_session"] = len(match_memory.get("confirmed", {}))
            debug_counters["rejected_matches_in_session"] = len(match_memory.get("rejected", set()))
            last_export_counts = session.get("last_export_counts", {})
            if isinstance(last_export_counts, dict):
                debug_counters["confirmed_matches_in_export"] = int(last_export_counts.get("confirmed", 0))
                debug_counters["rejected_matches_in_export"] = int(last_export_counts.get("rejected", 0))

            failed_step = "file upload"
            upload_debug["files_keys"] = list(request.files.keys())
            sysco_file = request.files.get("sysco_file")
            usfoods_file = request.files.get("usfoods_file")
            pfg_file = request.files.get("pfg_file")
            upload_debug["received"]["Sysco"] = bool(sysco_file and sysco_file.filename)
            upload_debug["received"]["US Foods"] = bool(usfoods_file and usfoods_file.filename)
            upload_debug["received"]["PFG"] = bool(pfg_file and pfg_file.filename)

            vendor_keys = [("Sysco", "sysco_file", "sysco"), ("US Foods", "usfoods_file", "usfoods"), ("PFG", "pfg_file", "pfg")]

            if action == "upload":
                upload_id = str(uuid.uuid4())
                UPLOAD_CACHE[upload_id] = {}
                session["last_upload_id"] = upload_id
                vendors = [("Sysco", sysco_file), ("US Foods", usfoods_file), ("PFG", pfg_file)]
                for vendor_name, file_obj in vendors:
                    failed_step = "header detection"
                    rows, file_errors, debug_info, file_text = parse_vendor_csv(vendor_name, file_obj)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)
                    if file_text.strip():
                        UPLOAD_CACHE[upload_id][vendor_name] = file_text
                    if debug_info.get("uploaded"):
                        missing_required = not debug_info["selected_columns"].get("description") or not debug_info["selected_columns"].get("price")
                        if missing_required:
                            show_mapping_form = True
                            vendor_key = next((key for name, _, key in vendor_keys if name == vendor_name), "")
                            mapping_options[vendor_key] = {
                                "vendor_name": vendor_name,
                                "headers": debug_info.get("headers", []),
                                "selected": debug_info.get("selected_columns", {}),
                            }

            elif action == "apply_mapping":
                failed_step = "parsing"
                if not upload_id:
                    upload_id = session.get("last_upload_id", "")
                cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})
                for vendor_name, _, vendor_key in vendor_keys:
                    file_text = cached_vendor_files.get(vendor_name, "")
                    if not file_text:
                        debug_details.append({
                            "vendor": vendor_name,
                            "uploaded": False,
                            "headers": [],
                            "sample_rows": [],
                            "parser_path": "not used",
                            "delimiter": "",
                            "selected_columns": {},
                            "price_selection_reason": "",
                            "mapping_needed": False,
                            "header_row_index": 0,
                            "skipped_intro_rows": 0,
                        })
                        continue
                    mapping = {
                        "description": request.form.get(f"{vendor_key}_description", ""),
                        "item_number": request.form.get(f"{vendor_key}_item_number", ""),
                        "pack_size": request.form.get(f"{vendor_key}_pack_size", ""),
                        "price": request.form.get(f"{vendor_key}_price", ""),
                    }
                    rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, mapping)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)
                    if debug_info.get("mapping_needed"):
                        show_mapping_form = True
                        mapping_options[vendor_key] = {
                            "vendor_name": vendor_name,
                            "headers": debug_info.get("headers", []),
                            "selected": debug_info.get("selected_columns", {}),
                        }

            elif action == "review_decision":
                failed_step = "review save"
                try:
                    decision = request.form.get("decision", "")
                    vendor_1_description = request.form.get("vendor_1_description", "")
                    vendor_2_description = request.form.get("vendor_2_description", "")
                    proposed_group_key = request.form.get("proposed_group_key", "")
                    if not vendor_2_description:
                        vendor_2_description = request.form.get("selected_vendor_2_description", "")
                    if not proposed_group_key:
                        proposed_group_key = request.form.get("selected_proposed_group_key", "")
                    if not proposed_group_key and vendor_2_description:
                        proposed_group_key = clean_description_for_match(vendor_2_description)
                    review_id = request.form.get("review_id", "")
                    pair_key = request.form.get("pair_key", "") or build_pair_key(vendor_1_description, vendor_2_description)
                    debug_prefix = (
                        f"Review submission id={review_id}, action={decision}, item1='{vendor_1_description}', "
                        f"item2='{vendor_2_description}', proposed_group_key='{proposed_group_key}'"
                    )
                    if pair_key and decision == "match":
                        match_memory["confirmed"][pair_key] = proposed_group_key
                        match_memory["rejected"].discard(pair_key)
                    elif pair_key and decision == "keep_separate":
                        match_memory["rejected"].add(pair_key)
                        match_memory["confirmed"].pop(pair_key, None)
                    else:
                        raise ValueError("Missing required review fields or unknown decision value.")

                    storage_location = "server_memory_fallback"
                    save_status = "succeeded"
                    try:
                        save_match_memory(match_memory)
                        if ENABLE_FILE_PERSISTENCE:
                            storage_location = "local_file"
                    except Exception:
                        storage_location = "server_memory_fallback"

                    SESSION_REVIEW_MEMORY[session_id] = {
                        "confirmed": dict(match_memory.get("confirmed", {})),
                        "rejected": set(match_memory.get("rejected", set())),
                        "unit_corrections": dict(match_memory.get("unit_corrections", {})),
                    }
                    session["match_memory_fallback"] = {
                        "confirmed": match_memory.get("confirmed", {}),
                        "rejected": sorted(list(match_memory.get("rejected", set()))),
                        "unit_corrections": match_memory.get("unit_corrections", {}),
                    }

                    message = f"{debug_prefix}, save_status={save_status}, stored_in={storage_location}"
                    print(message)
                    review_success_messages.append(message)
                except Exception as exc:
                    error_message = f"Review submission error: {exc}"
                    print(error_message)
                    review_error_messages.append(error_message)

                if not upload_id:
                    upload_id = session.get("last_upload_id", "")
                cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})
                for vendor_name, _, _ in vendor_keys:
                    file_text = cached_vendor_files.get(vendor_name, "")
                    if not file_text:
                        continue
                    rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, None)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)
            elif action == "submit_all_review_decisions":
                failed_step = "review save"
                confirmed_count = 0
                separated_count = 0
                skipped_count = 0
                decision_error_count = 0
                try:
                    total_cards = int(request.form.get("total_review_cards", "0") or "0")
                    for i in range(total_cards):
                        decision = request.form.get(f"decision_{i}", "skip")
                        vendor_1_description = request.form.get(f"vendor_1_description_{i}", "")
                        vendor_2_description = request.form.get(f"vendor_2_description_{i}", "")
                        proposed_group_key = request.form.get(f"proposed_group_key_{i}", "")
                        selected_vendor_2_description = request.form.get(f"selected_vendor_2_description_{i}", "")
                        selected_proposed_group_key = request.form.get(f"selected_proposed_group_key_{i}", "")

                        if selected_vendor_2_description:
                            vendor_2_description = selected_vendor_2_description
                        if selected_proposed_group_key:
                            proposed_group_key = selected_proposed_group_key
                        if not proposed_group_key and vendor_2_description:
                            proposed_group_key = clean_description_for_match(vendor_2_description)

                        pair_key = request.form.get(f"pair_key_{i}", "")
                        if not pair_key and vendor_1_description and vendor_2_description:
                            pair_key = build_pair_key(vendor_1_description, vendor_2_description)

                        if decision == "match" and pair_key:
                            match_memory["confirmed"][pair_key] = proposed_group_key
                            match_memory["rejected"].discard(pair_key)
                            confirmed_count += 1
                        elif decision == "match":
                            decision_error_count += 1
                            review_error_messages.append(
                                f"Card {i + 1}: Please choose a valid match target before selecting Match."
                            )
                            skipped_count += 1
                        elif decision == "keep_separate" and pair_key:
                            match_memory["rejected"].add(pair_key)
                            match_memory["confirmed"].pop(pair_key, None)
                            separated_count += 1
                        else:
                            skipped_count += 1

                    storage_location = "server_memory_fallback"
                    save_status = "succeeded"
                    try:
                        save_match_memory(match_memory)
                        if ENABLE_FILE_PERSISTENCE:
                            storage_location = "local_file"
                    except Exception:
                        storage_location = "server_memory_fallback"

                    SESSION_REVIEW_MEMORY[session_id] = {
                        "confirmed": dict(match_memory.get("confirmed", {})),
                        "rejected": set(match_memory.get("rejected", set())),
                        "unit_corrections": dict(match_memory.get("unit_corrections", {})),
                    }
                    session["match_memory_fallback"] = {
                        "confirmed": match_memory.get("confirmed", {}),
                        "rejected": sorted(list(match_memory.get("rejected", set()))),
                        "unit_corrections": match_memory.get("unit_corrections", {}),
                    }
                    _, forced_group_debug = build_forced_group_assignments(match_memory)
                    review_success_messages.append(
                        f"Submitted all review decisions: matches confirmed={confirmed_count}, "
                        f"kept separate={separated_count}, skipped={skipped_count}, "
                        f"save_status={save_status}, stored_in={storage_location}, "
                        f"errors={decision_error_count}, forced_group_keys_created={len(forced_group_debug)}"
                    )
                    review_success_messages.append(
                        f"{confirmed_count} confirmed matches applied to comparison table."
                    )
                    review_success_messages.append(
                        f"{confirmed_count} confirmed matches saved to memory"
                    )
                    review_success_messages.append(
                        "Forced group assignments: "
                        + "; ".join(
                            f"{group_key}: {', '.join(descriptions)}"
                            for group_key, descriptions in forced_group_debug.items()
                        )
                        if forced_group_debug
                        else "Forced group assignments: none"
                    )
                    review_batch_debug = {
                        "total_review_decisions_submitted": total_cards,
                        "matches_confirmed": confirmed_count,
                        "matches_rejected": separated_count,
                        "skipped": skipped_count,
                        "decision_errors": decision_error_count,
                        "confirmed_forced_group_keys_created": len(forced_group_debug),
                        "forced_group_assignments": forced_group_debug,
                    }
                except Exception as exc:
                    review_error_messages.append(f"Bulk review submission error: {exc}")

                if not upload_id:
                    upload_id = session.get("last_upload_id", "")
                cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})
                for vendor_name, _, _ in vendor_keys:
                    file_text = cached_vendor_files.get(vendor_name, "")
                    if not file_text:
                        continue
                    rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, None)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)
            elif action == "import_match_memory":
                failed_step = "review save"
                import_file = request.files.get("match_memory_file")
                if import_file and import_file.filename:
                    try:
                        imported_data = json.load(import_file.stream)
                        confirmed = imported_data.get("confirmed", {})
                        rejected = imported_data.get("rejected", [])
                        unit_corrections = imported_data.get("unit_corrections", {})
                        if not isinstance(confirmed, dict) or not isinstance(rejected, list):
                            raise ValueError("Invalid match memory format. Expected confirmed object and rejected list.")
                        if not isinstance(unit_corrections, dict):
                            raise ValueError("Invalid match memory format. Expected unit_corrections object.")
                        match_memory["confirmed"] = dict(confirmed)
                        match_memory["rejected"] = set(rejected)
                        match_memory["unit_corrections"] = dict(unit_corrections)
                        # Do not write to project filesystem on read-only platforms (for example Vercel).
                        # Keep imported memory in session for immediate use in this browser session.
                        # Keep session memory in sync so confirmed pairs apply immediately.
                        SESSION_REVIEW_MEMORY[session_id] = {
                            "confirmed": dict(match_memory.get("confirmed", {})),
                            "rejected": set(match_memory.get("rejected", set())),
                            "unit_corrections": dict(match_memory.get("unit_corrections", {})),
                        }
                        session["match_memory_fallback"] = {
                            "confirmed": match_memory.get("confirmed", {}),
                            "rejected": sorted(list(match_memory.get("rejected", set()))),
                            "unit_corrections": match_memory.get("unit_corrections", {}),
                        }
                        # Update startup-like debug state after successful import.
                        STARTUP_MATCH_MEMORY_STATUS["loaded"] = True
                        STARTUP_MATCH_MEMORY_STATUS["confirmed"] = len(match_memory["confirmed"])
                        STARTUP_MATCH_MEMORY_STATUS["rejected"] = len(match_memory["rejected"])
                        debug_counters["match_memory_loaded"] = True
                        debug_counters["confirmed_matches_in_file"] = len(match_memory["confirmed"])
                        debug_counters["rejected_matches_in_file"] = len(match_memory["rejected"])
                        debug_counters["confirmed_matches_loaded"] = len(match_memory["confirmed"])
                        debug_counters["imported_memory_loaded"] = True
                        debug_counters["confirmed_matches_imported"] = len(match_memory["confirmed"])
                        debug_counters["rejected_matches_imported"] = len(match_memory["rejected"])
                        review_success_messages.append(
                            "Match memory imported and applied."
                        )
                    except Exception as exc:
                        debug_counters["match_memory_loaded"] = False
                        debug_counters["imported_memory_loaded"] = False
                        review_error_messages.append(f"Import match memory failed: {exc}")
                else:
                    debug_counters["match_memory_loaded"] = False
                    debug_counters["imported_memory_loaded"] = False
                    review_error_messages.append("Please choose a match_memory.json file to import.")

                if not upload_id:
                    upload_id = session.get("last_upload_id", "")
                cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})
                for vendor_name, _, _ in vendor_keys:
                    file_text = cached_vendor_files.get(vendor_name, "")
                    if not file_text:
                        continue
                    rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, None)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)
            elif action == "submit_unit_corrections":
                failed_step = "review save"
                saved_corrections = 0
                try:
                    total_items = int(request.form.get("total_unit_review_items", "0") or "0")
                    unit_corrections = dict(match_memory.get("unit_corrections", {}))
                    for i in range(total_items):
                        review_key = request.form.get(f"unit_review_key_{i}", "")
                        qty_text = request.form.get(f"unit_quantity_{i}", "").strip()
                        unit_type = request.form.get(f"unit_type_{i}", "").strip().lower()
                        preferred_comparison_unit = request.form.get(f"preferred_unit_{i}", "").strip().lower()
                        note = request.form.get(f"unit_note_{i}", "").strip()
                        if review_key and qty_text and unit_type:
                            try:
                                qty_value = float(qty_text)
                                if qty_value > 0:
                                    unit_corrections[review_key] = {
                                        "total_unit_quantity": qty_value,
                                        "unit_type": unit_type,
                                        "preferred_comparison_unit": preferred_comparison_unit,
                                        "note": note,
                                    }
                                    saved_corrections += 1
                            except ValueError:
                                continue
                    match_memory["unit_corrections"] = unit_corrections
                    save_match_memory(match_memory)
                    debug_counters["unit_corrections_saved"] = saved_corrections
                    SESSION_REVIEW_MEMORY[session_id] = {
                        "confirmed": dict(match_memory.get("confirmed", {})),
                        "rejected": set(match_memory.get("rejected", set())),
                        "unit_corrections": dict(match_memory.get("unit_corrections", {})),
                    }
                    session["match_memory_fallback"] = {
                        "confirmed": match_memory.get("confirmed", {}),
                        "rejected": sorted(list(match_memory.get("rejected", set()))),
                        "unit_corrections": match_memory.get("unit_corrections", {}),
                    }
                    review_success_messages.append(f"Unit corrections saved: {saved_corrections}.")
                except Exception as exc:
                    review_error_messages.append(f"Submit unit corrections failed: {exc}")

                if not upload_id:
                    upload_id = session.get("last_upload_id", "")
                cached_vendor_files = UPLOAD_CACHE.get(upload_id, {})
                for vendor_name, _, _ in vendor_keys:
                    file_text = cached_vendor_files.get(vendor_name, "")
                    if not file_text:
                        continue
                    rows, file_errors, debug_info = parse_vendor_text(vendor_name, file_text, None)
                    all_vendor_rows.extend(rows)
                    debug_counters["rows_parsed_per_vendor"][vendor_name] += len(rows)
                    errors.extend(file_errors)
                    debug_details.append(debug_info)

            if not all_vendor_rows and not errors and action == "upload":
                errors.append("Please upload at least one CSV file.")

            if all_vendor_rows and not show_mapping_form:
                failed_step = "review candidate generation"
                try:
                    comparison_rows, match_debug_rows, possible_matches, review_stats, match_review_buckets, match_matrix_stats, unit_review_items = build_comparison_rows(
                        all_vendor_rows, match_memory
                    )
                except Exception as exc:
                    # Keep rendering table if review generation fails.
                    errors.append("Warning: Review candidate generation failed; showing comparison table fallback.")
                    review_error_messages.append(f"Failure at step 'review candidate generation': {type(exc).__name__} - {exc}")
                    print(f"Failure at step 'review candidate generation': {type(exc).__name__}: {exc}")
                    comparison_rows = build_basic_comparison_rows(all_vendor_rows)
                    possible_matches = []
                    match_debug_rows = []
                    match_review_buckets = {}
                    match_matrix_stats = {}
                    unit_review_items = []

                debug_counters["rows_grouped"] = len(comparison_rows)
                debug_counters["review_candidates_generated"] = len(possible_matches)
                debug_counters["forced_matches_applied"] = int(review_stats.get("forced_group_keys_created", 0))
                debug_counters["review_items_remaining"] = int(match_matrix_stats.get("needs_review", 0)) + int(
                    match_matrix_stats.get("no_match_found", 0)
                )
                review_stats["debug_counters"] = debug_counters
            elif show_mapping_form and not errors:
                errors.append("Please choose manual column mappings, then click Apply Column Mapping.")

        except Exception as exc:
            fatal_error = {
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "failed_step": failed_step,
            }
            review_error_messages.append(f"Failure at step '{failed_step}': {type(exc).__name__} - {exc}")
            print(f"Failure at step '{failed_step}': {type(exc).__name__}: {exc}")

    return render_template(
        "index.html",
        rows=comparison_rows,
        errors=errors,
        debug_details=debug_details,
        match_debug_rows=match_debug_rows,
        possible_matches=possible_matches,
        review_stats=review_stats,
        upload_debug=upload_debug,
        show_mapping_form=show_mapping_form,
        mapping_options=mapping_options,
        upload_id=upload_id,
        review_success_messages=review_success_messages,
        review_error_messages=review_error_messages,
        fatal_error=fatal_error,
        debug_counters=debug_counters,
        match_review_buckets=match_review_buckets,
        match_matrix_stats=match_matrix_stats,
        review_batch_debug=review_batch_debug,
        export_warning=str(session.get("export_warning", "")),
        unit_review_items=unit_review_items,
    )


@app.route("/export-match-memory", methods=["GET"])
def export_match_memory():
    memory = get_effective_match_memory()
    confirmed_count = len(memory.get("confirmed", {}))
    rejected_count = len(memory.get("rejected", set()))
    session["last_export_counts"] = {"confirmed": confirmed_count, "rejected": rejected_count}
    session["export_warning"] = "No confirmed matches are currently saved." if confirmed_count == 0 else ""
    payload = {
        "confirmed": memory.get("confirmed", {}),
        "rejected": sorted(list(memory.get("rejected", set()))),
        "unit_corrections": memory.get("unit_corrections", {}),
    }
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={MATCH_MEMORY_FILE}"},
    )


@app.errorhandler(500)
def handle_internal_server_error(error):
    # Global fallback so preview shows readable error details.
    return render_template(
        "index.html",
        rows=[],
        errors=[],
        debug_details=[],
        match_debug_rows=[],
        possible_matches=[],
        upload_debug={"request_method": request.method, "files_keys": [], "received": {"Sysco": False, "US Foods": False, "PFG": False}},
        show_mapping_form=False,
        mapping_options={},
        upload_id="",
        review_success_messages=[],
        review_error_messages=[],
        fatal_error={
            "exception_type": type(error).__name__,
            "exception_message": str(error),
            "failed_step": "global_500_handler",
        },
        debug_counters={},
        match_review_buckets={},
        match_matrix_stats={},
        review_batch_debug={},
        export_warning="",
        unit_review_items=[],
    ), 500


if __name__ == "__main__":
    app.run(debug=True)
