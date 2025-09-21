#!/bin/bash

# Simple shell script ACME DNS callback for Avassa Volga
# 
# This script listens to the acme:requests Volga topic, processes DNS challenge
# requests, and sends acknowledgments to the acme:events topic.
#
# Environment variables:
#   DNS_PROVIDER     - DNS provider type (cloudflare, route53, manual)
#   CF_API_TOKEN     - Cloudflare API token (if using cloudflare)
#   CF_API_BASE      - Cloudflare API base URL (default: https://api.cloudflare.com/client/v4)
#   MANAGED_DOMAINS  - Comma-separated domains this instance manages (optional)
#   DEBUG            - Enable debug logging (true/false)

set -euo pipefail

# Configuration
DNS_PROVIDER="${DNS_PROVIDER:-manual}"
CF_API_TOKEN="${CF_API_TOKEN:-}"
CF_API_BASE="${CF_API_BASE:-https://api.cloudflare.com/client/v4}"
MANAGED_DOMAINS="${MANAGED_DOMAINS:-}"
DEBUG="${DEBUG:-false}"

# Logging functions
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

debug() {
    if [[ "$DEBUG" == "true" ]]; then
        log "DEBUG: $*"
    fi
}

error() {
    log "ERROR: $*" >&2
}

# Check if domain should be handled by this instance
should_handle_domain() {
    local domain="$1"
    
    if [[ -z "$MANAGED_DOMAINS" ]]; then
        return 0  # Handle all domains
    fi
    
    # Convert to lowercase for comparison
    local domain_lower
    domain_lower=$(echo "$domain" | tr '[:upper:]' '[:lower:]')
    
    IFS=',' read -ra domains_array <<< "$MANAGED_DOMAINS"
    for managed_domain in "${domains_array[@]}"; do
        managed_domain=$(echo "$managed_domain" | tr '[:upper:]' '[:lower:]' | xargs)
        
        # Direct match
        if [[ "$domain_lower" == "$managed_domain" ]]; then
            return 0
        fi
        
        # Subdomain match (e.g., if we manage "example.com", handle "sub.example.com")
        if [[ "$domain_lower" == *".$managed_domain" ]]; then
            return 0
        fi
    done
    
    return 1
}

