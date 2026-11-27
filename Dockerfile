FROM python:3.12-slim

ARG APP_VERSION=""
ENV APP_VERSION=${APP_VERSION}

# Prevent Python from writing .pyc and enable buffer flushing
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies if required for Pillow, StoryDeck Bridge audio encoding & TZ support
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Persistent directories for disk image caching and config
RUN mkdir -p /app/cache/thumbnails /app/data

EXPOSE 5055

# Healthcheck endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5055/health || exit 1

# Run with a single Gunicorn gthread process so in-memory MQTT, scheduler, and OAuth token locks stay consistent
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "1", "--threads", "8", "--timeout", "60", "app:app"]
