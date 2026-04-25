# ── Build stage ───────────────────────────────────────────────
FROM python:3.11-slim

# System deps: ffmpeg (required by pydub + yt-dlp for audio conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Ensure required directories exist
RUN mkdir -p outputs static

# Railway injects $PORT at runtime; default 8080 for local testing
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
