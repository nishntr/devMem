# Recall shell hook, source this in ~/.bashrc
# Installed by: recall init

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/dev-recall}"
__devrecall_shell_log="$__devrecall_dir/shell.tsv"
__devrecall_cmd=""
__devrecall_start_ms=0

# trap DEBUG fires before each command executes
__devrecall_debug_trap() {
    # BASH_COMMAND is set to the command string before it runs.
    # Skip the trap when it fires inside PROMPT_COMMAND itself.
    if [[ "$BASH_COMMAND" != "__devrecall_precmd"* ]]; then
        __devrecall_cmd="$BASH_COMMAND"
        __devrecall_start_ms=$(( $(date +%s) * 1000 ))
    fi
}
trap '__devrecall_debug_trap' DEBUG

__devrecall_precmd() {
    local exit_code=$?
    [[ -z "$__devrecall_cmd" ]] && return

    local end_ms
    end_ms=$(( $(date +%s) * 1000 ))
    local dur=$(( end_ms - __devrecall_start_ms ))
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Replace tabs with spaces to keep TSV well-formed
    local safe_cmd="${__devrecall_cmd//$'\t'/ }"
    local safe_cwd="${PWD//$'\t'/ }"

    printf '%s\t%s\t%s\t%d\t%d\n' \
        "$ts" "$safe_cwd" "$safe_cmd" "$exit_code" "$dur" \
        >> "$__devrecall_shell_log" 2>/dev/null

    __devrecall_cmd=""
}

# Append to PROMPT_COMMAND — preserve existing hooks
if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND="__devrecall_precmd"
elif [[ "$PROMPT_COMMAND" != *"__devrecall_precmd"* ]]; then
    PROMPT_COMMAND="${PROMPT_COMMAND};__devrecall_precmd"
fi
