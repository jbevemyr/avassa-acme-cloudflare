# How to Set Up ACME Certificate Provisioning

This guide explains how to configure automatic certificate provisioning using ACME services (like Let's Encrypt) in Avassa. It covers both centralized and distributed certificate management scenarios, with support for DNS delegation and external DNS callback methods.

## Prerequisites

- Avassa Control Tower and edge sites deployed
- Access to modify DNS settings for your domains (either delegation or API access)
- ACME service provider account (Let's Encrypt, internal ACME server, etc.)

## Overview

Avassa supports two primary ACME certificate provisioning scenarios:

1. **Centralized Provisioning**: Certificates requested at Control Tower using static requests and automatically distributed to edge sites via distributed vaults
2. **Edge Site Provisioning**: Certificates provisioned for edge sites using auto-ACME secrets or static requests from Control Tower with targeted distribution

Each scenario supports two DNS challenge handling methods:

- **DNS Delegation**: DNS for the domain is delegated to the Avassa site
- **DNS Callback**: External DNS provider (like Cloudflare) handles challenges via callback service

**Key Features:**
- **Static Requests**: Configure once, automatically provision and renew certificates (Control Tower only)
- **Distributed Vaults**: Automatically distribute certificates to all edge sites
- **Auto-ACME Secrets**: Dynamic certificate provisioning based on service deployment context

> **Important Note**: Static requests are only processed at the Control Tower. Edge sites can use manual `request-cert` commands or receive certificates via static requests configured at the Control Tower with targeted vault distribution.

## Scenario 1: Centralized Certificate Provisioning

Use this approach when the same hostname is used across multiple edge sites, with local DNS pointing to the local service instance.

### Step 1: Configure ACME Service at Control Tower

Create an ACME service configuration for your certificate authority:

```bash
# For Let's Encrypt Staging
supctl create strongbox acme-services <<EOF
name: letsencrypt-staging
contact-email: admin@yourcompany.com
directory-url: https://acme-staging-v02.api.letsencrypt.org/directory
callback-domains:
  - yourcompany.com
  - app.yourcompany.com
EOF

# For Let's Encrypt Production  
supctl create strongbox acme-services <<EOF
name: letsencrypt-prod
contact-email: admin@yourcompany.com
directory-url: https://acme-v02.api.letsencrypt.org/directory
callback-domains:
  - yourcompany.com
  - app.yourcompany.com
EOF
```

### Step 2: Set Up DNS Callback Service (if using external DNS)

If your DNS is managed by an external provider like Cloudflare, deploy the callback service:

#### Create Secrets for DNS Provider

```bash
# Create vault for DNS provider credentials
supctl create strongbox vaults <<EOF
name: acme-secrets
EOF

# Add Cloudflare API token
supctl create strongbox vaults acme-secrets secrets <<EOF
name: cloudflare-credentials
allow-image-access: [ "*" ]
data:
  api-token: "your_cloudflare_api_token"
EOF
```

#### Configure Callback Policies and Authentication

```bash
# Create Volga topic policies
supctl create policy policies <<EOF
name: acme-callback
volga:
  topics:
    - name: "acme:requests"
      operations:
        create: allow
        consume: allow
    - name: "acme:events"
      operations:
        create: allow
        produce: allow
EOF

# Create AppRole for callback service
supctl create strongbox authentication approles <<EOF
name: acme-callback
fixed-role-id: acme-role-id
token-policies:
  - acme-callback
token-ttl: 24h
token-type: service
distribute:
  to: all
EOF
```

#### Deploy Callback Application

```bash
# Deploy the ACME callback service
supctl create applications <<EOF
name: acme-cloudflare-callback
version: "1.0"
services:
  - name: acme-callback
    mode: replicated
    replicas: 1
    variables:
      - name: CF_API_TOKEN
        value-from-vault-secret:
          vault: acme-secrets
          secret: cloudflare-credentials
          key: api-token
    containers:
      - name: acme-callback
        image: avassa/acme-cloudflare-callback:1.0
        approle: acme-callback
        env:
          CF_API_TOKEN: ${CF_API_TOKEN}
          VOLGA_ROLE_ID: "acme-role-id"
          APPROLE_SECRET_ID: ${SYS_APPROLE_SECRET_ID}
          API_CA_CERT: ${SYS_API_CA_CERT}
          CF_API_BASE: "https://api.cloudflare.com/client/v4"
          CF_DEFAULT_TTL: "120"
          AVASSA_API_HOST: "https://api.internal:4646"
          MANAGED_DOMAINS: "yourcompany.com"
          ACME_DEBUG_DNS_VERIFICATION: "true"
        restart-policy: always
EOF

# Deploy to Control Tower
supctl create application-deployments <<EOF
name: acme-callback-deployment
application: acme-cloudflare-callback
application-version: "1.0"
placement: |
  system/type = control-tower
sites-in-parallel: 1
EOF
```

### Step 3: Configure Automatic Certificate Distribution

Create a distributed vault that will receive the certificates:

```bash
# Create distributed certificate vault
supctl create strongbox vaults <<EOF
name: certs
distribute:
  to: all
EOF
```

### Step 4: Configure Static Certificate Request

Configure the ACME service to automatically request certificates and populate the distributed vault:

```bash
# Configure static request for automatic certificate provisioning
supctl create strongbox acme-services letsencrypt-staging static-requests <<EOF
names: app.yourcompany.com,api.yourcompany.com
vault: certs
secret: web-services
EOF
```

The ACME service will now automatically:
1. **Request certificates** for the specified names
2. **Handle DNS challenges** (via callback or delegation)
3. **Store certificates** in the `certs` vault under the specified secret name
4. **Distribute** to all edge sites immediately
5. **Renew automatically** before expiration

The secret will be populated with standard ACME certificate files:
- `acme-cert.pem` - The certificate
- `acme-cert.key` - The private key  
- `acme-chain.pem` - The certificate chain

### Step 5: Use Certificate in Edge Applications

Deploy applications that use the automatically distributed certificates:

```bash
supctl create applications <<EOF
name: web-service
version: "1.0"
services:
  - name: web
    variables:
      - name: TLS_CERT
        value-from-vault-secret:
          vault: certs
          secret: web-services  # This matches the secret name from static-requests
          key: acme-cert.pem
      - name: TLS_KEY
        value-from-vault-secret:
          vault: certs
          secret: web-services
          key: acme-cert.key
      - name: TLS_CHAIN
        value-from-vault-secret:
          vault: certs
          secret: web-services
          key: acme-chain.pem
    network:
      ingress-ip-per-instance:
        protocols:
          - name: tcp
            port-ranges: "443"
    containers:
      - name: nginx
        image: nginx:alpine
        env:
          TLS_CERT: ${TLS_CERT}
          TLS_KEY: ${TLS_KEY}
          TLS_CHAIN: ${TLS_CHAIN}
        # Configure nginx to use the certificates
    mode: replicated
    replicas: 1
EOF
```

## Scenario 2: Edge Site Certificate Provisioning

For edge sites, Avassa provides two recommended approaches:

## Approach 1: Auto-ACME Certificates (Recommended)

Auto-ACME certificates are the simplest way to provision certificates for edge sites. The certificates are automatically requested, provisioned, and renewed.

### Step 1: Configure ACME Service

First ensure you have an ACME service configured (either at Control Tower or locally at the edge site):

#### Option A: Using External ACME Service (Let's Encrypt)

```bash
# Configure for Let's Encrypt with DNS callback
supctl create strongbox acme-services <<EOF
name: letsencrypt-distributed
contact-email: admin@yourcompany.com
directory-url: https://acme-v02.api.letsencrypt.org/directory
callback-domains:
  - yourcompany.com
EOF
```

#### Option B: Using Local ACME Service (Pebble)

First deploy a local ACME server:

```bash
# Deploy Pebble ACME server at the site
supctl create applications <<EOF
name: pebble
version: "1.0"  
services:
  - name: pebble
    mode: replicated
    replicas: 1
    network:
      ingress-ip-per-instance:
        protocols:
          - name: tcp
            port-ranges: "14000,15000"
        access:
          allow-all: true
      outbound-access:
        allow-all: true
    containers:
      - name: pebble
        image: ghcr.io/letsencrypt/pebble
        cmd: [ "-config", "/test/config/pebble-config.json", "-strict" ]
        env:
          PEBBLE_VA_NOSLEEP: "0"
          PEBBLE_VA_ALWAYS_VALID: "0"
EOF

# Deploy to specific site
supctl create application-deployments <<EOF  
name: pebble-deployment
application: pebble
application-version: "1.0"
placement: |
  system/name = your-edge-site
sites-in-parallel: 1
EOF
```

Then configure ACME service to use the local server:

```bash
# Wait for Pebble to start and get its CA certificate
curl -k https://pebble-ingress-ip:14000/root > pebble.minica.pem

# Configure ACME service for local Pebble
supctl create strongbox acme-services <<EOF
name: local-pebble
contact-email: admin@yourcompany.com
directory-url: https://pebble.pebble.tenant.sitename.site.test:14000/dir
server-name-indication: pebble.pebble.tenant.sitename.site.test
use-root-ca-certs: false
tls-verify: false
api-ca-cert: |
$(cat pebble.minica.pem | sed 's/^/  /g')
EOF
```

### Step 2: Deploy DNS Callback Service (if needed)

If using external DNS, deploy the callback service to the same site where certificates will be requested:

```bash
# Deploy callback service to edge site
supctl create application-deployments <<EOF
name: callback-deployment
application: acme-cloudflare-callback
application-version: "1.0"
placement: |
  system/name = your-edge-site
sites-in-parallel: 1
EOF
```

### Step 2: Create Auto-ACME Certificate Vault

Create a vault with automatic certificate provisioning:

```bash
# Create vault for auto-generated certificates
supctl create strongbox vaults <<EOF
name: auto-certs
distribute:
  to: all  # or specific sites if needed
EOF

# Configure auto-ACME certificate
supctl create strongbox vaults auto-certs secrets <<EOF
name: service-cert
allow-image-access: [ "*" ]
auto-acme-cert:
  acme-service: letsencrypt-prod  # or your configured ACME service
  names:
    - myservice.yourcompany.com
    - api.yourcompany.com
EOF
```

#### Dynamic Certificate Names

For certificates that automatically match your service deployment patterns:

```bash
# Certificate with dynamic naming based on deployment context
supctl create strongbox vaults auto-certs secrets <<EOF
name: dynamic-cert
allow-image-access: [ "*" ]
auto-acme-cert:
  acme-service: letsencrypt-prod
  names:
    - ${SYS_SERVICE}.${SYS_APP}.${SYS_TENANT}.${SYS_SITE}.${SYS_GLOBAL_DOMAIN}
EOF
```

This will automatically generate certificates for names like `web.myapp.acme.edge-site-01.site.test`.

**Benefits of Auto-ACME approach:**
- ✅ Certificates automatically provisioned when applications are deployed
- ✅ No manual certificate management required
- ✅ Automatic renewal handled by Avassa
- ✅ Works with dynamic service naming patterns
- ✅ Scales automatically with application deployments

## Approach 2: Static Requests with Targeted Distribution

Use static requests from the Control Tower to provision certificates and distribute them to specific edge sites.

### Step 1: Create Targeted Distribution Vault

```bash
# Create vault that distributes to specific edge sites
supctl create strongbox vaults <<EOF
name: edge-site-certs
distribute:
  to:
    - edge-site-01
    - edge-site-02
EOF
```

### Step 2: Configure Static Request at Control Tower

```bash
# Configure static request for edge site certificates (only at Control Tower)
supctl create strongbox acme-services letsencrypt-prod static-requests <<EOF
names: app.edge-site-01.yourcompany.com,api.edge-site-01.yourcompany.com
vault: edge-site-certs
secret: edge-01-certs
EOF
```

**Benefits of Static Requests approach:**
- ✅ Centralized certificate management from Control Tower
- ✅ Explicit control over which certificates are provisioned
- ✅ Can target specific edge sites with vault distribution
- ✅ Good for well-known, stable certificate requirements
- ✅ Easy to monitor and troubleshoot from Control Tower

### Step 3: Use Certificates in Edge Applications

Both auto-ACME certificates and static request distributed certificates can be used in applications:

```bash
supctl create applications <<EOF
name: secure-web-app
version: "1.0"
services:
  - name: web
    variables:
      - name: TLS_CERT
        value-from-vault-secret:
          vault: auto-certs
          secret: service-cert
          key: acme-cert.pem
      - name: TLS_KEY  
        value-from-vault-secret:
          vault: auto-certs
          secret: service-cert
          key: acme-cert.key
      - name: TLS_CHAIN
        value-from-vault-secret:
          vault: auto-certs
          secret: service-cert
          key: acme-chain.pem
    network:
      ingress-ip-per-instance:
        protocols:
          - name: tcp
            port-ranges: "443"
    containers:
      - name: web-server
        image: nginx:alpine
        env:
          TLS_CERT: ${TLS_CERT}
          TLS_KEY: ${TLS_KEY}
          TLS_CHAIN: ${TLS_CHAIN}
    mode: replicated
    replicas: 1
EOF
```

## DNS Challenge Methods

### Method 1: DNS Delegation to Avassa

When Avassa can manage DNS for your domain:

1. **Delegate DNS subdomain** to your Avassa site's DNS server
2. **Configure ACME service** without `callback-domains`
3. **Avassa handles challenges automatically** using its built-in DNS server

```bash
# ACME service with DNS delegation (no callback-domains)
supctl create strongbox acme-services <<EOF
name: letsencrypt-delegated
contact-email: admin@yourcompany.com
directory-url: https://acme-v02.api.letsencrypt.org/directory
# Note: no callback-domains - Avassa will handle DNS directly
EOF
```

### Method 2: DNS Callback (External DNS Provider)

When DNS is managed externally (registrar, Cloudflare, etc.):

1. **Configure callback domains** in ACME service
2. **Deploy callback service** that listens to `acme:requests` topic
3. **Callback service manages DNS** via external provider API

#### ACME Service Configuration

```bash
supctl create strongbox acme-services <<EOF
name: letsencrypt-callback
contact-email: admin@yourcompany.com
directory-url: https://acme-v02.api.letsencrypt.org/directory
callback-domains:
  - yourcompany.com
  - subsidiary.com
EOF
```

#### Callback Message Flow

**Challenge Add Request** (sent to `acme:requests` topic):
```json
{
  "action": "add",
  "domain": "yourcompany.com",
  "ttl": 120,
  "value": "7J7pGj9O2Ye2OjaBhBRbGPlEUR7uqEBxRmvFv4B8maY",
  "name": "_acme-challenge.app.yourcompany.com",
  "id": "ee57cfef-66e1-4b71-8cf3-ba74599957a8"
}
```

**Challenge Response** (sent to `acme:events` topic):
```json
{
  "status": "ok",
  "id": "ee57cfef-66e1-4b71-8cf3-ba74599957a8"
}
```

**Challenge Remove Request**:
```json
{
  "action": "remove", 
  "domain": "yourcompany.com",
  "value": "7J7pGj9O2Ye2OjaBhBRbGPlEUR7uqEBxRmvFv4B8maY",
  "name": "_acme-challenge.app.yourcompany.com",
  "id": "fc80e166-fccd-4935-98ef-16184fb1b78c"
}
```

### Simple Shell Script Callback Example

For simpler integrations or testing, you can use a shell script callback instead of the full Python service:

```bash
#!/bin/bash
# Simple ACME DNS callback using supctl and curl

# Configuration
DNS_PROVIDER="cloudflare"  # or "manual" for testing
CF_API_TOKEN="your_cloudflare_api_token"
MANAGED_DOMAINS="yourcompany.com"

# Listen for ACME requests and process them
while true; do
    # Consume message from Volga topic
    message=$(supctl do volga topics acme:requests consume --payload-only --timeout 30s 2>/dev/null || true)
    
    if [[ -n "$message" ]]; then
        # Parse message
        action=$(echo "$message" | jq -r '.action')
        domain=$(echo "$message" | jq -r '.domain')
        name=$(echo "$message" | jq -r '.name')
        value=$(echo "$message" | jq -r '.value')
        id=$(echo "$message" | jq -r '.id')
        
        echo "Processing $action for $name (domain: $domain)"
        
        # Handle DNS challenge (implement your DNS provider logic here)
        if [[ "$action" == "add" ]]; then
            # Add TXT record to DNS
            echo "Would add TXT record: $name = '$value'"
            status="ok"
        elif [[ "$action" == "remove" ]]; then
            # Remove TXT record from DNS  
            echo "Would remove TXT record: $name with value '$value'"
            status="ok"
        else
            status="error"
        fi
        
        # Send acknowledgment
        ack_payload=$(jq -n --arg id "$id" --arg status "$status" '{id: $id, status: $status}')
        echo "$ack_payload" | supctl do volga topics acme:events produce -
        
        echo "Sent acknowledgment: $status (ID: $id)"
    fi
done
```

A complete shell script implementation (`acme_callback.sh`) is provided in the project repository with:
- Cloudflare API integration
- Domain filtering support  
- Error handling and logging
- Manual mode for testing

## Multi-Instance DNS Callback Setup

For large deployments with multiple domains, deploy domain-specific callback instances:

### Step 1: Create Domain-Specific Callback Instances

```bash
# Instance for primary domain
supctl create applications <<EOF
name: acme-callback-primary
version: "1.0"
services:
  - name: acme-callback
    variables:
      - name: CF_API_TOKEN
        value-from-vault-secret:
          vault: acme-secrets
          secret: cloudflare-credentials
          key: api-token
    containers:
      - name: acme-callback
        image: avassa/acme-cloudflare-callback:1.0
        approle: acme-callback
        env:
          CF_API_TOKEN: ${CF_API_TOKEN}
          VOLGA_ROLE_ID: "acme-role-id"
          APPROLE_SECRET_ID: ${SYS_APPROLE_SECRET_ID}
          API_CA_CERT: ${SYS_API_CA_CERT}
          MANAGED_DOMAINS: "yourcompany.com"
          ACME_DEBUG_DNS_VERIFICATION: "true"
        restart-policy: always
EOF

# Instance for subsidiary domains  
supctl create applications <<EOF
name: acme-callback-subsidiary
version: "1.0"
services:
  - name: acme-callback
    variables:
      - name: CF_API_TOKEN
        value-from-vault-secret:
          vault: acme-secrets
          secret: cloudflare-credentials
          key: api-token
    containers:
      - name: acme-callback
        image: avassa/acme-cloudflare-callback:1.0
        approle: acme-callback
        env:
          CF_API_TOKEN: ${CF_API_TOKEN}
          VOLGA_ROLE_ID: "acme-role-id"
          APPROLE_SECRET_ID: ${SYS_APPROLE_SECRET_ID}
          API_CA_CERT: ${SYS_API_CA_CERT}
          MANAGED_DOMAINS: "subsidiary.com,partner.org"
          ACME_DEBUG_DNS_VERIFICATION: "false"
        restart-policy: always
EOF
```

### Step 2: Deploy Instances to Appropriate Sites

```bash
# Deploy primary domain handler to Control Tower
supctl create application-deployments <<EOF
name: callback-primary-deployment
application: acme-callback-primary
application-version: "1.0"
placement: |
  system/type = control-tower
EOF

# Deploy subsidiary handler to specific region
supctl create application-deployments <<EOF
name: callback-subsidiary-deployment
application: acme-callback-subsidiary
application-version: "1.0"  
placement: |
  region = us-west
EOF
```

## Certificate Request Examples

### Centralized Request with Static Request

```bash
# Create distributed vault for certificates
supctl create strongbox vaults <<EOF
name: certs
distribute:
  to: all
EOF

# Configure static request for automatic provisioning and distribution
supctl create strongbox acme-services letsencrypt-prod static-requests <<EOF
names: app.yourcompany.com,api.yourcompany.com
vault: certs
secret: app-certificates
EOF

# Check certificate status
supctl show strongbox acme-services letsencrypt-prod

# The certificates will be automatically provisioned and stored in the distributed vault
# Available at all sites as: certs/app-certificates/{acme-cert.pem,acme-cert.key,acme-chain.pem}
```

### One-Time Request vs Static Request

**Static Request** (Automatic, Persistent):
```bash
# Sets up automatic certificate provisioning
supctl create strongbox acme-services letsencrypt-prod static-requests <<EOF
names: app.yourcompany.com
vault: certs  
secret: app-cert
EOF
# Certificate is automatically requested, renewed, and distributed
```

**One-Time Request** (Manual, Temporary):
```bash
# Manually request certificate once
supctl do strongbox acme-services letsencrypt-prod request-cert \
  --names app.yourcompany.com

# Check status
supctl show strongbox acme-services letsencrypt-prod
# Certificate is stored in ACME service, not automatically distributed
```


### Auto-ACME Certificate (Automatic Provisioning)

Create a secret that automatically provisions certificates:

```bash
supctl create strongbox vaults <<EOF
name: auto-ssl
distribute:
  to: all
EOF

supctl create strongbox vaults auto-ssl secrets <<EOF
name: web-cert
auto-acme-cert:
  acme-service: letsencrypt-prod
  names:
    - web.yourcompany.com
    - ${SYS_SERVICE}.${SYS_APP}.${SYS_TENANT}.${SYS_SITE}.${SYS_GLOBAL_DOMAIN}
EOF
```

## Monitoring and Troubleshooting

### Monitor ACME Operations

```bash
# Check ACME service status and static requests
supctl show strongbox acme-services letsencrypt-prod

# Check specific static request status
supctl show strongbox acme-services letsencrypt-prod static-requests

# Monitor certificate provisioning logs
supctl logs strongbox acme-services letsencrypt-prod

# Check distributed certificate vault contents
supctl show strongbox vaults certs secrets web-services

# Verify certificate distribution to edge sites
supctl show --site edge-site-01 strongbox vaults certs secrets web-services

# Check callback service health (if using DNS callback)
supctl logs application acme-cloudflare-callback --service acme-callback
```

### Monitor Volga Message Flow

```bash
# Monitor challenge requests
supctl do volga topics acme:requests consume --payload-only --follow

# Monitor challenge responses  
supctl do volga topics acme:events consume --payload-only --follow
```

### Debug DNS Issues

If using the Cloudflare callback service with debug mode enabled:

```bash
# Check callback service logs for DNS verification
supctl logs application acme-cloudflare-callback --service acme-callback

# Look for patterns like:
# ✅ ACME challenge record created successfully
# 🔍 Running DNS verification check (debug mode)
# ⚠️ DNS verification found potential issues
```

### Common Issues and Solutions

**Challenge Timeout**:
- Check DNS propagation with `dig _acme-challenge.yourdomain.com TXT`
- Verify callback service is running and processing messages
- Check Volga topic permissions

**DNS Resolution Issues**:
- Enable debug mode in callback service (`ACME_DEBUG_DNS_VERIFICATION: "true"`)
- Check if DNS records are created but not propagated
- Verify DNS provider API credentials

**Authentication Failures**:
- Check AppRole configuration and token policies
- Verify callback service has proper Volga topic access
- Check strongbox vault permissions

## Certificate Renewal

ACME certificates are automatically renewed by Avassa:

- **Auto-renewal**: Certificates are renewed automatically before expiration
- **Distribution**: Renewed certificates are automatically distributed to edge sites
- **Application restart**: Applications using the certificates are restarted when certificates are updated

## Best Practices

1. **Use staging environment first** - Always test with Let's Encrypt staging before production
2. **Monitor certificate expiration** - Set up alerts for certificate renewal failures
3. **Use domain-specific callbacks** - Deploy separate callback instances for different domains to improve reliability
4. **Enable debug logging** - Use debug mode during initial setup to diagnose DNS issues
5. **Test certificate distribution** - Verify certificates reach all edge sites correctly
6. **Plan for failure scenarios** - Have backup procedures for manual certificate deployment

## Security Considerations

- **API Token Security**: Store DNS provider API tokens in Avassa Strongbox, never in plain text
- **Certificate Distribution**: Use Avassa's encrypted strongbox distribution for certificate secrets
- **Access Control**: Configure proper policies to limit which applications can access certificates
- **Network Security**: Restrict callback service network access to only necessary endpoints

## Complete Working Example

Here's a complete example based on the provided test case, showing how to set up ACME certificate provisioning with Cloudflare DNS callback:

### 1. Deploy Pebble ACME Server
```bash
# Create and deploy Pebble for testing
supctl create applications <<EOF
name: pebble
version: "1.0"
services:
  - name: pebble
    mode: replicated
    replicas: 1
    network:
      ingress-ip-per-instance:
        protocols:
          - name: tcp
            port-ranges: "14000,15000"
        access:
          allow-all: true
      outbound-access:
        allow-all: true
    containers:
      - name: pebble
        image: ghcr.io/letsencrypt/pebble
        cmd: [ "-config", "/test/config/pebble-config.json", "-strict" ]
        env:
          PEBBLE_VA_NOSLEEP: "0"
          PEBBLE_VA_ALWAYS_VALID: "0"
EOF

supctl create application-deployments <<EOF
name: pebble
application: pebble  
application-version: "1.0"
placement: |
  system/name = topdc
sites-in-parallel: 1
EOF
```

### 2. Configure Secrets and Callback Service
```bash
# Create secrets vault
supctl create strongbox vaults <<EOF
name: acme-secrets
EOF

supctl create strongbox vaults acme-secrets secrets <<EOF
name: cloudflare-credentials
allow-image-access: [ "*" ]
data:
  api-token: "your_cloudflare_api_token"
EOF

# Deploy Cloudflare callback service
supctl create applications <<EOF
name: cloudflare
version: "1.0"
services:
  - name: cloudflare
    mode: replicated
    replicas: 1
    variables:
      - name: CF_API_TOKEN
        value-from-vault-secret:
          vault: acme-secrets
          secret: cloudflare-credentials
          key: api-token
    network:
      outbound-access:
        allow-all: true
    containers:
      - name: cloudflare
        image: avassa/acme-cloudflare-callback:1.0
        approle: acme-callback
        env:
          CF_API_TOKEN: ${CF_API_TOKEN}
          VOLGA_ROLE_ID: "acme-role-id"
          APPROLE_SECRET_ID: ${SYS_APPROLE_SECRET_ID}
          API_CA_CERT: ${SYS_API_CA_CERT}
          MANAGED_DOMAINS: "valudden17.com"
          ACME_DEBUG_DNS_VERIFICATION: "true"
EOF
```

### 3. Configure ACME Service with Callback
```bash
# Configure ACME service with callback domains
supctl create strongbox acme-services <<EOF
name: pebble
contact-email: admin@example.com
directory-url: https://pebble.pebble.telco.topdc.site.test:14000/dir
server-name-indication: pebble.pebble.telco.topdc.site.test
tls-verify: false
callback-domains:
  - valudden17.com
EOF
```

### 4. Set Up Automatic Certificate Distribution
```bash
# Create distributed vault
supctl create strongbox vaults <<EOF
name: certs
distribute:
  to: all
EOF

# Configure static request for automatic provisioning
supctl create strongbox acme-services pebble static-requests <<EOF
names: foo.valudden17.com
vault: certs
secret: kafka
EOF
```

### 5. Verify Certificate Provisioning
```bash
# Check that certificate was provisioned
supctl show strongbox vaults certs secrets kafka

# Should show:
# acme-cert.pem: |
#   -----BEGIN CERTIFICATE-----
#   ...
#   -----END CERTIFICATE-----
# acme-cert.key: |
#   -----BEGIN PRIVATE KEY-----
#   ...
#   -----END PRIVATE KEY-----
# acme-chain.pem: |
#   -----BEGIN CERTIFICATE-----
#   ...
#   -----END CERTIFICATE-----
```

## Summary

Avassa's ACME integration provides flexible certificate provisioning suitable for both centralized and distributed deployments:

- **Static Requests**: Enable fully automated certificate provisioning and distribution (Control Tower only)
- **Distributed Vaults**: Automatically distribute certificates to all edge sites
- **DNS Flexibility**: Support both DNS delegation and external DNS callbacks
- **Multi-Instance Support**: Domain-specific callback services for horizontal scaling

**Important**: Static requests are only processed at the Control Tower. For edge site certificates, the recommended approaches are:
1. **Auto-ACME secrets** - Certificates automatically provisioned when applications start
2. **Static requests from Control Tower** - With targeted vault distribution to specific edge sites

The callback-based approach using services like the Cloudflare ACME callback enables organizations to leverage existing DNS infrastructure while gaining the benefits of Avassa's distributed certificate management and automatic renewal.
