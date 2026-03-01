# Set Up ACME Certificate Provisioning for bevemyr.com, gt16.se and vininfo.org

This guide describes how to configure automatic certificate provisioning using Let's Encrypt
via the Avassa ACME integration with Cloudflare DNS callback (dns-01 challenge). It covers
the complete setup for the following DNS zones managed in Cloudflare:

- `bevemyr.com` — subdomains: `www`, `martin`, `nisse`, `jaktpass`, `lisa`, `cellar`
- `gt16.se` — subdomains: `oauth2`, `bastu`
- `vininfo.org` — subdomains: `www`

These domains were previously managed by certbot using http-01 (webroot) validation.
With the Avassa Cloudflare callback, webroot directories are no longer needed — challenges
are handled entirely through Cloudflare's DNS API.

## Prerequisites

- Avassa Control Tower deployed
- Domains `bevemyr.com`, `gt16.se` and `vininfo.org` managed in Cloudflare
- Cloudflare API token with `Zone DNS:Edit` permission for all three zones
- Docker image `ghcr.io/jbevemyr/avassa-acme-cloudflare:latest` accessible from your Avassa sites (published automatically from the `main` branch via GitHub Actions)

## Overview

The setup uses **centralized certificate provisioning**:

1. The Cloudflare callback service runs on the Control Tower, listening for ACME dns-01
   challenges on the `acme:requests` Volga topic and creating/removing TXT records via
   the Cloudflare API.
2. Avassa Strongbox requests certificates from Let's Encrypt using static requests.
3. Certificates are stored in a distributed vault and automatically pushed to all sites.
4. Applications consume the certificates from the vault — no manual renewal needed.

> **Important**: Static requests are only processed at the Control Tower. For edge site
> certificates, use auto-ACME secrets or consume secrets populated by Control Tower
> static requests. Manual `request-cert` commands are primarily for testing and debugging.

## Step 1: Configure ACME Service at Control Tower

Create ACME service configurations for Let's Encrypt. Always start with staging to
verify that the DNS callback is working before switching to production.

```bash
# Let's Encrypt Staging (for testing)
supctl create strongbox acme-services <<EOF
name: letsencrypt-staging
contact-email: admin@bevemyr.com
directory-url: https://acme-staging-v02.api.letsencrypt.org/directory
callback-domains:
  - bevemyr.com
  - gt16.se
  - vininfo.org
EOF

# Let's Encrypt Production
supctl create strongbox acme-services <<EOF
name: letsencrypt-prod
contact-email: admin@bevemyr.com
directory-url: https://acme-v02.api.letsencrypt.org/directory
callback-domains:
  - bevemyr.com
  - gt16.se
  - vininfo.org
EOF
```

## Step 2: Configure Secrets for Cloudflare

Store the Cloudflare API token in Avassa Strongbox:

```bash
# Create vault for ACME-related secrets
supctl create strongbox vaults <<EOF
name: acme-secrets
EOF

# Add Cloudflare API token (Zone DNS:Edit for bevemyr.com, gt16.se, vininfo.org)
supctl create strongbox vaults acme-secrets secrets <<EOF
name: cloudflare-credentials
allow-image-access: [ "*" ]
data:
  api-token: "YOUR_CLOUDFLARE_API_TOKEN"
EOF
```

## Step 3: Configure Callback Policy and AppRole

The callback service needs access to the Volga topics used for ACME challenge coordination:

```bash
# Create Volga topic policy
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

# Create AppRole for the callback service
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

## Step 4: Deploy the Cloudflare Callback Service

A single callback instance handles all three zones since they share the same Cloudflare
account and API token. `MANAGED_DOMAINS` is set to all three zones so the service
responds to challenges for any subdomain under them.

> **Note**: The callback service must run on an edge site, not on the Control Tower.
> It subscribes to the `acme:requests` and `acme:events` Volga topics at the parent
> (Control Tower) location using `Topic.parent()`, so ACME challenges issued by the
> Control Tower's Strongbox are delivered to — and acknowledged by — the service
> running on the edge site.

```bash
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
      - name: CF_API_BASE
        value: https://api.cloudflare.com/client/v4
      - name: CF_DEFAULT_TTL
        value: "120"
      - name: AVASSA_API_HOST
        value: https://api.internal:4646
      - name: MANAGED_DOMAINS
        value: "bevemyr.com,gt16.se,vininfo.org"
      - name: ACME_DEBUG_DNS_VERIFICATION
        value: "true"
    network:
      outbound-access:
        allow-all: true
    containers:
      - name: acme-callback
        image: ghcr.io/jbevemyr/avassa-acme-cloudflare:latest
        approle: acme-callback
        env:
          CF_API_TOKEN: ${CF_API_TOKEN}
          VOLGA_ROLE_ID: "acme-role-id"
          APPROLE_SECRET_ID: ${SYS_APPROLE_SECRET_ID}
          API_CA_CERT: ${SYS_API_CA_CERT}
          CF_API_BASE: ${CF_API_BASE}
          CF_DEFAULT_TTL: ${CF_DEFAULT_TTL}
          AVASSA_API_HOST: ${AVASSA_API_HOST}
          MANAGED_DOMAINS: ${MANAGED_DOMAINS}
          ACME_DEBUG_DNS_VERIFICATION: ${ACME_DEBUG_DNS_VERIFICATION}
