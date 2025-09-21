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
- ✅ Multi-instance domain filtering for horizontal scaling
- ✅ Shared consumer mode for concurrent processing
- ✅ Complete error handling and logging
- ✅ Configured via environment variables

## Installation

```bash
pip install -r requirements.txt
```

The service uses the [official Cloudflare Python library](https://pypi.org/project/cloudflare/) for robust, typed API interactions with Cloudflare's DNS services.

**Alternative**: A simpler shell script implementation (`acme_callback.sh`) is also provided for basic integrations or testing scenarios.

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

# Domain filtering (optional - for multi-instance deployments)
export MANAGED_DOMAINS="example.com,test.org"              # comma-separated domains this instance manages

# Debugging (optional)
export ACME_DEBUG_DNS_VERIFICATION="true"                  # enable DNS verification checks for debugging validation failures
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

## Multi-Instance Domain Filtering

The service supports horizontal scaling through domain-based partitioning:

- **Domain-Specific Instances**: Each instance can be configured to handle specific domains using `MANAGED_DOMAINS`
- **Shared Consumer Mode**: Uses Volga shared consumers to allow multiple instances to process messages concurrently
- **Message Filtering**: Only processes messages for domains it manages, allowing other instances to handle their domains
- **Hierarchical Domains**: If an instance manages "example.com", it also handles subdomains like "sub.example.com"
- **No Filtering**: If `MANAGED_DOMAINS` is not set, the instance handles all domains (backward compatibility)

### Example Multi-Instance Setup:

```bash
# Instance 1 - handles example.com and its subdomains
export MANAGED_DOMAINS="example.com"

# Instance 2 - handles test.org and demo.net
export MANAGED_DOMAINS="test.org,demo.net"  

# Instance 3 - handles all other domains
# (don't set MANAGED_DOMAINS)
```

## Token Management

The service automatically manages Avassa API token refresh to ensure continuous operation:

- **Automatic Refresh**: Tokens are automatically refreshed 5 minutes before expiration
- **Background Task**: A dedicated background task monitors token expiration
- **Refresh Endpoint**: Uses `/v1/state/strongbox/token/refresh` to obtain new tokens
- **Graceful Handling**: If token refresh fails, the service logs errors but continues attempting
- **Continuous Operation**: The service maintains an endless loop processing Volga messages

## Debugging ACME Validation Failures

When ACME validation fails (like in your Pebble logs), the enhanced logging helps diagnose issues:

### Enhanced Logging Features

- **Detailed Challenge Operations**: Shows all existing TXT records and whether they match the expected challenge value
- **Zone Information**: Logs which Cloudflare zone is being used and its ID
- **Record Verification**: Confirms TXT records are created and visible in Cloudflare DNS
- **DNS Verification Mode**: Optional comprehensive DNS resolution testing

### Enable Debug Mode

Set the `ACME_DEBUG_DNS_VERIFICATION=true` environment variable to enable comprehensive DNS verification:

```bash
export ACME_DEBUG_DNS_VERIFICATION=true
```

In debug mode, after creating challenge records, the service will:

1. **Test DNS resolution** from multiple servers (Cloudflare 1.1.1.1, Google 8.8.8.8, system resolver)
2. **Check propagation consistency** across different DNS servers
3. **Identify common issues** like multiple conflicting records or propagation delays
4. **Report validation problems** that might cause ACME verification to fail

### Common Validation Failure Causes

The debug logging helps identify these common issues:

- **DNS Propagation Delays**: Record created in Cloudflare but not yet visible to ACME validators
- **Multiple Conflicting Records**: Different TXT values for the same challenge name
- **DNS Resolution Inconsistency**: Challenge visible on some DNS servers but not others
- **Wrong Challenge Value**: TXT record content doesn't match ACME expectation
- **TTL Issues**: Very short TTL causing records to expire before validation

### Example Debug Output

```
✅ ACME challenge record created successfully:
   Domain: foo.valudden17.com
   FQDN: _acme-challenge.foo.valudden17.com
   Zone: valudden17.com (1234567890abcdef)
   Record ID: abcdef1234567890
   TTL: 120s
   Challenge Value: abc123def456...

🔍 Running DNS verification check (debug mode)...
   Cloudflare DNS: ✅ Found challenge value
   Google DNS: ❌ Missing challenge value  
   System DNS: ✅ Found challenge value

⚠️ DNS verification found potential issues:
   • Challenge value not consistently resolved across DNS servers
```

## Logging

The module uses Python's standard logging at INFO level. Logs include:
- Connection to Volga and token refresh events
- Detailed challenge operations with existing record analysis
- Cloudflare zone discovery and DNS record operations
- Message handling with domain filtering decisions
- Optional DNS verification and propagation testing
- Comprehensive error reporting and warnings

## Error types

Outgoing error messages can contain the following error types:

- `zone_not_found`: No Cloudflare zone found for the specified record name
- `add_failed`: Could not add TXT record
- `remove_failed`: Could not remove TXT record  
- `invalid_action`: Unknown action (must be "add" or "remove")

## Architecture

This service integrates into the Avassa ecosystem as follows:

1. **Edge Deployment**: Runs on Avassa edge sites for distributed ACME challenge handling
2. **Volga Integration**: Uses Avassa's Volga messaging with hardcoded topics (`acme:requests`/`acme:events`) and shared consumer mode for reliable multi-instance operation
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
# Build the Docker image (uses default target - same as 'make build')
make all IMAGE_TAG=1.0

# Or build explicitly
make build IMAGE_TAG=1.0

# Or build with explicit image name
make build IMAGE_NAME=avassa/acme-cloudflare-callback IMAGE_TAG=1.0

# Push to registry (if using external registry)
make push REGISTRY=your-registry.com
```

### 2. Configure Avassa Strongbox Secrets

Create the required secrets in Avassa Strongbox using `supctl`:

```bash
# Create Cloudflare credentials vault
supctl create strongbox vaults <<EOF
name: acme-secrets
EOF

# Add Cloudflare API token
supctl create strongbox vaults acme-secrets secrets <<EOF
name: avassa-credentials
allow-image-access: [ "*" ]
data:
  api-token: "..."
EOF

```

### 3. Deploy the Application

Deploy using the provided Avassa specifications:

```bash
# First, create the application specification
supctl create applications < avassa-app.yaml

# Then, create the application deployment
supctl create application-deployment < avasas-deployment.yaml
```


