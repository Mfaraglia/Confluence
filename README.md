# Restaurant Vendor Price Comparison (Prototype)

This is a beginner-friendly Flask web app.
You upload CSV files from Sysco, US Foods, and PFG, and the app builds one comparison table.

## What changed in this version
- The app still has 3 upload areas (Sysco, US Foods, PFG).
- After upload, it now **combines data into one table**.
- Matching is now more flexible: before grouping, descriptions are cleaned by lowercasing, trimming spaces, collapsing repeated spaces, and removing punctuation like commas, periods, dashes, slashes, and parentheses.
- It matches products using a **cleaned product description** (lowercase, trimmed spaces, multiple spaces collapsed, and common punctuation removed) so near-identical descriptions still group together.
- Matching is now smarter with simple rule-based normalization for common foodservice abbreviations and wording:
  - `chk` / `chkn` → `chicken`
  - `brst` → `breast`
  - `dbl` → `double`
  - `b/s` or `bnls` → `boneless` (and `b/s` also expands to `skinless`)
  - `sknls` → `skinless`
  - `bnlss` → `boneless`
  - `grnd` → `ground`
  - `bf` → `beef`
  - `mozz` → `mozzarella`
  - `shrd` → `shredded`
  - `frz` / `fz` → `frozen`
  - `hvy` → `heavy`
  - `tff` → `trans fat free`
  - `ntrsbst` → `nonthermostabilized`
  - `cont` → `container`
  - `cmpt` → `compartment`
  - `whi` → `white`
  - `hngd` → `hinged`
  - `lg` → `large`
  - `slvr src` → `silver source`
  - `applwd` → `applewood`
  - `ref` → `refrigerated`
  - `fc` → `fully cooked`
  - `slcd` → `sliced`
  - `lqd` → `liquid`
  - `blnd` → `blend`
  - `alt` / `alternative` → `alternative`
  - `bb` / `beer_battered` / `battered` → `battered`
  - `breader` / `tempura` / `batter mix` normalize together
  - `controlled_vacuum_packed` / `cvp` normalize together
  - `ff` or `fries french` → `french fries`
  - `#` → `lb`
  - plus a central alias dictionary for vendor shorthand (for example: `squid -> calamari`, `l/o -> laid out`, `bb -> beer battered`, `tndr -> tender`, `ched -> cheddar`, etc.)
  It also applies a small order fix like `mozzarella shredded cheese` → `mozzarella cheese shredded`.
- Matching now also uses **token-based similarity** (not just exact normalized text):
  - It splits normalized descriptions into tokens (words).
  - It removes weak tokens (for example: `raw`, `fresh`, `frozen`, `pack`, `source`, `west`, `creek`, `silver`, `mark`) so they do not control grouping.
  - It separates tokens into `core_tokens`, `attribute_tokens`, and `size_tokens`.
  - It groups mainly by product family first, then core tokens, then attribute tokens.
  - It uses size/pack tokens as weaker tie-breakers only.
  - It adds product-family aliases for common categories such as onion rings, tempura batter mix, ground beef, foam container, chicken breast boneless skinless, heavy cream, and more.
  - It computes a simple match confidence score; high-confidence matches auto-group, low-confidence matches stay separate.
  - Every row now always receives a `product_family`. If alias rules do not find one, a keyword fallback classifier infers one from core tokens.
  - After family assignment, the app always uses `final_group_key = product_family` for grouping.
  - Manual override groups (from `manual_overrides.py`) are checked first and win before alias rules and token matching.
  - It now separates tokens into:
    - **core_tokens** (main food words, primary grouping signal)
    - **size_tokens** (pack/size-like words, secondary signal)
  This helps group things like `FRIES FRENCH 6/5#`, `Frozen French Fries`, and `French Fries Frozen 6/5 lb` together.
- It now includes a simple **CSV Parse Debug** section after upload, showing for each vendor:
  - detected headers
  - first 3 parsed rows
  - parser path used (`normal` or `fallback used`)
  - detected delimiter
  - header row chosen
  - rows skipped before table
  - normalized description (used for matching)
- It now includes a **Matching Debug** section showing for each parsed row:
  - alias_expanded_description
  - override_group_hit
  - product_family
  - inferred_product_family (when fallback classifier is used)
  - core_tokens
  - attribute_tokens
  - size_tokens
  - match_confidence
  - final_group_key
