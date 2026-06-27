# Sumio backend (FastAPI). A portable image you can deploy to Render, Railway,
# Fly.io, Google Cloud Run, or any container host.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install deps first so this layer is cached when only app code changes.
# pandas/openpyxl ship prebuilt wheels, so no system build tools are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only (see .dockerignore for what's excluded).
COPY app ./app

# Most hosts inject the port to listen on via $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} expands at runtime.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