# Cloudflare DNS functions
cloudflare_find_zone() {
    local fqdn="$1"
    local zone_name=""
    local zone_id=""
    
    # Split FQDN into parts and try progressively shorter names
    local parts
    IFS='.' read -ra parts <<< "$fqdn"
    
    for ((i=0; i<${#parts[@]}-1; i++)); do
        local candidate
        candidate=$(printf ".%s" "${parts[@]:$i}" | cut -c2-)
        
        debug "Trying to find zone for: $candidate"
        
        local response
        response=$(curl -s -H "Authorization: Bearer $CF_API_TOKEN" \
                       -H "Content-Type: application/json" \
                       "$CF_API_BASE/zones?name=$candidate")
        
        local success
        success=$(echo "$response" | jq -r '.success // false')
        
        if [[ "$success" == "true" ]]; then
            local result_count
            result_count=$(echo "$response" | jq -r '.result | length')
            
            if [[ "$result_count" -gt 0 ]]; then
                zone_id=$(echo "$response" | jq -r '.result[0].id')
                zone_name=$(echo "$response" | jq -r '.result[0].name')
                echo "$zone_id,$zone_name"
                return 0
            fi
        fi
    done
    
    error "No Cloudflare zone found for $fqdn"
    return 1
}

cloudflare_add_txt() {
    local zone_id="$1"
    local name="$2"
    local value="$3"
    local ttl="${4:-120}"
    
    log "Creating TXT record: $name = '$value' (TTL: $ttl)"
    
    # Check for existing records first (idempotency)
    local existing_response
    existing_response=$(curl -s -H "Authorization: Bearer $CF_API_TOKEN" \
                           -H "Content-Type: application/json" \
                           "$CF_API_BASE/zones/$zone_id/dns_records?type=TXT&name=$name")
    
    local existing_records
    existing_records=$(echo "$existing_response" | jq -r '.result[]? | select(.content == "'"$value"'") | .id')
    
    if [[ -n "$existing_records" ]]; then
        local record_id
        record_id=$(echo "$existing_records" | head -n1)
        log "TXT record already exists with matching value, reusing ID: $record_id"
        echo "$record_id"
        return 0
    fi
    
    # Create new record
    local payload
    payload=$(jq -n \
        --arg type "TXT" \
        --arg name "$name" \
        --arg content "$value" \
        --argjson ttl "$ttl" \
        '{type: $type, name: $name, content: $content, ttl: $ttl}')
    
    local response
    response=$(curl -s -X POST \
                   -H "Authorization: Bearer $CF_API_TOKEN" \
                   -H "Content-Type: application/json" \
                   -d "$payload" \
                   "$CF_API_BASE/zones/$zone_id/dns_records")
    
    local success
    success=$(echo "$response" | jq -r '.success // false')
    
    if [[ "$success" == "true" ]]; then
        local record_id
        record_id=$(echo "$response" | jq -r '.result.id')
        log "Successfully created TXT record: $record_id"
        echo "$record_id"
        return 0
    else
        local errors
        errors=$(echo "$response" | jq -r '.errors[]? | "\(.code): \(.message)"' | tr '\n' '; ')
        error "Failed to create TXT record: $errors"
        return 1
    fi
}

cloudflare_remove_txt() {
    local zone_id="$1"
    local name="$2"
    local value="$3"
    
    log "Removing TXT record: $name with value '$value'"
    
    # Find records with matching content
    local existing_response
    existing_response=$(curl -s -H "Authorization: Bearer $CF_API_TOKEN" \
                           -H "Content-Type: application/json" \
                           "$CF_API_BASE/zones/$zone_id/dns_records?type=TXT&name=$name")
    
    local target_record
    target_record=$(echo "$existing_response" | jq -r '.result[]? | select(.content == "'"$value"'") | .id')
    
    if [[ -z "$target_record" ]]; then
        log "No TXT record found with matching value - considering removal successful (idempotent)"
        return 0
    fi
    
    # Delete the record
    local response
    response=$(curl -s -X DELETE \
                   -H "Authorization: Bearer $CF_API_TOKEN" \
                   "$CF_API_BASE/zones/$zone_id/dns_records/$target_record")
    
    local success
    success=$(echo "$response" | jq -r '.success // false')
    
    if [[ "$success" == "true" ]]; then
        log "Successfully removed TXT record: $target_record"
        return 0
    else
        local errors
        errors=$(echo "$response" | jq -r '.errors[]? | "\(.code): \(.message)"' | tr '\n' '; ')
        error "Failed to remove TXT record: $errors"
        return 1
    fi
}

# Manual DNS functions (for testing/debugging)
manual_add_txt() {
    local name="$1"
    local value="$2"
    local ttl="$3"
    
    log "MANUAL: Would add TXT record: $name = '$value' (TTL: $ttl)"
    log "Execute this command on your DNS server:"
    log "  dig $name TXT  # should return '$value'"
    return 0
}

manual_remove_txt() {
    local name="$1"  
    local value="$2"
    
    log "MANUAL: Would remove TXT record: $name with value '$value'"
    log "Execute this command on your DNS server:"
    log "  # Remove TXT record for $name with content '$value'"
    return 0
}

# Generic DNS functions
add_dns_record() {
    local zone_name="$1"
    local name="$2"
    local value="$3"
    local ttl="$4"
    
    case "$DNS_PROVIDER" in
        cloudflare)
            if [[ -z "$CF_API_TOKEN" ]]; then
                error "CF_API_TOKEN required for Cloudflare provider"
                return 1
            fi
            
            local zone_info
            zone_info=$(cloudflare_find_zone "$name")
            if [[ $? -ne 0 ]]; then
                return 1
            fi
            
            local zone_id
            zone_id=$(echo "$zone_info" | cut -d',' -f1)
            
            cloudflare_add_txt "$zone_id" "$name" "$value" "$ttl"
            ;;
        manual)
            manual_add_txt "$name" "$value" "$ttl"
            ;;
        *)
            error "Unsupported DNS provider: $DNS_PROVIDER"
            return 1
            ;;
    esac
}

remove_dns_record() {
    local zone_name="$1"
    local name="$2"
    local value="$3"
    
    case "$DNS_PROVIDER" in
        cloudflare)
            if [[ -z "$CF_API_TOKEN" ]]; then
                error "CF_API_TOKEN required for Cloudflare provider"
                return 1
            fi
            
            local zone_info
            zone_info=$(cloudflare_find_zone "$name")
            if [[ $? -ne 0 ]]; then
                return 1
            fi
            
            local zone_id
            zone_id=$(echo "$zone_info" | cut -d',' -f1)
            
            cloudflare_remove_txt "$zone_id" "$name" "$value"
            ;;
        manual)
            manual_remove_txt "$name" "$value"
            ;;
        *)
            error "Unsupported DNS provider: $DNS_PROVIDER"
            return 1
            ;;
    esac
}

# Volga message handling
send_ack() {
    local message_id="$1"
    local status="$2"
    local error_msg="${3:-}"
    
    local ack_payload
    if [[ "$status" == "ok" ]]; then
        ack_payload=$(jq -n --arg id "$message_id" --arg status "$status" \
                         '{id: $id, status: $status}')
    else
        ack_payload=$(jq -n --arg id "$message_id" --arg status "$status" --arg error "$error_msg" \
                         '{id: $id, status: $status, error: {type: "dns_provider", message: $error}}')
    fi
    
    debug "Sending ack: $ack_payload"
    
    # Send acknowledgment to acme:events topic
    echo "$ack_payload" | supctl do volga topics acme:events produce -
    
    if [[ $? -eq 0 ]]; then
        log "Sent acknowledgment: $status (ID: $message_id)"
    else
        error "Failed to send acknowledgment for message ID: $message_id"
    fi
}