- It now includes a **Possible Matches** review section:
  - high-confidence matches auto-group
  - medium-confidence matches are shown for human review
  - low-confidence matches stay separate
  You can click **Match** or **Keep Separate**.
- Review memory is saved locally in `match_memory.json`:
  - confirmed pairs are remembered and auto-grouped next time
  - rejected pairs are remembered and not suggested again
- It now supports **manual column mapping** when headers are not obvious:
  - Product Description
  - Item Number
  - Pack Size
  - Price
  The dropdowns show the actual headers from that vendor file.
- It includes an **Upload Debug Summary** at the top after submit, so you can confirm Flask actually received each file.
  It shows the request method, `request.files` keys, and per-vendor received status.
- File upload wiring was fixed and verified:
  - form uses `method="POST"`
  - form uses `enctype="multipart/form-data"`
  - all 3 file inputs and the submit button are inside the same form
  - backend reads `request.files` with matching names: `sysco_file`, `usfoods_file`, `pfg_file`
- It now safely handles messy rows where a full CSV line gets stuck in one column.
  In that case, it manually splits into:
  `description, item_number, pack_size, price`.
- It shows these columns:
  - Product Description
  - Sysco Price
  - US Foods Price
  - PFG Price
  - Cheapest Vendor
- If a product is missing from one vendor, that vendor price is left blank.
- It highlights the cheapest available vendor price in each row.
- If a CSV is missing description or price columns, it shows a friendly error message.

---

## Project structure (simple)

```text
Confluence/
├── app.py
├── requirements.txt
├── README.md
└── templates/
    └── index.html
```

### `app.py`
- Backend server.
- Handles file uploads.
- Reads CSV files.
- Validates required columns.
- Detects delimiter when possible (comma, semicolon, tab, or pipe).
- Includes a safe fallback split for rows that were parsed into one field.
- Combines rows using a cleaned description key, while still showing the original readable description.
- Calculates cheapest vendor.

### `templates/index.html`
- Single web page.
- Shows upload form and comparison table.
- Highlights cheapest price cell with basic styling.

### `requirements.txt`
- Python package list.
- Only Flask is needed.

### `README.md`
- Setup and usage instructions.

---

## CSV file guidance
Use normal CSV files with a header row.

The app looks for description column names like:
- `description`
- `product description`
- `item description`
- `product`
- `name`

The app looks for price column names like:
- `price`
- `unit price`
- `cost`
- `net price`

### Parser flow (simple)
For each uploaded file, the app:
1. Reads the file as text.
2. Tries to detect the delimiter.
3. Scans rows to find the most likely real table header row (useful when exports contain intro/report text first).
4. Parses with Python's `csv` module starting from that header row and ignores blank rows.
5. If a row is parsed as one field but still contains a full comma-separated line, it safely splits into:
   - description
   - item number
   - pack size
   - price

### Manual mapping flow
If the app cannot confidently find required columns, it shows a mapping form for that vendor.
1. Pick which CSV header should be used for Product Description, Item Number, Pack Size, and Price.
2. Click **Apply Column Mapping**.
3. The app rebuilds the comparison table using your selections.

### Example CSV
```csv
description,price
Chicken Breast,128.50
French Fries,40.95
```

---

## How to run (step by step)

## 1) Install Python
- Install Python 3.10+ from: https://www.python.org/downloads/
- On Windows, check **"Add Python to PATH"**.

## 2) Open terminal in this project folder
- Windows: PowerShell or Command Prompt
- Mac/Linux: Terminal

## 3) (Recommended) Create and activate virtual environment

### Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Mac/Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 4) Install dependencies
```bash
pip install -r requirements.txt
```

## 5) Start the app
```bash
python app.py
```

## 6) Open browser
Go to:

```text
http://127.0.0.1:5000
```

---

## How to use
1. Upload Sysco CSV, US Foods CSV, and/or PFG CSV.
2. Click **Upload and Compare Prices**.
3. Review the combined table.
4. Check the highlighted cheapest price and cheapest vendor column.

---

## What is not included yet (on purpose)
- Login
- Chatbot
- Automation
- Database
- Vendor website/API connections

This keeps the prototype simple and easy to understand.
