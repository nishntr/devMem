# DevMem shell hook — source this in ~/.zshrc
# Installed by: devmem init

__devmem_dir="${DEVMEM_DATA_DIR:-$HOME/.local/share/devmem}"
__devmem_shell_log="$__devmem_dir/shell.tsv"

__devmem_preexec() {
    __devmem_cmd="$1"
    __devmem_start_ms=$(( $(date +%s) * 1000 ))
}

__devmem_precmd() {
    local exit_code=$?
    [[ -z "$__devmem_cmd" ]] && return

    local end_ms=$(( $(date +%s) * 1000 ))
    local dur=$(( end_ms - __devmem_start_ms ))
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Tab-separated: timestamp TAB cwd TAB command TAB exit_code TAB duration_ms
    # Tabs inside cmd/cwd are replaced with space to avoid ambiguity
    local safe_cmd="${__devmem_cmd//$'\t'/ }"
    local safe_cwd="${PWD//$'\t'/ }"

    printf '%s\t%s\t%s\t%d\t%d\n' \
        "$ts" "$safe_cwd" "$safe_cmd" "$exit_code" "$dur" \
        >> "$__devmem_shell_log" 2>/dev/null

    unset __devmem_cmd
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec __devmem_preexec
add-zsh-hook precmd  __devmem_precmd
