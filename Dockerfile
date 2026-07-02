# Pinned to the tested minor (the image currently resolves python:3-slim to
# 3.14). Bump deliberately; for full reproducibility pin to a digest.
FROM python:3.14-slim

WORKDIR /app

# Installera dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiera applikation
COPY acme_callback.py .

# Kör som non-root user för säkerhet
RUN useradd -u 1000 acme && chown -R acme:acme /app
USER acme

# Starta applikationen
CMD ["python", "acme_callback.py"]
