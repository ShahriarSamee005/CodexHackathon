# Production image for the QueueStorm Investigator FastAPI service.
# - Slim Python 3.11 base; dependencies cached in their own layer.
# - No secrets baked in (.env is excluded via .dockerignore).
# - No model weights downloaded: the LLM is a remote Groq API call, so the
#   image stays well under 500MB.
FROM python:3.11-slim

# Clean container logs; no .pyc clutter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first — this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source (flat layout: main.py, analyzer.py, llm.py, safety.py, models.py).
# .dockerignore keeps .env, __pycache__, and tooling caches out of the image.
COPY . .

EXPOSE 8000
ENV PORT=8000

# Shell form so ${PORT} is expanded at runtime (Railway injects its own PORT).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
