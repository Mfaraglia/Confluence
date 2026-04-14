# Restaurant Vendor Price Comparison (Prototype)

This is a beginner-friendly Flask web app for uploading and viewing vendor CSV data.

## What this version does
- Shows one page in your browser.
- Lets you upload up to 3 CSV files:
  - Sysco
  - US Foods
  - PFG
- Reads each uploaded CSV and displays the rows in one simple table.
- Shows a friendly error message if a CSV is missing a **description** column or a **price** column.
- Does **not** match products across vendors yet (it just lists rows as uploaded).

---

## Project structure (what each file does)

```text
Confluence/
├── app.py
├── requirements.txt
├── README.md
└── templates/
    └── index.html
```

### `app.py`
- This is the backend server.
- It receives file uploads from the form.
- It reads CSV files safely.
- It checks for required columns (description + price).
- It sends the uploaded rows and any error messages to the page.

### `templates/index.html`
- This is the single web page.
- It shows:
  - title + subtitle
  - 3 upload areas (Sysco, US Foods, PFG)
  - upload button
  - error messages
  - uploaded data table
- It also contains simple CSS styling.

### `requirements.txt`
- Lists required Python packages.
- Only Flask is required.

### `README.md`
- Explains how to run and use the app.

---

## CSV format (simple guidance)
Your CSV should have a header row with column names.

The app looks for names like:
- Description column: `description`, `product description`, `item description`, `product`, `name`
- Price column: `price`, `unit price`, `cost`, `net price`
- Optional columns: item number, pack size

### Example CSV
```csv
description,item number,pack size,price
Chicken Breast,1001,40 lb,128.50
French Fries,2002,6/5 lb,40.95
```

---

## Step-by-step: run on your computer

## 1) Install Python
- Install Python 3.10 or newer from: https://www.python.org/downloads/
- On Windows, check **"Add Python to PATH"** during install.

## 2) Open a terminal in this project folder
- Windows: PowerShell or Command Prompt
- Mac/Linux: Terminal

## 3) Create and activate a virtual environment (recommended)

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

## 6) Open in browser
Go to:

```text
http://127.0.0.1:5000
```

---

## How to use the app
1. Click **Upload Sysco CSV**, **Upload US Foods CSV**, and/or **Upload PFG CSV**.
2. Choose CSV files from your computer.
3. Click **Upload and Show Data**.
4. Review rows in the table.
5. If a required column is missing, read the friendly error message and fix your CSV.

---

## What is intentionally NOT included yet
- Login
- Chatbot
- Automation
- Database
- Vendor website/API connections

This keeps the prototype simple and easy to understand.
