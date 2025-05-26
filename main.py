import os
import uuid
import base64
import json
import requests
from flask import Flask, request, jsonify, make_response
from google.cloud import firestore, storage

app = Flask(__name__)

# Use BUCKET_NAME from env as before
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"), 'coe558-genai-media')
db = firestore.Client()
COL = "genai_history"  # Firestore collection for all history

# GenAI service URL
GENAI_SERVICE_URL = "https://genai-service-90452453058.europe-west1.run.app"

# --- CORS helper ---
def corsify(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# --- Preflight handlers ---
@app.route("/records", methods=["OPTIONS"])
@app.route("/records/<record_id>", methods=["OPTIONS"])
@app.route("/generate", methods=["OPTIONS"])
def handle_options(record_id=None):
    return corsify(make_response("", 204))

# ——— NEW: GenAI Generation endpoint ———

@app.route("/generate", methods=["POST"])
def generate_content():
    """
    Generate content (text or images) using the external GenAI service.
    Supports both text and image generation modes.
    """
    try:
        payload = request.get_json(force=True)
        
        # Validate required fields
        if not payload.get("prompt"):
            return corsify(jsonify({"error": "prompt is required"})), 400
        
        # Default to text mode if not specified
        mode = payload.get("mode", "text")
        
        # Prepare request to GenAI service
        genai_payload = {
            "prompt": payload["prompt"],
            "mode": mode
        }
        
        # Add image-specific parameters if in image mode
        if mode == "image":
            genai_payload.update({
                "width": payload.get("width", 512),
                "height": payload.get("height", 512),
                "style": payload.get("style", "vivid"),
                "count": payload.get("count", 1)
            })
        
        # Call the external GenAI service
        try:
            response = requests.post(
                GENAI_SERVICE_URL,
                json=genai_payload,
                headers={"Content-Type": "application/json"},
                timeout=60  # 60 second timeout for image generation
            )
            response.raise_for_status()
            genai_result = response.json()
        except requests.exceptions.RequestException as e:
            return corsify(jsonify({
                "error": f"Failed to call GenAI service: {str(e)}"
            })), 502
        except json.JSONDecodeError as e:
            return corsify(jsonify({
                "error": f"Invalid response from GenAI service: {str(e)}"
            })), 502
        
        # Store the generation request and result in Firestore for history
        try:
            doc_id = str(uuid.uuid4())
            history_record = {
                "id": doc_id,
                "service": "genai",
                "mode": mode,
                "request": {
                    "prompt": payload["prompt"],
                    "parameters": {k: v for k, v in payload.items() if k != "prompt"}
                },
                "response": genai_result,
                "timestamp": firestore.SERVER_TIMESTAMP
            }
            db.collection(COL).document(doc_id).set(history_record)
        except Exception as e:
            # Log the error but don't fail the request
            print(f"Warning: Failed to save to Firestore: {e}")
        
        # Return the generation result
        return corsify(jsonify({
            "success": True,
            "mode": mode,
            "prompt": payload["prompt"],
            "result": genai_result,
            "generation_id": doc_id
        })), 200
        
    except Exception as e:
        return corsify(jsonify({
            "error": f"Internal server error: {str(e)}"
        })), 500

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
