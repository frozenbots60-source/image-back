import os
import json
import uuid
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

import boto3
import requests
from urllib.parse import quote

# =========================
# Config
# =========================

MAX_FILE_SIZE = 80 * 1024 * 1024  # 80 MB

# Bucketeer / S3 config (provided by Heroku Bucketeer add-on)
BUCKETEER_AWS_ACCESS_KEY_ID = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
BUCKETEER_AWS_SECRET_ACCESS_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
BUCKETEER_AWS_REGION = os.getenv("BUCKETEER_AWS_REGION", "us-east-1")
BUCKETEER_BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

if not all([BUCKETEER_AWS_ACCESS_KEY_ID, BUCKETEER_AWS_SECRET_ACCESS_KEY, BUCKETEER_AWS_REGION, BUCKETEER_BUCKET_NAME]):
    raise RuntimeError("Bucketeer env vars are not set correctly.")

# Upstash Redis config
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not all([UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN]):
    raise RuntimeError("Upstash Redis env vars are not set correctly.")

# Optional base URL for building absolute URLs in responses
APP_BASE_URL = os.getenv("APP_BASE_URL")  # e.g. "https://your-app-name.herokuapp.com"

# =========================
# S3 Client
# =========================

s3_client = boto3.client(
    "s3",
    region_name=BUCKETEER_AWS_REGION,
    aws_access_key_id=BUCKETEER_AWS_ACCESS_KEY_ID,
    aws_secret_access_key=BUCKETEER_AWS_SECRET_ACCESS_KEY,
)

# =========================
# Upstash Redis Helpers
# =========================

def redis_request(command: str, *args: str):
    """
    Execute a Redis command against Upstash REST API.
    Usage: redis_request("set", "key", "value")
           redis_request("get", "key")
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        raise RuntimeError("Upstash Redis config missing.")

    # Build URL like: <REST_URL>/<command>/<arg1>/<arg2>...
    encoded_args = "/".join(quote(str(a), safe="") for a in args)
    url = f"{UPSTASH_REDIS_REST_URL}/{command}/{encoded_args}" if encoded_args else f"{UPSTASH_REDIS_REST_URL}/{command}"

    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
    }

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data and data["error"] is not None:
        raise RuntimeError(f"Upstash Redis error: {data['error']}")

    return data.get("result")


def save_file_metadata(file_id: str, metadata: dict):
    value = json.dumps(metadata)
    redis_request("set", f"file:{file_id}", value)


def load_file_metadata(file_id: str) -> Optional[dict]:
    result = redis_request("get", f"file:{file_id}")
    if result is None:
        return None
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return None

# =========================
# FastAPI App
# =========================

app = FastAPI(title="Simple File Hosting API")

# CORS (adjust origins if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Routes
# =========================

@app.get("/", response_class=PlainTextResponse)
def root():
    return "File Hosting API is running."


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Upload a file (image/audio/video/anything) up to 80 MB.

    Request:
      - multipart/form-data with `file` field.

    Response:
      {
        "id": "<file_id>",
        "url": "<base>/f/<file_id>",
        "filename": "original_name.ext",
        "content_type": "mime/type",
        "size": 12345
      }
    """
    # Read file into memory (simple; for huge files you'd want streaming)
    contents = await file.read()
    size = len(contents)

    if size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 80MB size limit.")

    # Generate unique S3 key and file ID
    file_id = uuid.uuid4().hex
    s3_key = f"uploads/{file_id}_{file.filename}"

    # Upload to S3 (Bucketeer)
    try:
        s3_client.put_object(
            Bucket=BUCKETEER_BUCKET_NAME,
            Key=s3_key,
            Body=contents,
            ContentType=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error uploading to storage: {str(e)}")

    # Save metadata in Redis
    metadata = {
        "file_id": file_id,
        "s3_key": s3_key,
        "filename": file.filename,
        "content_type": file.content_type or "application/octet-stream",
        "size": size,
    }

    try:
        save_file_metadata(file_id, metadata)
    except Exception as e:
        # If metadata save fails, we should probably delete from S3 to avoid orphaned objects
        try:
            s3_client.delete_object(Bucket=BUCKETEER_BUCKET_NAME, Key=s3_key)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error saving metadata: {str(e)}")

    # Build URL to access the file via this API
    if APP_BASE_URL:
        file_url = f"{APP_BASE_URL}/f/{file_id}"
    else:
        file_url = f"/f/{file_id}"

    return {
        "id": file_id,
        "url": file_url,
        "filename": file.filename,
        "content_type": metadata["content_type"],
        "size": size,
    }


@app.get("/f/{file_id}")
def get_file(file_id: str):
    """
    Redirect to a presigned S3 URL for the file.
    """
    metadata = load_file_metadata(file_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="File not found.")

    s3_key = metadata["s3_key"]

    # Generate a presigned URL (1 hour expiration)
    try:
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKETEER_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating download URL: {str(e)}")

    return RedirectResponse(url=presigned_url)


@app.get("/meta/{file_id}")
def get_metadata(file_id: str):
    """
    Get stored metadata for a file.
    Response example:
      {
        "file_id": "...",
        "filename": "...",
        "content_type": "...",
        "size": 12345,
        "s3_key": "uploads/...."
      }
    """
    metadata = load_file_metadata(file_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="File not found.")
    return JSONResponse(metadata)


# For local debugging: `python main.py`
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
