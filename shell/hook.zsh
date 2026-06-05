# Recall shell hook, source this in ~/.zshrc
# Installed by: recall init

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/dev-recall}"
__devrecall_shell_log="$__devrecall_dir/shell.tsv"

__devrecall_preexec() {
    __devrecall_cmd="$1"
    __devrecall_start_ms=$(( $(date +%s) * 1000 ))
}

__devrecall_precmd() {
    local exit_code=$?
    [[ -z "$__devrecall_cmd" ]] && return

    local end_ms=$(( $(date +%s) * 1000 ))
    local dur=$(( end_ms - __devrecall_start_ms ))
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Tab-separated: timestamp TAB cwd TAB command TAB exit_code TAB duration_ms
    # Tabs inside cmd/cwd are replaced with space to avoid ambiguity
    local safe_cmd="${__devrecall_cmd//$'\t'/ }"
    local safe_cwd="${PWD//$'\t'/ }"

    printf '%s\t%s\t%s\t%d\t%d\n' \
        "$ts" "$safe_cwd" "$safe_cmd" "$exit_code" "$dur" \
        >> "$__devrecall_shell_log" 2>/dev/null

    unset __devrecall_cmd
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec __devrecall_preexec
add-zsh-hook precmd  __devrecall_precmd