process_message() {
    local payload="$1"
    
    # Parse JSON message
    local action domain name value ttl message_id
    action=$(echo "$payload" | jq -r '.action // ""')
    domain=$(echo "$payload" | jq -r '.domain // ""')
    name=$(echo "$payload" | jq -r '.name // ""')
    value=$(echo "$payload" | jq -r '.value // ""')
    ttl=$(echo "$payload" | jq -r '.ttl // 120')
    message_id=$(echo "$payload" | jq -r '.id // ""')
    
    debug "Received message: action=$action, domain=$domain, name=$name"
    
    # Validate message
    if [[ -z "$action" || -z "$name" || -z "$value" ]]; then
        error "Invalid message: missing required fields (action, name, value)"
        send_ack "$message_id" "error" "Missing required fields"
        return 1
    fi
    
    if [[ "$action" != "add" && "$action" != "remove" ]]; then
        error "Invalid action: $action (must be 'add' or 'remove')"
        send_ack "$message_id" "error" "Invalid action: $action"
        return 1
    fi
    
    # Check domain filtering
    if ! should_handle_domain "$domain"; then
        log "Skipping message for domain '$domain' - not managed by this instance"
        return 0  # Don't send ack, let other instances handle it
    fi
    
    log "Processing $action request for $name (domain: $domain)"
    
    # Process the DNS challenge
    local dns_result=0
    if [[ "$action" == "add" ]]; then
        add_dns_record "$domain" "$name" "$value" "$ttl"
        dns_result=$?
    elif [[ "$action" == "remove" ]]; then
        remove_dns_record "$domain" "$name" "$value"
        dns_result=$?
    fi
    
    # Send acknowledgment
    if [[ $dns_result -eq 0 ]]; then
        send_ack "$message_id" "ok"
        log "Successfully processed $action for $name"
    else
        send_ack "$message_id" "error" "DNS operation failed"
        error "Failed to process $action for $name"
    fi
}

# Main message loop
main() {
    log "Starting ACME DNS callback service"
    log "DNS Provider: $DNS_PROVIDER"
    
    if [[ -n "$MANAGED_DOMAINS" ]]; then
        log "Managed domains: $MANAGED_DOMAINS"
    else
        log "Managing ALL domains (no filtering)"
    fi
    
    # Verify prerequisites
    if ! command -v jq >/dev/null 2>&1; then
        error "jq is required but not installed"
        exit 1
    fi
    
    if ! command -v supctl >/dev/null 2>&1; then
        error "supctl is required but not installed"
        exit 1
    fi
    
    if [[ "$DNS_PROVIDER" == "cloudflare" ]]; then
        if [[ -z "$CF_API_TOKEN" ]]; then
            error "CF_API_TOKEN required for Cloudflare provider"
            exit 1
        fi
        if ! command -v curl >/dev/null 2>&1; then
            error "curl is required for Cloudflare provider"
            exit 1
        fi
    fi
    
    log "Starting to listen for ACME challenge requests..."
    
    # Main processing loop
    while true; do
        # Consume messages from acme:requests topic
        local message
        message=$(supctl do volga topics acme:requests consume --payload-only --timeout 30s 2>/dev/null || true)
        
        if [[ -n "$message" ]]; then
            # Process the message
            process_message "$message"
        else
            # No message received, continue waiting
            debug "No message received, continuing to wait..."
            sleep 1
        fi
    done
}

# Signal handling for graceful shutdown
cleanup() {
    log "Received shutdown signal, exiting..."
    exit 0
}

trap cleanup SIGINT SIGTERM

# Print usage if help requested
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat << EOF
ACME DNS Callback Service

Usage: $0 [OPTIONS]

Environment Variables:
  DNS_PROVIDER              DNS provider type (cloudflare, manual) [default: manual]
  CF_API_TOKEN              Cloudflare API token (required if DNS_PROVIDER=cloudflare)  
  CF_API_BASE               Cloudflare API base URL [default: https://api.cloudflare.com/client/v4]
  MANAGED_DOMAINS           Comma-separated domains to manage [default: all domains]
  DEBUG                     Enable debug logging (true/false) [default: false]

Examples:
  # Manual mode (logs what would be done)
  DNS_PROVIDER=manual $0
  
  # Cloudflare mode
  DNS_PROVIDER=cloudflare CF_API_TOKEN=your_token $0
  
  # Domain-specific instance
  DNS_PROVIDER=cloudflare CF_API_TOKEN=your_token MANAGED_DOMAINS=example.com,test.org $0

The service listens to the acme:requests Volga topic for JSON messages like:
{
  "action": "add",
  "domain": "example.com", 
  "name": "_acme-challenge.app.example.com",
  "value": "challenge_token",
  "ttl": 120,
  "id": "correlation_id"
}

And responds on the acme:events topic with:
{
  "id": "correlation_id",
  "status": "ok"
}

EOF
    exit 0
fi

# Run main function
main "$@"
