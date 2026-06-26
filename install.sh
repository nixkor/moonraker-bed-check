#!/usr/bin/env bash
#
# install.sh — install/uninstall the bed_check Moonraker component.
#
# Assumes this repo was `git clone`d to a folder under your home dir on the Pi.
# It:
#   - symlinks components/bed_check.py into Moonraker's components dir
#     (so `git pull` updates the live component),
#   - symlinks config/bed_check.cfg into your Klipper config dir
#     (so `git pull` updates the macro too),
#   - appends a default [bed_check] section to moonraker.conf,
#   - adds `[include bed_check.cfg]` to printer.cfg.
#
# Usage:
#   ./install.sh              # install
#   ./install.sh --uninstall  # undo (the [bed_check] section is commented
#                             #   out and PRESERVED, not deleted)
#
# Override paths if your setup differs:
#   MOONRAKER_DIR=~/moonraker CONFIG_DIR=~/printer_data/config ./install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- paths (override via env) ------------------------------------------------
MOONRAKER_DIR="${MOONRAKER_DIR:-$HOME/moonraker}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
API_KEY_FILE="${API_KEY_FILE:-$HOME/.anthropic_api_key}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$(dirname "$CONFIG_DIR")/bed_check_snapshots}"

COMPONENTS_DIR="$MOONRAKER_DIR/moonraker/components"
MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"
PRINTER_CFG="$CONFIG_DIR/printer.cfg"

COMPONENT_SRC="$SCRIPT_DIR/components/bed_check.py"
CFG_SRC="$SCRIPT_DIR/config/bed_check.cfg"
COMPONENT_LINK="$COMPONENTS_DIR/bed_check.py"
CFG_LINK="$CONFIG_DIR/bed_check.cfg"

MARK_BEGIN="# >>> bed_check >>>"
MARK_END="# <<< bed_check <<<"
INCLUDE_LINE="[include bed_check.cfg]"

# --- output helpers ----------------------------------------------------------
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

backup() {
    # Timestamped backup so an edit is never silently lost.
    local f="$1"
    [ -f "$f" ] || return 0
    local b="${f}.bedcheck.$(date +%Y%m%d_%H%M%S).bak"
    cp "$f" "$b"
    info "backed up $(basename "$f") -> $(basename "$b")"
}

add_printer_include() {
    # Insert after the last existing [include ...], else at the very top.
    # NEVER append to the end: Klipper's auto-generated "#*# SAVE_CONFIG" block
    # lives at the bottom and anything placed after it corrupts saved state.
    local file="$1" line="$2" lastinc
    # `|| true`: no [include] lines is normal, but the failing grep would abort
    # the script under `set -euo pipefail`.
    lastinc="$(grep -nE '^\[include ' "$file" | tail -1 | cut -d: -f1 || true)"
    if [ -n "$lastinc" ]; then
        sed -i "${lastinc}a ${line}" "$file"
    else
        # No existing includes — prepend at the very top (portable, no sed quirks).
        printf '%s\n' "$line" | cat - "$file" > "$file.tmp" && mv "$file.tmp" "$file"
    fi
}

bedcheck_conf_block() {
cat <<EOF
$MARK_BEGIN
# Added by bed_check install.sh — defaults; edit as needed (see README).
[bed_check]
# Keep the API key OUT of this file. Put it in the path below (chmod 600), or
# set ANTHROPIC_API_KEY in the moonraker service environment.
anthropic_api_key_path: $API_KEY_FILE
# Opus 4.8 = most reliable default. claude-sonnet-4-6 is cheaper and usually fine
# on a well-lit, upright camera; switch if cost matters more than max accuracy.
model: claude-opus-4-8
max_tokens: 1024
# request_timeout MUST be < the CHECK_BED dwell WINDOW (default 25s):
#   request_timeout (20) < WINDOW (25) < delayed_gcode dead-man (60)
request_timeout: 20
enabled_default: True
fail_open: False
orient_snapshot: True
# Optional: extra system-prompt text appended as site-specific context (e.g.
# pointing out a permanent fixture in the camera view). It refines the prompt but
# cannot override the safety rules or the JSON schema, and is echoed back in
# /server/bed_check/status. For multiple lines, indent the continuation lines.
# extra_system_prompt: The grey clip at the back-left corner is a permanent cable
#   guide, not a part left on the bed.
snapshot_save_dir: $SNAPSHOT_DIR
snapshot_keep: 5
$MARK_END
EOF
}

require_dirs() {
    [ -d "$COMPONENTS_DIR" ] || {
        c_red "Moonraker components dir not found: $COMPONENTS_DIR"
        c_red "Set MOONRAKER_DIR=/path/to/moonraker and retry."
        exit 1
    }
    [ -d "$CONFIG_DIR" ] || {
        c_red "Config dir not found: $CONFIG_DIR"
        c_red "Set CONFIG_DIR=/path/to/printer_data/config and retry."
        exit 1
    }
}

