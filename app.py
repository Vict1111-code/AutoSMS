# app.py
import os
import threading
import time
import uuid
from flask import Flask, request, render_template, jsonify
import pandas as pd
import requests
from dotenv import load_dotenv

# load .env
load_dotenv()

TERMII_API_KEY = os.getenv("TERMII_API_KEY")  # set in .env
TERMII_SENDER_ID = os.getenv("TERMII_SENDER_ID", "InfoText")

app = Flask(__name__, static_folder="static", template_folder="templates")

# in-memory job store (preview and send jobs)
jobs = {}

# ------------- Utilities -------------
def normalize_phone(raw):
    """Return normalized phone like 0XXXXXXXXXX or None if invalid."""
    if pd.isna(raw):
        return None
    s = str(raw).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("0"):
        return digits
    if digits.startswith("234") and len(digits) >= 10:
        return "0" + digits[3:]
    return "0" + digits

def extract_preview_from_df(df):
    """Return list of dicts with keys fullname and phone."""
    # normalize column names to make detection robust
    cols = {c.lower().strip(): c for c in df.columns}
    # identify candidate name and phone columns
    name_col = None
    phone_col = None
    for key, orig in cols.items():
        if key in ("fullname", "full name", "name", "clientname"):
            name_col = orig
        if any(k in key for k in ("phone", "mobile", "phonenumber", "telephone")):
            phone_col = orig
    # fallback heuristics
    if not name_col:
        # use first text-like column
        for c in df.columns:
            if df[c].dtype == object:
                name_col = c
                break
    if not phone_col:
        # choose any column with digits
        for c in df.columns:
            sample = df[c].astype(str).fillna("").iloc[0]
            if any(ch.isdigit() for ch in sample):
                phone_col = c
                break

    rows = []
    if phone_col is None:
        return rows  # couldn't detect phone column

    for _, row in df.iterrows():
        name = row.get(name_col, "") if name_col else ""
        phone_raw = row.get(phone_col, "")
        phone = normalize_phone(phone_raw)
        if phone:
            rows.append({"fullname": str(name) if name is not None else "", "phone": phone})
    return rows

# ------------- Termii send -------------
def termii_send_sms(phone, message, sender_id=TERMII_SENDER_ID):
    """Send SMS via Termii API. Returns parsed JSON or error dict."""
    url = "https://v3.api.termii.com/api/sms/send"
    payload = {
        "to": phone,
        "from": sender_id,
        "sms": message,
        "type": "plain",
        "channel": "generic",
        "api_key": TERMII_API_KEY,
    }
    try:
        r = requests.post(url, json=payload, timeout=12)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ------------- Background sender -------------
def send_worker(send_job_id, contacts, message, personalize=False, delay=0.2):
    job = jobs[send_job_id]
    job.update({"status": "running", "total": len(contacts), "sent": 0, "failed": 0})
    for contact in contacts:
        msg = message
        if personalize:
            first = contact.get("fullname", "").split()[0] if contact.get("fullname") else ""
            msg = msg.replace("{name}", first)
        res = termii_send_sms(contact["phone"], msg)
        if isinstance(res, dict) and res.get("code") == "ok":
            job["sent"] += 1
        else:
            job["failed"] += 1
            # store latest error for inspection
            job.setdefault("errors", []).append({"contact": contact, "response": res})
        job["last_update"] = time.time()
        time.sleep(delay)
    job["status"] = "completed"
    job["completed_at"] = time.time()

# ------------- Routes -------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        # read file; allow xlsx/xls/csv
        if f.filename.lower().endswith(".csv"):
            df = pd.read_csv(f)
        else:
            df = pd.read_excel(f)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    preview_list = extract_preview_from_df(df)
    # deduplicate by phone preserving order
    seen = set()
    uniq = []
    for r in preview_list:
        if r["phone"] not in seen:
            seen.add(r["phone"])
            uniq.append(r)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "preview", "preview": uniq, "created": time.time()}

    # return preview_count and first page
    return jsonify({
        "job_id": job_id,
        "preview_count": len(uniq),
        "preview": uniq[:100]
    })

@app.route("/preview/<job_id>", methods=["GET"])
def preview(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({"error": "job not found"}), 404
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 100))
    except:
        offset, limit = 0, 100
    data = j.get("preview", [])[offset: offset + limit]
    return jsonify({"preview": data})

@app.route("/send", methods=["POST"])
def send():
    data = request.json or {}
    job_id = data.get("job_id")
    message = data.get("message", "")
    personalize = bool(data.get("personalize", False))
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Invalid or missing job_id"}), 400
    if not message:
        return jsonify({"error": "Message empty"}), 400

    contacts = jobs[job_id].get("preview", [])
    send_job_id = str(uuid.uuid4())
    jobs[send_job_id] = {"status": "queued", "created": time.time()}

    t = threading.Thread(target=send_worker, args=(send_job_id, contacts, message, personalize))
    t.daemon = True
    t.start()

    return jsonify({"send_job_id": send_job_id})

@app.route("/progress/<send_job_id>", methods=["GET"])
def progress(send_job_id):
    j = jobs.get(send_job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    total = j.get("total", 0)
    sent = j.get("sent", 0)
    failed = j.get("failed", 0)
    percent = int((sent + failed) / total * 100) if total else 0
    return jsonify({
        "status": j.get("status"),
        "total": total,
        "sent": sent,
        "failed": failed,
        "percent": percent
    })

if __name__ == "__main__":
    app.run(debug=True, port=5000)
