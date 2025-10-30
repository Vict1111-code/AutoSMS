# app.py
import os
import threading
import time
import uuid
from flask import Flask, request, render_template, jsonify
import pandas as pd
from dotenv import load_dotenv
import requests

load_dotenv()
TERMII_API_KEY = os.getenv("TERMII_API_KEY")
TERMII_SENDER_ID = os.getenv("TERMII_SENDER_ID", "InfoText")  # default

app = Flask(__name__, static_folder="static", template_folder="templates")

# In-memory jobs store: job_id -> {total, sent, failed, status, results:list}
jobs = {}

# ---------- Utilities ----------
def normalize_phone(raw):
    """Normalize phone numbers:
       - Remove spaces and non-digits
       - If not starting with '0', add '0'
       - Return None if too short
    """
    if pd.isna(raw):
        return None
    s = str(raw).strip()
    # keep digits only
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits == "":
        return None
    # If it already starts with 0, keep as is
    if digits.startswith("0"):
        return digits
    # If it starts with country code 234 (Nigeria), convert to local 0XXXXXXXXXX
    if digits.startswith("234") and len(digits) >= 10:
        return "0" + digits[len("234"):]
    # else just add leading zero (per your spec)
    return "0" + digits

def extract_preview_from_df(df):
    """Return list of dicts: {id, fullname, phone}"""
    rows = []
    for idx, row in df.iterrows():
        name = row.get("Fullname") or row.get("Name") or row.get("fullname") or ""
        phone_raw = row.get("phone") or row.get("Phone") or row.get("phone number") or row.get("PhoneNumber") or row.get("phone_number") or row.get("Phone_Number") or row.get("phonenumber") or ""
        phone = normalize_phone(phone_raw)
        if phone:  # only include if phone could be normalized
            rows.append({
                "id": str(idx),
                "fullname": str(name),
                "phone": phone
            })
    return rows

# ---------- Termii sending ----------
def termii_send_sms(phone_number, message, sender_id=TERMII_SENDER_ID):
    url = "https://api.ng.termii.com/api/sms/send"
    payload = {
        "to": phone_number,
        "from": sender_id,
        "sms": message,
        "type": "plain",
        "channel": "generic",
        "api_key": TERMII_API_KEY
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

# ---------- Background worker ----------
def send_worker(job_id, targets, message, personalize=False, per_send_delay=0.05):
    job = jobs[job_id]
    job["total"] = len(targets)
    job["sent"] = 0
    job["failed"] = 0
    job["status"] = "running"
    job["results"] = []
    for i, t in enumerate(targets, start=1):
        # personalize
        msg = message
        if personalize:
            # replace {name} placeholder with first name (if exists)
            name = t.get("fullname", "")
            first = name.split()[0] if name else ""
            msg = message.replace("{name}", first)
        res = termii_send_sms(t["phone"], msg)
        success = False
        # consider success if API returns code 'ok' (Termii typical)
        if isinstance(res, dict) and res.get("code") == "ok":
            success = True
            job["sent"] += 1
        else:
            job["failed"] += 1
        job["results"].append({"target": t, "response": res, "success": success})
        # update progress
        job["last_update"] = time.time()
        # optional delay to avoid rate limits
        time.sleep(per_send_delay)
    job["status"] = "completed"

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    # accepts file in 'file'
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    # read with pandas (auto detect)
    try:
        df = pd.read_excel(f)
    except Exception as e:
        return jsonify({"error": f"could not read excel: {e}"}), 400
    preview = extract_preview_from_df(df)
    # remove duplicates by phone
    seen = set()
    uniq = []
    for r in preview:
        if r["phone"] not in seen:
            seen.add(r["phone"])
            uniq.append(r)
    # store preview in a short-lived job id so frontend can fetch if needed
    temp_id = str(uuid.uuid4())
    jobs[temp_id] = {"status": "preview", "preview": uniq, "created": time.time()}
    return jsonify({"job_id": temp_id, "preview_count": len(uniq)})

@app.route("/preview/<job_id>", methods=["GET"])
def preview(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"preview": j.get("preview", [])})

@app.route("/send", methods=["POST"])
def send():
    data = request.json
    # expected: job_id (preview id) OR targets list, message, personalize (bool)
    message = data.get("message", "")
    personalize = bool(data.get("personalize", False))
    per_send_delay = float(data.get("per_send_delay", 0.05))  # optional
    # targets come either from provided list or from preview job id
    targets = data.get("targets")
    if not targets:
        job_id = data.get("job_id")
        if not job_id or job_id not in jobs:
            return jsonify({"error": "no targets provided and invalid job_id"}), 400
        targets = jobs[job_id].get("preview", [])
    if not message:
        return jsonify({"error": "message is empty"}), 400
    # new job for sending
    send_job_id = str(uuid.uuid4())
    jobs[send_job_id] = {"status": "queued", "total": 0, "sent": 0, "failed": 0, "results": []}
    # start background thread
    thread = threading.Thread(target=send_worker, args=(send_job_id, targets, message, personalize, per_send_delay))
    thread.start()
    return jsonify({"send_job_id": send_job_id})

@app.route("/progress/<send_job_id>", methods=["GET"])
def progress(send_job_id):
    j = jobs.get(send_job_id)
    if not j:
        return jsonify({"error": "job not found"}), 404
    total = j.get("total", 0)
    sent = j.get("sent", 0)
    failed = j.get("failed", 0)
    status = j.get("status", "unknown")
    percent = int((sent + failed) / total * 100) if total > 0 else 0
    return jsonify({
        "status": status,
        "total": total,
        "sent": sent,
        "failed": failed,
        "percent": percent
    })

if __name__ == "__main__":
    app.run(debug=True, port=5000)