EOF

# Deploy to the gt16 site
supctl create application-deployments <<EOF
name: acme-callback-deployment
application: acme-cloudflare-callback
application-version: "1.0"
placement: |
  system/name = gt16
sites-in-parallel: 1
EOF
```

## Step 5: Create Distributed Certificate Vault

Create a distributed vault that receives the certificates and pushes them to all sites:

```bash
supctl create strongbox vaults <<EOF
name: certs
distribute:
  to: all
EOF
```

## Step 6: Configure Static Certificate Requests

Static requests cause Avassa to automatically request, renew and distribute the
certificates. Start with staging to verify the full flow.

```bash
# Test with Let's Encrypt Staging first
supctl create strongbox acme-services letsencrypt-staging static-requests <<EOF
name: server-cert
names:
  - www.bevemyr.com
  - bevemyr.com,
  - martin.bevemyr.com
  - nisse.bevemyr.com
  - jaktpass.bevemyr.com
  - lisa.bevemyr.com
  - cellar.bevemyr.com
  - oauth2.gt16.se
  - bastu.gt16.se
  - www.vininfo.org
  - vininfo.org
vault: certs
secret: bevemyr-cert
EOF
```

Check that the certificate was provisioned successfully:

```bash
supctl show strongbox acme-services letsencrypt-staging
supctl show strongbox vaults certs secrets bevemyr-cert
```

Once staging works, create the equivalent production static request:

```bash
supctl create strongbox acme-services letsencrypt-prod static-requests <<EOF
name: server-cert
names:
  - www.bevemyr.com
  - bevemyr.com,
  - martin.bevemyr.com
  - nisse.bevemyr.com
  - jaktpass.bevemyr.com
  - lisa.bevemyr.com
  - cellar.bevemyr.com
  - oauth2.gt16.se
  - bastu.gt16.se,
  - www.vininfo.org
  - vininfo.org
vault: certs
secret: bevemyr-cert
EOF
```

Avassa will now automatically:
1. **Request the certificate** from Let's Encrypt for all eleven names
2. **Handle DNS challenges** by sending requests to the callback service via Volga
3. **Store the certificate** in the `certs` vault under the secret `bevemyr-cert`
4. **Distribute** to all sites immediately
5. **Renew automatically** before expiration

The secret contains three keys:

| Key | Content |
|---|---|
| `acme-cert.pem` | The certificate |
| `acme-cert.key` | The private key |
| `acme-chain.pem` | The certificate chain |

## Step 7: Use the Certificate in Applications

Applications reference the certificate via vault secrets:

```bash
supctl create applications <<EOF
name: my-web-app
version: "1.0"
services:
  - name: web
    variables:
      - name: TLS_CERT
        value-from-vault-secret:
          vault: certs
          secret: bevemyr-cert
          key: acme-cert.pem
      - name: TLS_KEY
        value-from-vault-secret:
          vault: certs
          secret: bevemyr-cert
          key: acme-cert.key
      - name: TLS_CHAIN
        value-from-vault-secret:
          vault: certs
          secret: bevemyr-cert
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
    mode: replicated
    replicas: 1
