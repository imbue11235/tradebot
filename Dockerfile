FROM python:3.12-slim

WORKDIR /app

# System deps (needed for torch + some ML libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cached layer unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Logs dir (will be mounted as a volume)
RUN mkdir -p /app/logs

# Default: run the bot. Override in docker-compose for the dashboard.
CMD ["python", "main.py"]
