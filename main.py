import os
import uuid
import base64
import json
from flask import Flask, request, jsonify
from google.cloud import firestore, storage

app = Flask(__name__)

# Keep using BUCKET_NAME from environment as before
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"))
db = firestore.Client()

# ——— Existing CRUD endpoints ———

@app.route("/records", methods=["POST"])
def create_record():
    payload = request.get_json(force=True)
    prompt = payload.get("prompt")
    result = payload.get("result")
    if not prompt or result is None:
        return jsonify({"error": "prompt and result are required"}), 400

    doc_id = str(uuid.uuid4())
    record = {"id": doc_id, "prompt": prompt, "result": result}
    db.collection("genai_history").document(doc_id).set(record)
    return jsonify(record), 201

@app.route("/records", methods=["GET"])
def list_records():
    docs = db.collection("genai_history").stream()
    return jsonify([d.to_dict() for d in docs]), 200

@app.route("/records/<record_id>", methods=["GET"])
def get_record(record_id):
    doc = db.collection("genai_history").document(record_id).get()
    if not doc.exists:
        return jsonify({"error": "not found"}), 404
    return jsonify(doc.to_dict()), 200

@app.route("/records/<record_id>", methods=["DELETE"])
def delete_record(record_id):
    doc_ref = db.collection("genai_history").document(record_id)
    if not doc_ref.get().exists:
        return jsonify({"error": "not found"}), 404
    doc_ref.delete()
    return ("", 204)

# ——— New Pub/Sub push endpoint ———

@app.route("/history", methods=["POST"])
def history_push():
    envelope = request.get_json(force=True)
    if not envelope or "message" not in envelope:
        return jsonify({"error": "Invalid Pub/Sub message format"}), 400

    message = envelope["message"]
    data_b64 = message.get("data")
    if not data_b64:
        return jsonify({"error": "No data in Pub/Sub message"}), 400

    try:
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"Failed to decode data: {e}"}), 400

    # Build and store history record
    doc_id = str(uuid.uuid4())
    record = {
        "id":        doc_id,
        "service":   payload.get("service"),
        "request":   payload.get("request"),
        "response":  payload.get("response"),
        "timestamp": firestore.SERVER_TIMESTAMP
    }
    db.collection("genai_history").document(doc_id).set(record)
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