# --- install -----------------------------------------------------------------
do_install() {
    require_dirs
    [ -f "$COMPONENT_SRC" ] || { c_red "missing $COMPONENT_SRC"; exit 1; }
    [ -f "$CFG_SRC" ]       || { c_red "missing $CFG_SRC"; exit 1; }

    c_grn "Installing bed_check..."

    # 1. component symlink (git pull updates the live component)
    ln -sfn "$COMPONENT_SRC" "$COMPONENT_LINK"
    info "symlinked component -> $COMPONENT_LINK"

    # 2. symlink the Klipper macro (git pull updates the live macro). Klipper
    #    resolves the symlink when it [include]s bed_check.cfg.
    ln -sfn "$CFG_SRC" "$CFG_LINK"
    info "symlinked bed_check.cfg -> $CFG_LINK"

    # 3. moonraker.conf [bed_check] section
    if [ -f "$MOONRAKER_CONF" ]; then
        if grep -qF "$MARK_BEGIN" "$MOONRAKER_CONF"; then
            info "moonraker.conf already has the bed_check block — leaving it."
        elif grep -qE '^\[bed_check\]' "$MOONRAKER_CONF"; then
            c_ylw "  moonraker.conf already has a [bed_check] section not managed by"
            c_ylw "  this script — leaving it untouched."
        else
            backup "$MOONRAKER_CONF"
            printf '\n%s\n' "$(bedcheck_conf_block)" >> "$MOONRAKER_CONF"
            info "added [bed_check] section to moonraker.conf"
        fi
    else
        c_ylw "  moonraker.conf not found at $MOONRAKER_CONF — add [bed_check] by hand."
    fi

    # 4. printer.cfg include
    if [ -f "$PRINTER_CFG" ]; then
        if grep -qE "^\[include bed_check\.cfg\]" "$PRINTER_CFG"; then
            info "printer.cfg already includes bed_check.cfg."
        else
            backup "$PRINTER_CFG"
            add_printer_include "$PRINTER_CFG" "$INCLUDE_LINE"
            info "added '$INCLUDE_LINE' to printer.cfg"
        fi
    else
        c_ylw "  printer.cfg not found at $PRINTER_CFG — add '$INCLUDE_LINE' by hand."
    fi

    c_grn "Done."
    echo
    c_ylw "Next steps:"
    info "1. API key:  echo 'sk-ant-...' > $API_KEY_FILE && chmod 600 $API_KEY_FILE"
    info "2. Add 'CHECK_BED' to the top of your slicer start gcode / PRINT_START"
    info "   (before the first homing move). See config/bed_check.cfg."
    info "3. sudo systemctl restart moonraker"
    info "4. FIRMWARE_RESTART   (in the Klipper console)"
    info "bed_check.py and bed_check.cfg are symlinked, so 'git pull' updates both"
    info "in place (FIRMWARE_RESTART after a macro change to reload it)."
}

# --- uninstall ---------------------------------------------------------------
do_uninstall() {
    c_grn "Uninstalling bed_check..."

    # 1. component symlink (only remove if it IS our symlink)
    if [ -L "$COMPONENT_LINK" ]; then
        rm -f "$COMPONENT_LINK"; info "removed component symlink"
    elif [ -e "$COMPONENT_LINK" ]; then
        c_ylw "  $COMPONENT_LINK is a real file, not our symlink — leaving it."
    fi

    # 2. macro symlink (only remove if it IS our symlink)
    if [ -L "$CFG_LINK" ]; then
        rm -f "$CFG_LINK"; info "removed bed_check.cfg symlink"
    elif [ -e "$CFG_LINK" ]; then
        c_ylw "  $CFG_LINK is a real file, not our symlink — leaving it."
    fi

    # 3. moonraker.conf: comment out the [bed_check] block, PRESERVE it
    local preserved=0
    if [ -f "$MOONRAKER_CONF" ] && grep -qF "$MARK_BEGIN" "$MOONRAKER_CONF"; then
        backup "$MOONRAKER_CONF"
        local stamp; stamp="$(date +%Y-%m-%d)"
        awk -v b="$MARK_BEGIN" -v e="$MARK_END" -v stamp="$stamp" '
            $0==b { print "# >>> bed_check (UNINSTALLED " stamp " — preserved; delete this block to remove) >>>"; inblk=1; next }
            $0==e { print $0; inblk=0; next }
            inblk { if ($0 ~ /^[[:space:]]*#/) print $0; else print "# " $0; next }
            { print }
        ' "$MOONRAKER_CONF" > "${MOONRAKER_CONF}.tmp" && mv "${MOONRAKER_CONF}.tmp" "$MOONRAKER_CONF"
        info "commented out [bed_check] in moonraker.conf (preserved)"
        preserved=1
    fi

    # 4. printer.cfg include
    if [ -f "$PRINTER_CFG" ] && grep -qE "^\[include bed_check\.cfg\]" "$PRINTER_CFG"; then
        backup "$PRINTER_CFG"
        sed -i '/^\[include bed_check\.cfg\]/d' "$PRINTER_CFG"
        info "removed include from printer.cfg"
    fi

    c_grn "Done."
    echo
    if [ "$preserved" -eq 1 ]; then
        c_ylw "NOTE: your [bed_check] settings were NOT deleted from moonraker.conf."
        c_ylw "They are commented out and preserved under the line:"
        c_ylw "    # >>> bed_check (UNINSTALLED ... — preserved; delete this block to remove) >>>"
        c_ylw "Delete that block by hand if you want the settings gone for good."
    fi
    info "Restart Moonraker and FIRMWARE_RESTART Klipper to apply."
}

# --- dispatch ----------------------------------------------------------------
case "${1:-}" in
    ""|--install|install)   do_install ;;
    --uninstall|uninstall)  do_uninstall ;;
    -h|--help)              usage 0 ;;
    *) c_red "unknown argument: $1"; usage 1 ;;
esac
