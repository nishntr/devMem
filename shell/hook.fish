# Recall shell hook, source this in ~/.config/fish/config.fish
# Installed by: recall init

set -g __devrecall_dir (set -q DEV_RECALL_DATA_DIR; and echo $DEV_RECALL_DATA_DIR; or echo "$HOME/.local/share/dev-recall")
set -g __devrecall_shell_log "$__devrecall_dir/shell.tsv"
set -g __devrecall_cmd ""
set -g __devrecall_start_ms 0

function __devrecall_preexec --on-event fish_preexec
    set -g __devrecall_cmd $argv[1]
    set -g __devrecall_start_ms (math (date +%s) \* 1000)
end

function __devrecall_postexec --on-event fish_postexec
    # fish_postexec passes the command as $argv[1]; exit code is in $status
    # but $status here reflects the function's own invocation, not the last command.
    # fish 3.4+ passes exit_code as $argv[2]; use it when available.
    set cmd $argv[1]
    set exit_code 0
    if test (count $argv) -ge 2
        set exit_code $argv[2]
    end

    test -z "$__devrecall_cmd"; and return

    set end_ms (math (date +%s) \* 1000)
    set dur (math $end_ms - $__devrecall_start_ms)
    set ts (date -u +%Y-%m-%dT%H:%M:%SZ)

    set safe_cmd (string replace --all \t ' ' "$__devrecall_cmd")
    set safe_cwd (string replace --all \t ' ' "$PWD")

    printf '%s\t%s\t%s\t%d\t%d\n' \
        "$ts" "$safe_cwd" "$safe_cmd" "$exit_code" "$dur" \
        >> "$__devrecall_shell_log" 2>/dev/null

    set -g __devrecall_cmd ""
end
