# Restaurant Vendor Price Comparison (Prototype)

This is a **very simple beginner-friendly web app** that compares fake food-service vendor prices.

It has:
- 1 backend file (Python + Flask)
- 1 frontend page (HTML + CSS + JavaScript)
- 1 table with 10 fake product rows
- Automatic highlighting of the cheapest vendor price in each row

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
- It starts a small local web server using Flask.
- It stores fake product price data.
- It sends that data to the web page.

### `templates/index.html`
- This is the one web page you see in the browser.
- It shows the page title and subtitle.
- It renders the comparison table.
- It includes:
  - simple CSS styling (for clean/professional look)
  - small JavaScript logic to find and highlight the cheapest price in each row.

### `requirements.txt`
- This lists Python package dependencies.
- Here we only need **Flask**.

### `README.md`
- This file (the one you are reading now).
- It explains how to run everything step by step.

---

## Step-by-step: run on your computer

## 1) Install Python (if you do not already have it)
- Download Python 3.10+ from: https://www.python.org/downloads/
- During installation, check the box that says **"Add Python to PATH"** (on Windows).

## 2) Open a terminal in this project folder
Use one of these:
- **Windows:** PowerShell or Command Prompt
- **Mac:** Terminal
- **Linux:** Terminal

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

## 6) Open your browser
Go to:

```text
http://127.0.0.1:5000
```

You should now see the **Restaurant Vendor Price Comparison** page.

---

## How to stop the app
- In the terminal, press `Ctrl + C`.

## How to edit fake data
- Open `app.py`.
- Find the `sample_products()` function.
- Change product names or prices.
- Save the file and refresh the browser.

---

## Notes
- This is only a prototype (fake data).
- No login, no database, no file upload, and no automation are included yet (as requested).
