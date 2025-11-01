# app.py
import os
import threading
import time
import uuid
from flask import Flask, request, render_template, jsonify # type: ignore
import pandas as pd # type: ignore
import requests # type: ignore
from dotenv import load_dotenv # type: ignore

# Load environment variables
load_dotenv()

TERMII_API_KEY = os.getenv("TERMII_API_KEY")
TERMII_SENDER_ID = os.getenv("TERMII_SENDER_ID", "InfoText")
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+234")  # Default: Nigeria

app = Flask(__name__, static_folder="static", template_folder="templates")
jobs = {}  # in-memory store for uploads and sending jobs

# ---------------- Utilities ----------------
def normalize_phone(raw, country_code=DEFAULT_COUNTRY_CODE):
    """Normalize phone numbers into +countrycodeXXXXXXXXXX format."""
    if pd.isna(raw):
        return None
    s = str(raw).strip().replace(" ", "")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None

    # Already has country code?
    if digits.startswith(country_code.replace("+", "")):
        return f"+{digits}"

    # Nigerian local style -> convert 0XXXXXXXXXX to +234XXXXXXXXXX
    if digits.startswith("0"):
        return f"{country_code}{digits[1:]}"

    # If it starts with something else (like 8XXXXXXXXX), assume missing 0
    if len(digits) == 10:
        return f"{country_code}{digits}"

    # If still not sure, just return +digits
    return f"+{digits}"

def extract_preview_from_df(df):
    """Extract contacts (name + phone) from dataframe with flexible column mapping."""
    cols = {c.lower().strip(): c for c in df.columns}

    # Identify columns
    name_col = next((cols[c] for c in cols if any(k in c for k in ["name", "fullname", "clientname"])), None)
    phone_col = next((cols[c] for c in cols if any(k in c for k in ["phone", "mobile", "number", "telephone"])), None)
    country_col = next((cols[c] for c in cols if "country" in c), None)

    rows = []
    if not phone_col:
        return rows

    for _, row in df.iterrows():
        name = str(row.get(name_col, "")).strip() if name_col else ""
        phone_raw = row.get(phone_col, "")
        country = str(row.get(country_col, "")).lower() if country_col else ""

        # Auto-detect code if country column exists
        code = DEFAULT_COUNTRY_CODE
        if "gh" in country:
            code = "+233"
        elif "ng" in country or "nigeria" in country:
            code = "+234"

        phone = normalize_phone(phone_raw, code)
        if phone:
            rows.append({"fullname": name, "phone": phone})

    return rows

# ---------------- Termii API ----------------
def termii_send_sms(phone, message, sender_id=TERMII_SENDER_ID):
    """Send an SMS via Termii v3 API."""
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
        r = requests.post(url, json=payload, timeout=10)
        res = r.json()
        print(f"📨 Sent to {phone} | Response: {res}")
        return res
    except Exception as e:
        print(f"⚠️ Error sending to {phone}: {e}")
        return {"error": str(e)}

# ---------------- Background worker ----------------
def send_worker(send_job_id, contacts, message, personalize=False, delay=0.25):
    job = jobs[send_job_id]
    job.update({"status": "running", "total": len(contacts), "sent": 0, "failed": 0, "errors": []})

    for contact in contacts:
        msg = message
        if personalize and "{name}" in msg:
            first_name = contact.get("fullname", "").split()[0] or ""
            msg = msg.replace("{name}", first_name)

        res = termii_send_sms(contact["phone"], msg)
        if isinstance(res, dict) and res.get("message", "").lower().startswith("success"):
            job["sent"] += 1
        else:
            job["failed"] += 1
            job["errors"].append({"contact": contact, "response": res})

        job["last_update"] = time.time()
        time.sleep(delay)

    job["status"] = "completed"
    job["completed_at"] = time.time()

# ---------------- Flask Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        if f.filename.lower().endswith(".csv"):
            df = pd.read_csv(f)
        else:
            df = pd.read_excel(f)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    preview = extract_preview_from_df(df)
    seen = set()
    uniq = [r for r in preview if not (r["phone"] in seen or seen.add(r["phone"]))]

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "preview", "preview": uniq, "created": time.time()}

    return jsonify({"job_id": job_id, "preview_count": len(uniq), "preview": uniq[:100]})

@app.route("/preview/<job_id>")
def preview(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))
    data = job.get("preview", [])[offset: offset + limit]
    return jsonify({"preview": data})

@app.route("/send", methods=["POST"])
def send():
    data = request.json or {}
    job_id = data.get("job_id")
    message = data.get("message", "")
    personalize = bool(data.get("personalize", False))

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Invalid job_id"}), 400
    if not message:
        return jsonify({"error": "Message empty"}), 400

    contacts = jobs[job_id].get("preview", [])
    send_job_id = str(uuid.uuid4())
    jobs[send_job_id] = {"status": "queued", "created": time.time()}

    t = threading.Thread(target=send_worker, args=(send_job_id, contacts, message, personalize))
    t.daemon = True
    t.start()

    return jsonify({"send_job_id": send_job_id})

@app.route("/progress/<send_job_id>")
def progress(send_job_id):
    j = jobs.get(send_job_id)
    if not j:
        return jsonify({"error": "Not found"}), 404
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
