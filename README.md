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

## Architecture

This service integrates into the Avassa ecosystem as follows:

1. **Edge Deployment**: Runs on Avassa edge sites for distributed ACME challenge handling
2. **Volga Integration**: Uses Avassa's Volga messaging for reliable request/response communication
3. **Secrets Management**: Leverages Avassa Strongbox for secure credential storage
4. **Token Management**: Automatically handles Avassa API token refresh for long-running operation

The service acts as a bridge between ACME certificate managers and Cloudflare DNS, enabling automated certificate provisioning across distributed edge infrastructure.

## Avassa Deployment

This service is designed to run in the Avassa edge computing platform. Follow these steps to deploy:

### Prerequisites

1. **Container Registry**: Push the Docker image to your container registry
2. **Avassa Access**: Ensure you have access to deploy applications in your Avassa tenant
3. **Secrets Setup**: Configure required secrets in Avassa Strongbox

### 1. Build and Push Container Image

```bash
# Build the Docker image
make build IMAGE_NAME=your-registry/acme-cloudflare-callback IMAGE_TAG=1.0

# Push to your registry
make push REGISTRY=your-registry.com
```

### 2. Configure Avassa Strongbox Secrets

Create the required secrets in Avassa Strongbox using `supctl`:

```bash
# Create Cloudflare credentials vault
supctl create strongbox-vault acme-secrets

# Add Cloudflare API token
supctl create strongbox-secret acme-secrets cloudflare-credentials \
  --from-literal api-token="your_cloudflare_api_token"

# Add Avassa credentials  
supctl create strongbox-secret acme-secrets avassa-credentials \
  --from-literal role-id="your_volga_role_id"
```

### 3. Deploy the Application

Deploy using the provided Avassa specifications:

```bash
# First, create the application specification
supctl apply -f avassa-app.yaml

# Then, create the application deployment
supctl apply -f avassa-deployment.yaml
```

Alternatively, you can create deployments manually with specific configurations:

```bash
# Create application deployment to target sites
supctl create application-deployment acme-callback-deployment \
  --application acme-cloudflare-callback \
  --application-version "1.0" \
  --placement "system/type = edge"
```

### 4. Monitor Deployment

```bash
# Check deployment status
supctl get application-deployments acme-callback-deployment

# View application logs
supctl logs application acme-cloudflare-callback --service acme-callback

# Check service status
supctl get applications acme-cloudflare-callback
```

### Application Configuration

The project includes two Avassa specification files:

**Application Specification (`avassa-app.yaml`)**:
- **Container Definition**: Docker image and runtime configuration
- **Secrets Management**: Uses Avassa Strongbox for sensitive data like API tokens
- **Service Variables**: Maps secrets to environment variables securely
- **Environment Variables**: All configuration through environment variables
- **Restart Policy**: Automatic restart on failure

**Deployment Specification (`avassa-deployment.yaml`)**:
- **Placement Rules**: Targets edge sites using placement constraints
- **Canary Deployment**: Gradual rollout with staging environment validation
- **Parallel Deployment**: Controls how many sites are updated simultaneously
- **Success Thresholds**: Defines deployment success criteria

### Site-Specific Deployment

You can create custom deployment specifications for different environments. For example, create a production deployment:

```yaml
# avassa-deployment-prod.yaml
name: acme-callback-prod
application: acme-cloudflare-callback
application-version: "1.0"
placement: |
  environment = production
sites-in-parallel: 1
canary-sites: |
  city = stockholm
canary-healthy-time: 30m
success-threshold: 1.0
```

Then deploy:

```bash
# Deploy to production
supctl apply -f avassa-deployment-prod.yaml

# Or create deployments manually for specific configurations
supctl create application-deployment acme-callback-test \
  --application acme-cloudflare-callback \
  --application-version "1.0" \
  --placement "environment = test"
```

### Development and Testing

For development, you can still use Docker locally:

```bash
# Setup local development environment
make setup-dev

# Run locally with Docker
cp .env.template .env
# Edit .env with your values
make run-dev
```