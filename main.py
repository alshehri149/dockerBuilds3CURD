# main.py
from flask import Flask, request, jsonify
from google.cloud import firestore, storage
import os
import uuid # For generating unique IDs for media files
import base64 # For decoding base64 media

app = Flask(__name__)

# Initialize Firestore and Cloud Storage clients
# These clients automatically pick up credentials from the environment
# when deployed on GCP services like Cloud Run.
db = firestore.Client()
storage_client = storage.Client()

# Configuration (from environment variables)
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "genai_history")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME") # THIS MUST BE SET IN CLOUD RUN ENV VARS!
if not GCS_BUCKET_NAME:
    # This will raise an error if not set, which is good for early detection
    raise ValueError("GCS_BUCKET_NAME environment variable is not set.")
bucket = storage_client.bucket(GCS_BUCKET_NAME)

@app.route('/prompts', methods=['POST'])
def create_prompt_entry():
    """
    Creates a new prompt entry in Firestore and optionally uploads media to Cloud Storage.
    Expects JSON payload with 'prompt' and optionally 'media_file_base64' (base64 encoded) and 'media_type'.
    """
    data = request.json
    if not data or 'prompt' not in data:
        return jsonify({"error": "Prompt is required"}), 400

    prompt_text = data['prompt']
    media_url = None
    media_type = None

    if 'media_file_base64' in data and 'media_type' in data:
        try:
            media_bytes = base64.b64decode(data['media_file_base64'])
            media_type = data['media_type'] # e.g., 'image/png', 'audio/mpeg', 'video/mp4'

            # Generate a unique filename for Cloud Storage
            file_extension = media_type.split('/')[-1] if '/' in media_type else 'bin'
            file_name = f"{uuid.uuid4()}.{file_extension}"
            blob = bucket.blob(file_name)

            # Upload to Cloud Storage
            blob.upload_from_string(media_bytes, content_type=media_type)
            # Make the blob publicly readable. For production, consider signed URLs for private access.
            blob.make_public()
            media_url = blob.public_url

        except Exception as e:
            app.logger.error(f"Failed to upload media: {str(e)}") # Log the error
            return jsonify({"error": f"Failed to upload media: {str(e)}"}), 500

    # Create Firestore document
    try:
        doc_ref = db.collection(FIRESTORE_COLLECTION).add({
            'prompt': prompt_text,
            'timestamp': firestore.SERVER_TIMESTAMP,
            'media_url': media_url,
            'media_type': media_type,
            'genai_response': data.get('genai_response'),
            'metadata': data.get('metadata', {})
        })
        return jsonify({"id": doc_ref[1].id, "message": "Prompt entry created", "media_url": media_url}), 201
    except Exception as e:
        app.logger.error(f"Failed to save to Firestore: {str(e)}")
        return jsonify({"error": f"Failed to save to Firestore: {str(e)}"}), 500

@app.route('/prompts', methods=['GET'])
def get_all_prompts():
    """Retrieves all prompt entries from Firestore, ordered by timestamp."""
    try:
        docs = db.collection(FIRESTORE_COLLECTION).order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
        results = []
        for doc in docs:
            prompt_data = doc.to_dict()
            prompt_data['id'] = doc.id
            # Convert timestamp to string for JSON serialization if needed,
            # otherwise it might be a Firestore Timestamp object.
            if 'timestamp' in prompt_data and hasattr(prompt_data['timestamp'], 'isoformat'):
                prompt_data['timestamp'] = prompt_data['timestamp'].isoformat()
            results.append(prompt_data)
        return jsonify(results), 200
    except Exception as e:
        app.logger.error(f"Failed to retrieve prompts: {str(e)}")
        return jsonify({"error": f"Failed to retrieve prompts: {str(e)}"}), 500

