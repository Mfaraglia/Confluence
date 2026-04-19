# Restaurant Vendor Price Comparison (Prototype)

This is a beginner-friendly Flask web app.
You upload CSV files from Sysco, US Foods, and PFG, and the app builds one comparison table.

## What changed in this version
- The app still has 3 upload areas (Sysco, US Foods, PFG).
- After upload, it now **combines data into one table**.
- Matching is now more flexible: before grouping, descriptions are cleaned by lowercasing, trimming spaces, collapsing repeated spaces, and removing punctuation like commas, periods, dashes, slashes, and parentheses.
- It matches products using a **cleaned product description** (lowercase, trimmed spaces, multiple spaces collapsed, and common punctuation removed) so near-identical descriptions still group together.
- It now includes a simple **CSV Parse Debug** section after upload, showing for each vendor:
  - detected headers
  - first 3 parsed rows
  - parser path used (`normal` or `fallback used`)
  - detected delimiter
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
3. Parses with Python's `csv` module.
4. If a row is parsed as one field but still contains a full comma-separated line, it safely splits into:
   - description
   - item number
   - pack size
   - price

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
