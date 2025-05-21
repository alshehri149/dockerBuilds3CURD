import os
import uuid
from flask import Flask, request, jsonify
from google.cloud import firestore, storage

app = Flask(__name__)
db = firestore.Client()
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"))

@app.route("/records", methods=["POST"])
def create_record():
    payload = request.get_json(force=True)
    prompt = payload.get("prompt")
    result = payload.get("result")
    if not prompt or not result:
        return jsonify({"error": "prompt and result are required"}), 400

    doc_id = str(uuid.uuid4())
    record = {"id": doc_id, "prompt": prompt, "result": result}
    db.collection("genai_history").document(doc_id).set(record)
    return jsonify(record), 201

@app.route("/records", methods=["GET"])
def list_records():
    docs = db.collection("genai_history").stream()
    return jsonify([doc.to_dict() for doc in docs]), 200

@app.route("/records/<record_id>", methods=["GET"])
def get_record(record_id):
    doc = db.collection("genai_history").document(record_id).get()
    if not doc.exists:
        return jsonify({"error": "not found"}), 404
    return jsonify(doc.to_dict()), 200

@app.route("/records/<record_id>", methods=["DELETE"])
def delete_record(record_id):
    db.collection("genai_history").document(record_id).delete()
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
