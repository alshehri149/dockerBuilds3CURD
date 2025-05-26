import os
import uuid
import base64
import json
import requests
import time
from flask import Flask, request, jsonify, make_response
from google.cloud import firestore, storage

app = Flask(__name__)

# Use BUCKET_NAME from env as before
bucket = storage.Client().bucket(os.getenv("BUCKET_NAME"))
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
        
        # Handle response based on mode
        if mode == "image":
            # Process image URLs to convert API references to actual URLs
            if genai_result and 'images' in genai_result:
                for image in genai_result['images']:
                    if image.get('url', '').startswith('api://'):
                        # Convert api://filename to actual API endpoint URL
                        filename = image['url'].replace('api://', '')
                        # Use the current request's host to build the proper URL
                        # This ensures it works in any deployment environment
                        image['url'] = f"{request.scheme}://{request.host}/images/{filename}"
                        print(f"Converted API reference to: {image['url']}")
        
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

# ——— NEW: Image serving endpoint ———

@app.route("/images/<path:filename>", methods=["GET"])
def serve_image(filename):
    """
    Serve images directly from Google Cloud Storage bucket.
    This allows the frontend to request images without needing to construct URLs.
    Supports multiple filename patterns for better compatibility.
    """
    try:
        print(f"Image request for: {filename}")
        
        # List of possible filename patterns to try
        filename_patterns = []
        
        # If filename already has generated/ prefix, use as-is
        if filename.startswith('generated/'):
            filename_patterns.append(filename)
            base_filename = filename.replace('generated/', '')
        else:
            base_filename = filename
            filename_patterns.append(f"generated/{filename}")
        
        # Extract potential hash from the filename
        hash_match = None
        if '_' in base_filename:
            # Pattern: image_hash_timestamp.png or hash_timestamp.png
            parts = base_filename.replace('.png', '').split('_')
            if len(parts) >= 2:
                # Second part should be hash (first 8 chars)
                potential_hash = parts[1] if parts[0] == 'image' else parts[0]
                hash_match = potential_hash[:8] if len(potential_hash) >= 8 else potential_hash
        elif '.' in base_filename:
            # Pattern: hash.png
            hash_match = base_filename.split('.')[0][:8]
        else:
            # Use entire filename as hash (truncated to 8 chars)
            hash_match = base_filename[:8]
        
        # Generate additional filename patterns if we have a hash
        if hash_match:
            print(f"Looking for files with hash: {hash_match}")
            
            # Generate patterns with different formats
            base_patterns = [
                f"generated/{hash_match}.png",
                f"generated/image_{hash_match}.png",
                f"generated/img_{hash_match}.png",
                f"{hash_match}.png",
                f"image_{hash_match}.png",
                f"img_{hash_match}.png"
            ]
            filename_patterns.extend(base_patterns)
            
            # Try with recent timestamps (search last 2 hours)
            current_timestamp = int(time.time())
            for i in range(7200):  # Check last 2 hours (7200 seconds)
                timestamp = current_timestamp - i
                timestamp_patterns = [
                    f"generated/image_{hash_match}_{timestamp}.png",
                    f"generated/{hash_match}_{timestamp}.png",
                    f"generated/img_{hash_match}_{timestamp}.png",
                    f"image_{hash_match}_{timestamp}.png",
                    f"{hash_match}_{timestamp}.png"
                ]
                filename_patterns.extend(timestamp_patterns)
                
                # Limit to reasonable number of attempts
                if len(filename_patterns) > 100:
                    break
        
        # Remove duplicates while preserving order
        seen = set()
        unique_patterns = []
        for pattern in filename_patterns:
            if pattern not in seen:
                unique_patterns.append(pattern)
                seen.add(pattern)
        
        filename_patterns = unique_patterns[:50]  # Limit to first 50 patterns
        print(f"Trying {len(filename_patterns)} filename patterns for hash: {hash_match}")
        
        # Try each pattern until we find a match
        found_blob = None
        found_filename = None
        
        for i, pattern in enumerate(filename_patterns):
            if i < 5:  # Log first 5 attempts
                print(f"Checking pattern {i+1}: {pattern}")
            blob = bucket.blob(pattern)
            if blob.exists():
                found_blob = blob
                found_filename = pattern
                print(f"✅ Found match: {pattern}")
                break
        
        if not found_blob:
            print(f"❌ No matching image found for any pattern")
            
            # List available files for debugging (limited to recent files)
            try:
                print("Listing recent files in generated/ directory...")
                blobs = list(bucket.list_blobs(prefix="generated/", max_results=30))
                available_files = [b.name for b in blobs]
                print(f"Available files: {available_files}")
                
                # Try to find files with similar hash
                if hash_match:
                    similar_files = [f for f in available_files if hash_match in f]
                    print(f"Files containing hash '{hash_match}': {similar_files}")
                    
                    # If we found similar files, try them directly
                    if similar_files:
                        print(f"Trying direct match with similar file: {similar_files[0]}")
                        direct_blob = bucket.blob(similar_files[0])
                        if direct_blob.exists():
                            found_blob = direct_blob
                            found_filename = similar_files[0]
                            print(f"✅ Direct match successful: {found_filename}")
                
                if not found_blob:
                    return corsify(jsonify({
                        "error": "Image not found", 
                        "requested": filename,
                        "hash_searched": hash_match,
                        "patterns_tried": min(5, len(filename_patterns)),
                        "available_files": available_files[:10],  # Show first 10
                        "similar_files": similar_files[:5] if hash_match else []
                    })), 404
                
            except Exception as e:
                print(f"Error listing files: {e}")
                return corsify(jsonify({
                    "error": "Image not found and could not list bucket contents", 
                    "requested": filename,
                    "listing_error": str(e)
                })), 404
        
        print(f"📥 Downloading image: {found_filename}")
        
        # Download the image data
        image_data = found_blob.download_as_bytes()
        
        # Determine content type based on file extension
        content_type = "image/png"
        if found_filename.lower().endswith(('.jpg', '.jpeg')):
            content_type = "image/jpeg"
        elif found_filename.lower().endswith('.gif'):
            content_type = "image/gif"
        elif found_filename.lower().endswith('.webp'):
            content_type = "image/webp"
        
        print(f"✅ Serving image: {found_filename}, size: {len(image_data)} bytes, type: {content_type}")
        
        # Create response with image data
        response = make_response(image_data)
        response.headers['Content-Type'] = content_type
        response.headers['Cache-Control'] = 'public, max-age=3600'  # Cache for 1 hour
        response.headers['Access-Control-Allow-Origin'] = '*'  # Explicit CORS for images
        return response
        
    except Exception as e:
        print(f"❌ Error serving image {filename}: {e}")
        return corsify(jsonify({
            "error": f"Failed to serve image: {str(e)}",
            "requested": filename
        })), 500

@app.route("/images", methods=["OPTIONS"])
@app.route("/images/<path:filename>", methods=["OPTIONS"])
def handle_images_options(filename=None):
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
