# DevMem shell hook — source this in ~/.bashrc
# Installed by: devmem init

__devmem_dir="${DEVMEM_DATA_DIR:-$HOME/.local/share/devmem}"
__devmem_shell_log="$__devmem_dir/shell.tsv"
__devmem_cmd=""
__devmem_start_ms=0

# trap DEBUG fires before each command executes
__devmem_debug_trap() {
    # BASH_COMMAND is set to the command string before it runs.
    # Skip the trap when it fires inside PROMPT_COMMAND itself.
    if [[ "$BASH_COMMAND" != "__devmem_precmd"* ]]; then
        __devmem_cmd="$BASH_COMMAND"
        __devmem_start_ms=$(( $(date +%s) * 1000 ))
    fi
}
trap '__devmem_debug_trap' DEBUG

__devmem_precmd() {
    local exit_code=$?
    [[ -z "$__devmem_cmd" ]] && return

    local end_ms
    end_ms=$(( $(date +%s) * 1000 ))
    local dur=$(( end_ms - __devmem_start_ms ))
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Replace tabs with spaces to keep TSV well-formed
    local safe_cmd="${__devmem_cmd//$'\t'/ }"
    local safe_cwd="${PWD//$'\t'/ }"

    printf '%s\t%s\t%s\t%d\t%d\n' \
        "$ts" "$safe_cwd" "$safe_cmd" "$exit_code" "$dur" \
        >> "$__devmem_shell_log" 2>/dev/null

    __devmem_cmd=""
}

# Append to PROMPT_COMMAND — preserve existing hooks
if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND="__devmem_precmd"
elif [[ "$PROMPT_COMMAND" != *"__devmem_precmd"* ]]; then
    PROMPT_COMMAND="${PROMPT_COMMAND};__devmem_precmd"
fi