@app.route('/prompts/<string:prompt_id>', methods=['GET'])
def get_prompt_by_id(prompt_id):
    """Retrieves a specific prompt entry by ID."""
    try:
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(prompt_id)
        doc = doc_ref.get()
        if doc.exists:
            prompt_data = doc.to_dict()
            prompt_data['id'] = doc.id
            if 'timestamp' in prompt_data and hasattr(prompt_data['timestamp'], 'isoformat'):
                prompt_data['timestamp'] = prompt_data['timestamp'].isoformat()
            return jsonify(prompt_data), 200
        else:
            return jsonify({"error": "Prompt not found"}), 404
    except Exception as e:
        app.logger.error(f"Failed to retrieve prompt: {str(e)}")
        return jsonify({"error": f"Failed to retrieve prompt: {str(e)}"}), 500

@app.route('/prompts/<string:prompt_id>', methods=['PUT'])
def update_prompt_entry(prompt_id):
    """Updates an existing prompt entry."""
    data = request.json
    if not data:
        return jsonify({"error": "No update data provided"}), 400

    try:
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(prompt_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Prompt not found"}), 404

        update_data = {}
        if 'prompt' in data:
            update_data['prompt'] = data['prompt']
        if 'genai_response' in data:
            update_data['genai_response'] = data['genai_response']
        if 'metadata' in data:
            update_data['metadata'] = data['metadata']
        # For media updates, you would typically have a separate endpoint or more complex logic
        # (e.g., delete old blob, upload new one, update media_url).
        # For simplicity, this example doesn't directly support media updates via PUT.

        if update_data: # Only update if there's something to update
            doc_ref.update(update_data)
            return jsonify({"message": "Prompt entry updated"}), 200
        else:
            return jsonify({"message": "No valid fields to update"}), 200
    except Exception as e:
        app.logger.error(f"Failed to update prompt: {str(e)}")
        return jsonify({"error": f"Failed to update prompt: {str(e)}"}), 500

@app.route('/prompts/<string:prompt_id>', methods=['DELETE'])
def delete_prompt_entry(prompt_id):
    """Deletes a prompt entry from Firestore and optionally its associated media from Cloud Storage."""
    try:
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(prompt_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Prompt not found"}), 404

        prompt_data = doc.to_dict()
        media_url = prompt_data.get('media_url')

        # Delete from Firestore
        doc_ref.delete()
        app.logger.info(f"Deleted Firestore document: {prompt_id}")

        # Optionally delete media from Cloud Storage
        if media_url:
            # Extract filename from URL (assumes public URL structure like:
            # https://storage.googleapis.com/<bucket_name>/<filename>)
            # Be careful with this parsing in production; use a more robust method
            # if your URLs might vary.
            try:
                # A safer way to get the blob name if you stored the full URL is to use the
                # path component after the bucket name.
                # This example assumes a simple public URL.
                from urllib.parse import urlparse
                parsed_url = urlparse(media_url)
                # Example: /<bucket_name>/<object_name>
                # We need just the object_name, which is everything after the bucket name
                path_parts = parsed_url.path.strip('/').split('/')
                if len(path_parts) > 1 and path_parts[0] == GCS_BUCKET_NAME:
                    file_name = '/'.join(path_parts[1:])
                else:
                    file_name = None # Could not parse reliably

                if file_name:
                    blob = bucket.blob(file_name)
                    if blob.exists():
                        blob.delete()
                        app.logger.info(f"Deleted media file from GCS: {file_name}")
                    else:
                        app.logger.warning(f"Media file not found in GCS for deletion: {file_name}")
                else:
                    app.logger.warning(f"Could not extract filename from URL: {media_url} for deletion.")
            except Exception as e:
                app.logger.error(f"Error deleting media from GCS: {str(e)}")


        return jsonify({"message": "Prompt entry and associated media deleted"}), 200
    except Exception as e:
        app.logger.error(f"Failed to delete prompt: {str(e)}")
        return jsonify({"error": f"Failed to delete prompt: {str(e)}"}), 500

if __name__ == '__main__':
    # For local development, you might need to set environment variables or
    # configure service account key JSON for authentication.
    # Example for local:
    # os.environ["GCS_BUCKET_NAME"] = "your-project-id-genai-media"
    # os.environ["FIRESTORE_COLLECTION"] = "genai_history"
    # os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/path/to/your/service-account-key.json"
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
