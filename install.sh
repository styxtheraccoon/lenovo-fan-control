#!/bin/bash
#
# Lenovo P330 Fan Control - Host Installation Script
# Run as root on the Proxmox host
#
# Usage:
#   ./install.sh              Install the service
#   ./install.sh --uninstall  Remove the service
#

set -euo pipefail

INSTALL_DIR="/opt/fan-control"
CONFIG_DIR="/etc/fan-control"
SERVICE_NAME="fan-control"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Check root
[[ $EUID -eq 0 ]] || error "This script must be run as root"

# --- Uninstall ---

files_match() {
    # Compare installed file against source. Returns 0 if identical.
    local installed="$1" source="$2"
    [[ -f "$installed" ]] && [[ -f "$source" ]] && \
        [[ "$(md5sum "$installed" | cut -d' ' -f1)" == "$(md5sum "$source" | cut -d' ' -f1)" ]]
}

do_uninstall() {
    info "Uninstalling ${SERVICE_NAME}..."

    # Stop and disable service
    if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
        info "Stopping ${SERVICE_NAME} service..."
        systemctl stop "${SERVICE_NAME}.service"
    fi
    if systemctl is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
        info "Disabling ${SERVICE_NAME} service..."
        systemctl disable "${SERVICE_NAME}.service"
    fi

    # Remove systemd unit file if unchanged
    local unit_file="/etc/systemd/system/${SERVICE_NAME}.service"
    if [[ -f "$unit_file" ]]; then
        if files_match "$unit_file" "${SCRIPT_DIR}/systemd/fan-control.service"; then
            rm -f "$unit_file"
            info "Removed ${unit_file}"
        else
            warn "Skipping ${unit_file} (modified by user)"
        fi
        systemctl daemon-reload
    fi

    # Remove installed program files if unchanged
    local -a install_files=(
        "fan_control_service.py:${SCRIPT_DIR}/host/fan_control_service.py"
        "api_server.py:${SCRIPT_DIR}/host/api_server.py"
    )
    for entry in "${install_files[@]}"; do
        local fname="${entry%%:*}"
        local source="${entry#*:}"
        local installed="${INSTALL_DIR}/${fname}"
        if [[ -f "$installed" ]]; then
            if files_match "$installed" "$source"; then
                rm -f "$installed"
                info "Removed ${installed}"
            else
                warn "Skipping ${installed} (modified by user)"
            fi
        fi
    done

    # Remove config files if unchanged
    local -a config_files=(
        "config.json:${SCRIPT_DIR}/host/config.json.example"
        "fan-control.env:${SCRIPT_DIR}/systemd/fan-control.env"
    )
    for entry in "${config_files[@]}"; do
        local fname="${entry%%:*}"
        local source="${entry#*:}"
        local installed="${CONFIG_DIR}/${fname}"
        if [[ -f "$installed" ]]; then
            if files_match "$installed" "$source"; then
                rm -f "$installed"
                info "Removed ${installed}"
            else
                warn "Skipping ${installed} (modified by user)"
            fi
        fi
    done

    # Remove directories if empty
    if [[ -d "$INSTALL_DIR" ]]; then
        if [[ -z "$(ls -A "$INSTALL_DIR")" ]]; then
            rmdir "$INSTALL_DIR"
            info "Removed ${INSTALL_DIR}"
        else
            warn "Skipping ${INSTALL_DIR} (not empty)"
            ls -la "$INSTALL_DIR"
        fi
    fi

    if [[ -d "$CONFIG_DIR" ]]; then
        if [[ -z "$(ls -A "$CONFIG_DIR")" ]]; then
            rmdir "$CONFIG_DIR"
            info "Removed ${CONFIG_DIR}"
        else
            warn "Skipping ${CONFIG_DIR} (not empty)"
            ls -la "$CONFIG_DIR"
        fi
    fi

    echo ""
    info "Uninstall complete."
    info "Note: Python packages (pyserial) and lm-sensors were not removed."
    exit 0
}

# Route to uninstall if requested
if [[ "${1:-}" == "--uninstall" ]]; then
    do_uninstall
fi

# --- Install ---

# Check dependencies
info "Checking dependencies..."
command -v python3 >/dev/null 2>&1 || error "python3 is not installed"
command -v sensors >/dev/null 2>&1 || {
    warn "lm-sensors not found - installing..."
    apt-get update && apt-get install -y lm-sensors
}

# Install Python dependencies
info "Installing Python dependencies..."
pip3 install --break-system-packages -r "${SCRIPT_DIR}/host/requirements.txt" 2>/dev/null || \
    pip3 install -r "${SCRIPT_DIR}/host/requirements.txt"

# Create directories
info "Creating directories..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${CONFIG_DIR}"

# Copy host files
info "Installing service files to ${INSTALL_DIR}..."
cp "${SCRIPT_DIR}/host/fan_control_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/host/api_server.py" "${INSTALL_DIR}/"

# Install config (don't overwrite existing)
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
    info "Installing default config to ${CONFIG_DIR}/config.json..."
    cp "${SCRIPT_DIR}/host/config.json.example" "${CONFIG_DIR}/config.json"
    warn "IMPORTANT: Edit ${CONFIG_DIR}/config.json and set your API key!"
else
    info "Config file already exists - skipping"
fi

# Install env file (don't overwrite existing)
if [[ ! -f "${CONFIG_DIR}/fan-control.env" ]]; then
    cp "${SCRIPT_DIR}/systemd/fan-control.env" "${CONFIG_DIR}/"
    warn "IMPORTANT: Edit ${CONFIG_DIR}/fan-control.env and set your API key!"
else
    info "Env file already exists - skipping"
fi

# Point config file path
export FAN_CONTROL_CONFIG="${CONFIG_DIR}/config.json"

# Install systemd service
info "Installing systemd service..."
cp "${SCRIPT_DIR}/systemd/fan-control.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

# Enable but don't start (user should configure first)
systemctl enable "${SERVICE_NAME}.service"
info "Service enabled (will start on boot)"

echo ""
info "============================================"
info "Installation complete!"
info "============================================"
echo ""
info "Next steps:"
echo "  1. Edit ${CONFIG_DIR}/config.json"
echo "     - Set 'api_key' to a secure value"
echo "     - Verify 'serial_port' matches your RP2040 (or leave as 'auto')"
echo ""
echo "  2. Flash the RP2040 firmware:"
echo "     - Install MicroPython on the RP2040 Zero"
echo "     - Copy firmware/*.py files to the device"
echo ""
echo "  3. Connect the RP2040 via USB and verify:"
echo "     ls -la /dev/ttyACM*"
echo ""
echo "  4. Start the service:"
echo "     systemctl start ${SERVICE_NAME}"
echo "     journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  5. Test the API:"
echo "     curl -H 'X-API-Key: YOUR_KEY' http://localhost:9780/api/status"
echo ""

