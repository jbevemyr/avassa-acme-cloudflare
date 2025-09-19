FROM python:3-slim

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
