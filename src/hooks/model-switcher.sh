#!/bin/bash
# Model Switcher — auto-fallback and manual control for Claude models
# Tracks: Sonnet 4.6 (primary) <-> Haiku 4.5 (fallback)
# User-facing strings are Malay (owner reads them on WhatsApp). EN gloss on each.

STATE_DIR="${WA_STATE_DIR:-$HOME/.jessica}"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
STATE_FILE="$STATE_DIR/model-state"
umask 077
PRIMARY_MODEL="claude-sonnet-4-6"
FALLBACK_MODEL="claude-haiku-4-5-20251001"

# Initialize state file if missing
init_state() {
    if [[ ! -f "$STATE_FILE" ]]; then
        cat > "$STATE_FILE" << EOFSTATE
current_model=sonnet
primary_model=claude-sonnet-4-6
fallback_model=claude-haiku-4-5-20251001
fallback_active=no
reason=none
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
rate_limit_count=0
last_switch=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOFSTATE
    fi
}

# Read current state
read_state() {
    if [[ -f "$STATE_FILE" ]]; then
        # Parse known keys only. Never `source` a state file.
        local key val
        while IFS='=' read -r key val; do
            case "$key" in
                current_model|primary_model|fallback_model|fallback_active|reason|timestamp|rate_limit_count|last_switch)
                    printf -v "$key" '%s' "$val" ;;
            esac
        done < "$STATE_FILE"
        return 0
    fi
    return 1
}

# Write state
write_state() {
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    cat > "$STATE_FILE" << EOFWRITE
current_model=$current_model
primary_model=claude-sonnet-4-6
fallback_model=claude-haiku-4-5-20251001
fallback_active=$fallback_active
reason=$reason
timestamp=$ts
rate_limit_count=$rate_limit_count
last_switch=$ts
EOFWRITE
}

# Switch model
switch_model() {
    local target=$1
    local reason=${2:-"manual"}

    read_state

    case "$target" in
        sonnet|primary)
            current_model="sonnet"
            fallback_active="no"
            reason="$reason"
            write_state
            echo "✅ Tukar ke Sonnet 4.6 (primary)"  # EN: "Switched to Sonnet 4.6"
            return 0
            ;;
        haiku|fallback)
            current_model="haiku"
            fallback_active="yes"
            reason="$reason"
            ((rate_limit_count++))
            write_state
            echo "⚠️ Fallback aktif. Guna Haiku 4.5 sekarang."  # EN: "Fallback active. Using Haiku 4.5 now."
            return 0
            ;;
        *)
            echo "ERROR: Unknown model '$target'. Use 'sonnet' or 'haiku'."
            return 1
            ;;
    esac
}

# Check if rate-limited
check_rate_limit() {
    read_state

    # If already on fallback, assume limit is still active
    if [[ "$fallback_active" == "yes" ]]; then
        # Check if >60 mins have passed (rough recovery time)
        if command -v date &> /dev/null; then
            local last_epoch=$(date -d "$last_switch" +%s 2>/dev/null || echo 0)
            local now_epoch=$(date +%s)
            local elapsed=$((now_epoch - last_epoch))

            if [[ $elapsed -gt 3600 ]]; then
                switch_model "sonnet" "auto_recovery"
                echo "[RECOVERED] Sonnet limit cleared, reverted to primary"
                return 0
            fi
        fi
        return 1
    fi
    return 0
}

# Trigger fallback
trigger_fallback() {
    read_state

    if [[ "$fallback_active" == "no" ]]; then
        switch_model "haiku" "rate_limit"
        echo "[FALLBACK ACTIVE] Sonnet rate-limit detected. Using Haiku 4.5."
        return 0
    fi
    return 1
}

# Status report
status() {
    read_state

    local model_display="Sonnet 4.6"
    [[ "$current_model" == "haiku" ]] && model_display="Haiku 4.5"

    # EN: "tidak aktif" = inactive, "aktif" = active; status labels below: current model, fallback, reason, switch count, last switch
    local fallback_text="tidak aktif"
    [[ "$fallback_active" == "yes" ]] && fallback_text="aktif"

    cat << EOFSTATUS
Model semasa: $model_display
Fallback: $fallback_text
Sebab: $reason
Bilangan switch: $rate_limit_count
Tukar terakhir: $last_switch
EOFSTATUS
}

# Reset
reset() {
    read_state
    current_model="sonnet"
    fallback_active="no"
    reason="manual_reset"
    rate_limit_count=0
    write_state
    echo "✅ Dah reset. Balik ke Sonnet. Fallback counter kosong."  # EN: "Reset. Back to Sonnet. Fallback counter cleared."
}

# Main dispatcher
case "${1:-status}" in
    switch)
        init_state
        switch_model "$2" "${3:-manual}"
        ;;
    fallback)
        init_state
        trigger_fallback
        ;;
    check)
        init_state
        check_rate_limit
        ;;
    status)
        init_state
        status
        ;;
    reset)
        init_state
        reset
        ;;
    *)
        echo "Usage: $0 {switch|fallback|check|status|reset} [model] [reason]"
        exit 1
        ;;
esac
