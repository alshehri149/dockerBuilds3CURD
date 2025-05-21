import os
import uuid
import base64
import json
from flask import Flask, request, jsonify
from google.cloud import firestore, storage

# ----- Configuration from environment -----
PORT           = int(os.environ.get("PORT", 8080))
BUCKET_NAME    = os.environ.get("BUCKET_NAME")
if not BUCKET_NAME:
    raise RuntimeError("Missing BUCKET_NAME env-var")
COLLECTION     = os.environ.get("HISTORY_COLLECTION", "genai_history")

# ----- Initialize clients -----
db     = firestore.Client()
bucket = storage.Client().bucket(BUCKET_NAME)
app    = Flask(__name__)

# ----- CRUD endpoints on /records -----
@app.route("/records", methods=["POST"])
def create_record():
    payload = request.get_json(force=True)
    prompt  = payload.get("prompt")
    result  = payload.get("result")
    if not prompt or result is None:
        return jsonify({"error": "prompt and result are required"}), 400

    doc_id = str(uuid.uuid4())
    record = {"id": doc_id, "prompt": prompt, "result": result}
    db.collection(COLLECTION).document(doc_id).set(record)
    return jsonify(record), 201

@app.route("/records", methods=["GET"])
def list_records():
    docs = db.collection(COLLECTION).stream()
    return jsonify([d.to_dict() for d in docs]), 200

@app.route("/records/<record_id>", methods=["GET"])
def get_record(record_id):
    doc = db.collection(COLLECTION).document(record_id).get()
    if not doc.exists:
        return jsonify({"error": "not found"}), 404
    return jsonify(doc.to_dict()), 200

@app.route("/records/<record_id>", methods=["DELETE"])
def delete_record(record_id):
    doc_ref = db.collection(COLLECTION).document(record_id)
    if not doc_ref.get().exists:
        return jsonify({"error": "not found"}), 404
    doc_ref.delete()
    return ("", 204)

# ----- Pub/Sub push endpoint on /history -----
@app.route("/history", methods=["POST"])
def history_push():
    envelope = request.get_json(force=True)
    if not envelope or "message" not in envelope:
        return jsonify({"error": "Invalid Pub/Sub message format"}), 400

    message = envelope["message"]
    data_b64 = message.get("data")
    if not data_b64:
        return jsonify({"error": "No data in Pub/Sub message"}), 400

    # Decode the JSON payload
    try:
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"Failed to decode data: {e}"}), 400

    # If there's a media field with base64 content, store it in Cloud Storage
    if "media_b64" in payload:
        media_bytes = base64.b64decode(payload["media_b64"])
        filename    = f"{uuid.uuid4()}"
        blob        = bucket.blob(filename)
        blob.upload_from_string(media_bytes)
        # Replace the base64 in the record with a public URL
        payload["media_url"] = blob.public_url
        del payload["media_b64"]

    # Construct and store the history record
    doc_id = str(uuid.uuid4())
    record = {
        "id":        doc_id,
        **payload,
        "timestamp": firestore.SERVER_TIMESTAMP
    }
    db.collection(COLLECTION).document(doc_id).set(record)
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
