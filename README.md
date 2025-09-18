# Avassa ACME Cloudflare Callback

A Python callback module that listens on Volga topics for JSON messages instructing it to add/remove ACME dns-01 challenge TXT records on Cloudflare.

## Features

- ✅ Listens on Volga topic for ACME challenge requests
- ✅ Automatic Cloudflare zone discovery (strips labels from left until matching zone is found)
- ✅ Idempotent "add" operations (reuses existing records with same content)
- ✅ Safe "remove" operations (only removes records with exactly matching value)
- ✅ Publishes acknowledgement messages on output topic
- ✅ Automatic Avassa API token refresh before expiration
- ✅ Continuous message processing with robust error handling
- ✅ Complete error handling and logging
- ✅ Configured via environment variables

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

All settings are retrieved from environment variables:

### Required variables:

```bash
# Cloudflare API token with Zone DNS:Edit permissions for relevant zones
export CF_API_TOKEN="your_cloudflare_token"

# Avassa AppRole credentials  
export VOLGA_ROLE_ID="your_role_id"
export APPROLE_SECRET_ID="${SYS_APPROLE_SECRET_ID}"  # Injected by Avassa
export API_CA_CERT="${SYS_API_CA_CERT}"              # Injected by Avassa
```

### Optional variables:

```bash
# Cloudflare API settings
export CF_API_BASE="https://api.cloudflare.com/client/v4"  # default
export CF_DEFAULT_TTL="120"                                 # default

# Avassa / Volga settings  
export AVASSA_API_HOST="https://api.internal:4646"         # default
export VOLGA_TOPIC_IN="acme:requests"                      # default
export VOLGA_TOPIC_OUT="acme:events"                       # default
export VOLGA_CONSUMER_MODE="exclusive"                     # default (or "shared")
export VOLGA_POSITION="latest"                             # default (or "beginning")
```

## Usage

```bash
python acme_callback.py
```

## Message schemas

### Incoming messages (JSON in Volga message payload):

```json
{
  "id": "optional-correlation-id",
  "action": "add",
  "domain": "example.com",
  "name": "_acme-challenge.example.com", 
  "value": "acme-challenge-token",
  "ttl": 120
}
```

```json
{
  "id": "optional-correlation-id", 
  "action": "remove",
  "domain": "example.com",
  "name": "_acme-challenge.example.com",
  "value": "acme-challenge-token"
}
```

### Outgoing acknowledgements (JSON payload):

**Success:**
```json
{
  "id": "echoed-correlation-id",
  "action": "add", 
  "name": "_acme-challenge.example.com",
  "value": "acme-challenge-token",
  "status": "ok",
  "record_id": "cloudflare-record-id",
  "zone_id": "cloudflare-zone-id"
}
```

**Error:**
```json
{
  "id": "echoed-correlation-id",
  "action": "add",
  "name": "_acme-challenge.example.com", 
  "value": "acme-challenge-token",
  "status": "error",
  "zone_id": "cloudflare-zone-id", 
  "error": {
    "type": "zone_not_found",
    "message": "Could not find Cloudflare zone for _acme-challenge.example.com"
  }
}
```

## Zone Discovery

The module finds the correct Cloudflare zone by successively stripping labels from the left in the record name until a matching zone is found:

```
_acme-challenge.www.example.co.uk
→ try www.example.co.uk  
→ try example.co.uk ✓ (zone found)
```

## Idempotency & Safety

- **Add operations**: Checks if a TXT record with the same name+value already exists and reuses its ID instead of creating duplicates.

- **Remove operations**: Only removes TXT records whose content exactly matches the specified value, leaving other TXT records at the same name intact.

## Token Management

The service automatically manages Avassa API token refresh to ensure continuous operation:

- **Automatic Refresh**: Tokens are automatically refreshed 5 minutes before expiration
- **Background Task**: A dedicated background task monitors token expiration
- **Refresh Endpoint**: Uses `/v1/state/strongbox/token/refresh` to obtain new tokens
- **Graceful Handling**: If token refresh fails, the service logs errors but continues attempting
- **Continuous Operation**: The service maintains an endless loop processing Volga messages

## Logging

The module uses Python's standard logging at INFO level. Logs include:
- Connection to Volga
- Cloudflare zone discovery
- DNS record operations  
- Message handling
- Errors and warnings

## Error types

Outgoing error messages can contain the following error types:

- `zone_not_found`: No Cloudflare zone found for the specified record name
- `add_failed`: Could not add TXT record
- `remove_failed`: Could not remove TXT record  
- `invalid_action`: Unknown action (must be "add" or "remove")

## Avassa Deployment

For deployment in Avassa, set up a workload with:

1. **Image**: Python 3.9+ with this module
2. **Environment variables**: Configure as above
3. **Secrets**: Cloudflare API token via Avassa secrets
4. **AppRole**: Configure for Volga access
5. **Resources**: Minimal CPU/memory (this is a lightweight service)

Example workload snippet:
```yaml
spec:
  containers:
  - name: acme-callback
    image: your-registry/acme-callback:latest
    env:
    - name: CF_API_TOKEN
      valueFrom:
        secretKeyRef:
          name: cloudflare-secret
          key: api-token
    - name: VOLGA_ROLE_ID 
      value: "your-role-id"
    - name: APPROLE_SECRET_ID
      value: "${SYS_APPROLE_SECRET_ID}"
    - name: API_CA_CERT
      value: "${SYS_API_CA_CERT}"
```

## Docker

### Using the Makefile (Recommended)

The project includes a comprehensive Makefile for easy Docker management:

```bash
# Show all available commands
make help

# Setup development environment (copies .env.template to .env and installs deps)
make setup-dev

# Build Docker image
make build

# Run with docker-compose (requires .env file)
make run

# View logs
make logs

# Stop services
make stop

# Clean up
make clean
```

### Manual Docker Commands

If you prefer to use Docker directly:

```bash
# Build the image
docker build -t acme-callback .

# Run with docker-compose
cp .env.template .env
# Edit .env with your values
docker-compose up -d
```

### Available Makefile Targets

- `make build` - Build Docker image
- `make run` - Run with docker-compose in background
- `make run-fg` - Run with docker-compose in foreground
- `make stop` - Stop running services
- `make logs` - Show container logs
- `make shell` - Open shell in running container
- `make clean` - Remove Docker images and containers
- `make lint` - Run Python code linting
- `make test` - Run tests (when available)
- `make setup-dev` - Setup development environment

### Configuration

The Makefile supports several variables:

```bash
# Build with custom image name/tag
make build IMAGE_NAME=my-acme-callback IMAGE_TAG=v1.0

# Push to registry
make push REGISTRY=your-registry.com

# Pull from registry
make pull REGISTRY=your-registry.com
```