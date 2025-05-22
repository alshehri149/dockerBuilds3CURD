import os
import uuid
import base64
import json
from flask import Flask, request, jsonify, make_response
from google.cloud import firestore, storage

app = Flask(__name__)

# Use BUCKET_NAME from env as before
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"))
db = firestore.Client()
COL = "genai_history"  # Firestore collection for all history

# --- CORS helper ---
def corsify(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# --- Preflight handlers ---
@app.route("/records", methods=["OPTIONS"])
@app.route("/records/<record_id>", methods=["OPTIONS"])
def handle_options(record_id=None):
    return corsify(make_response("", 204))

# ——— CRUD endpoints ———

@app.route("/records", methods=["POST"])
def create_record():
    payload = request.get_json(force=True)
    prompt = payload.get("prompt")
    result = payload.get("result")
    if not prompt or result is None:
        return corsify(jsonify({"error": "prompt and result are required"})), 400

    doc_id = str(uuid.uuid4())
    rec = {"id": doc_id, "prompt": prompt, "result": result}
    db.collection(COL).document(doc_id).set(rec)
    return corsify(jsonify(rec)), 201

@app.route("/records", methods=["GET"])
def list_records():
    docs = db.collection(COL).stream()
    records = [d.to_dict() for d in docs]
    return corsify(jsonify(records)), 200

@app.route("/records/<record_id>", methods=["GET"])
def get_record(record_id):
    doc = db.collection(COL).document(record_id).get()
    if not doc.exists:
        return corsify(jsonify({"error": "not found"})), 404
    return corsify(jsonify(doc.to_dict())), 200

@app.route("/records/<record_id>", methods=["PUT"])
def update_record(record_id):
    payload = request.get_json(force=True)
    updates = {}
    if "prompt" in payload:
        updates["prompt"] = payload["prompt"]
    if "result" in payload:
        updates["result"] = payload["result"]
    if not updates:
        return corsify(jsonify({"error": "nothing to update"})), 400

    doc_ref = db.collection(COL).document(record_id)
    if not doc_ref.get().exists:
        return corsify(jsonify({"error": "not found"})), 404
    doc_ref.update(updates)
    return corsify(jsonify(doc_ref.get().to_dict())), 200

@app.route("/records/<record_id>", methods=["DELETE"])
def delete_record(record_id):
    doc_ref = db.collection(COL).document(record_id)
    if not doc_ref.get().exists:
        return corsify(jsonify({"error": "not found"})), 404
    doc_ref.delete()
    return corsify(make_response("", 204))

# ——— Pub/Sub push ingestion ———

@app.route("/history", methods=["POST"])
def history_push():
    envelope = request.get_json(force=True)
    if not envelope or "message" not in envelope:
        return jsonify({"error": "Invalid Pub/Sub message format"}), 400

    msg = envelope["message"]
    data_b64 = msg.get("data")
    if not data_b64:
        return jsonify({"error": "No data in Pub/Sub message"}), 400

    try:
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"Failed to decode data: {e}"}), 400

    # Build the history record
    doc_id = str(uuid.uuid4())
    record = {
        "id":        doc_id,
        "service":   payload.get("service"),
        "request":   payload.get("request"),
        "response":  payload.get("response"),
        "timestamp": firestore.SERVER_TIMESTAMP
    }

    # Save to Firestore
    db.collection(COL).document(doc_id).set(record)
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
