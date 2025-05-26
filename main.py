import os
import uuid
import base64
import json
import requests
import logging
from flask import Flask, request, jsonify, make_response
from google.cloud import firestore, storage

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Use BUCKET_NAME from env as before
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"))
db = firestore.Client()
COL = "genai_history"  # Firestore collection for all history

# GenAI service URL (set via environment variable)
GENAI_SERVICE_URL = os.getenv("GENAI_SERVICE_URL", "https://your-genai-service-url")

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

# ——— GenAI Integration Endpoints ———

@app.route("/generate", methods=["POST"])
def generate_content():
    """Generate content (text or images) via GenAI service and optionally save to records"""
    try:
        payload = request.get_json(force=True)
        prompt = payload.get("prompt")
        mode = payload.get("mode", "text")  # Default to text mode
        save_to_history = payload.get("save_to_history", True)
        
        if not prompt:
            return corsify(jsonify({"error": "prompt is required"})), 400
        
        # Prepare request for GenAI service
        genai_payload = {
            "prompt": prompt,
            "mode": mode
        }
        
        # Add image-specific parameters if mode is image
        if mode == "image":
            genai_payload.update({
                "width": payload.get("width", 512),
                "height": payload.get("height", 512),
                "style": payload.get("style", "vivid"),
                "count": payload.get("count", 1)
            })
        
        logger.info(f"Calling GenAI service with mode: {mode}, prompt: {prompt[:50]}...")
        
        # Call GenAI service
        response = requests.post(
            GENAI_SERVICE_URL,
            json=genai_payload,
            headers={"Content-Type": "application/json"},
            timeout=60  # 60 second timeout for image generation
        )
        
        if response.status_code != 200:
            logger.error(f"GenAI service error: {response.status_code} - {response.text}")
            return corsify(jsonify({
                "error": f"GenAI service error: {response.status_code}",
                "details": response.text
            })), response.status_code
        
        genai_result = response.json()
        logger.info(f"GenAI service responded successfully")
        
        # Save to records if requested
        record_id = None
        if save_to_history:
            record_id = str(uuid.uuid4())
            record = {
                "id": record_id,
                "prompt": prompt,
                "result": genai_result,
                "mode": mode,
                "timestamp": firestore.SERVER_TIMESTAMP
            }
            db.collection(COL).document(record_id).set(record)
            logger.info(f"Saved record with ID: {record_id}")
        
        # Return response with record ID if saved
        response_data = {
            "result": genai_result,
            "mode": mode,
            "prompt": prompt
        }
        
        if record_id:
            response_data["record_id"] = record_id
        
        return corsify(jsonify(response_data)), 200
        
    except requests.RequestException as e:
        logger.error(f"Failed to call GenAI service: {e}")
        return corsify(jsonify({
            "error": "Failed to communicate with GenAI service",
            "details": str(e)
        })), 503
    except Exception as e:
        logger.error(f"Unexpected error in generate_content: {e}")
        return corsify(jsonify({
            "error": "Internal server error",
            "details": str(e)
        })), 500

@app.route("/generate/text", methods=["POST"])
def generate_text():
    """Convenience endpoint for text generation"""
    payload = request.get_json(force=True)
    payload["mode"] = "text"
    return generate_content()

@app.route("/generate/image", methods=["POST"])
def generate_image():
    """Convenience endpoint for image generation"""
    payload = request.get_json(force=True)
    payload["mode"] = "image"
    return generate_content()

@app.route("/status", methods=["GET"])
def status():
    """Health check endpoint"""
    try:
        # Test GenAI service connectivity
        genai_status = "unknown"
        try:
            response = requests.get(f"{GENAI_SERVICE_URL}/status", timeout=5)
            genai_status = "connected" if response.status_code == 200 else "error"
        except:
            genai_status = "unreachable"
        
        # Test Firestore connectivity
        firestore_status = "unknown"
        try:
            db.collection(COL).limit(1).get()
            firestore_status = "connected"
        except:
            firestore_status = "error"
        
        return corsify(jsonify({
            "status": "ok",
            "services": {
                "genai": genai_status,
                "firestore": firestore_status,
                "genai_url": GENAI_SERVICE_URL
            }
        })), 200
    except Exception as e:
        return corsify(jsonify({
            "status": "error",
            "error": str(e)
        })), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