EOF
```

## DNS Challenge Flow

When Avassa needs to obtain or renew a certificate, the following sequence occurs:

**Challenge Add** (published to `acme:requests` topic):
```json
{
  "action": "add",
  "domain": "bevemyr.com",
  "ttl": 120,
  "value": "7J7pGj9O2Ye2OjaBhBRbGPlEUR7uqEBxRmvFv4B8maY",
  "name": "_acme-challenge.www.bevemyr.com",
  "id": "ee57cfef-66e1-4b71-8cf3-ba74599957a8"
}
```

**Callback Response** (published to `acme:events` topic):
```json
{
  "status": "ok",
  "id": "ee57cfef-66e1-4b71-8cf3-ba74599957a8"
}
```

**Challenge Remove** (after Let's Encrypt has validated):
```json
{
  "action": "remove",
  "domain": "bevemyr.com",
  "value": "7J7pGj9O2Ye2OjaBhBRbGPlEUR7uqEBxRmvFv4B8maY",
  "name": "_acme-challenge.www.bevemyr.com",
  "id": "fc80e166-fccd-4935-98ef-16184fb1b78c"
}
```

The callback service automatically discovers the correct Cloudflare zone by
successively stripping labels from the left side of the record name until a matching
zone is found (e.g. `_acme-challenge.cellar.bevemyr.com` → tries `cellar.bevemyr.com`
→ tries `bevemyr.com` ✓).

## Testing and Debugging

### Manual Certificate Request (Testing Only)

```bash
# One-time test request (does not auto-renew or distribute)
supctl do strongbox acme-services letsencrypt-staging request-cert \
  --names www.bevemyr.com

# Check result
supctl show strongbox acme-services letsencrypt-staging
```

> **Note**: Manual `request-cert` commands are for testing and debugging only.
> Production use should rely on static requests.

## Monitoring and Troubleshooting

### Monitor ACME Operations

```bash
# Check ACME service status and static request state
supctl show strongbox acme-services letsencrypt-prod

# Check static requests specifically
supctl show strongbox acme-services letsencrypt-prod static-requests

# Monitor ACME service logs
supctl logs strongbox acme-services letsencrypt-prod

# Check certificate contents in the vault
supctl show strongbox vaults certs secrets bevemyr-cert

# Verify the certificate has been distributed to a site
supctl show --site YOUR_SITE_NAME strongbox vaults certs secrets bevemyr-cert

# Check callback service health
supctl logs application acme-cloudflare-callback --service acme-callback
```

### Monitor the Volga Message Flow

```bash
# Watch challenge requests in real time
supctl do volga topics acme:requests consume --payload-only --follow

# Watch callback acknowledgements in real time
supctl do volga topics acme:events consume --payload-only --follow
```

### Debug DNS Issues

The callback service is deployed with `ACME_DEBUG_DNS_VERIFICATION=true`. After
creating each challenge record it probes the record from Cloudflare DNS (1.1.1.1),
Google DNS (8.8.8.8) and the system resolver and reports any propagation
inconsistencies. Look for these patterns in the callback service logs:

```
✅ ACME challenge record created successfully
🔍 Running DNS verification check (debug mode)
⚠️ DNS verification found potential issues
```

You can also verify challenge records directly:

```bash
dig _acme-challenge.www.bevemyr.com TXT @1.1.1.1
dig _acme-challenge.oauth2.gt16.se TXT @1.1.1.1
dig _acme-challenge.vininfo.org TXT @1.1.1.1
```

### Common Issues and Solutions

**Challenge Timeout**:
- Check that the TXT record was created in Cloudflare: `dig _acme-challenge.bevemyr.com TXT`
- Verify the callback service is running and consuming messages from the `acme:requests` topic
- Check Volga topic permissions on the `acme-callback` AppRole

**`zone_not_found` Error in Callback Logs**:
- Verify that the Cloudflare API token has `Zone DNS:Edit` permission for all three zones
- Confirm that `MANAGED_DOMAINS` contains `bevemyr.com,gt16.se,vininfo.org`

**Certificate Not Distributed to Edge Sites**:
- Verify that the `certs` vault has `distribute: to: all`
- Check that the edge site is online and connected to the Control Tower

**Authentication Failures**:
- Check AppRole configuration and token policies
- Verify the callback service has proper Volga topic access
- Check strongbox vault permissions

## Certificate Renewal

Once static requests are configured, Avassa handles the entire certificate lifecycle
automatically — no cron jobs required:

- **Auto-renewal**: Strongbox renews certificates before expiration
- **Distribution**: The renewed certificate is automatically pushed to all sites via
  the distributed vault
- **Application restart**: Applications consuming the certificate are restarted when
  it is updated

## Security Considerations

- **API Token**: The Cloudflare API token is stored in Avassa Strongbox and never
  appears in plain text in application definitions
- **Certificate Distribution**: Vault replication uses Avassa's encrypted strongbox
  distribution channel
- **Access Control**: The `allow-image-access` setting on the `cloudflare-credentials`
  secret limits which container images can read the token
- **Network Access**: The callback service has `outbound-access: allow-all` to reach
  `api.cloudflare.com`; tighten this to the Cloudflare API CIDR range if your security
  policy requires it
