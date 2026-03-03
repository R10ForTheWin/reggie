FROM python:3.11-slim

# System dependencies for Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and all its system dependencies via Playwright
RUN playwright install --with-deps chromium

COPY . .

RUN python3 create_icons.py

EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "2", "--timeout", "120", "--bind", "0.0.0.0:8080", "app:app"]
