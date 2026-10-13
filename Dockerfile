FROM python:3.12-slim

# Install ffmpeg and required system tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create non-root bot user
RUN useradd -m -u 1000 -s /bin/bash botuser

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure secure directories and permissions
RUN mkdir -p data logs reports backups && \
    chown -R botuser:botuser /app && \
    chmod 700 data reports backups

USER botuser

# Healthcheck checking data/heartbeat
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -m src.tools.healthcheck || exit 1

CMD ["python", "-m", "src.main"]
