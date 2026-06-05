"""Bundled shell hook scripts for pip-installed environments.

When dev-recall is installed via pip, the shell/ directory isn't available.
This module embeds the hook scripts as strings so `recall init` can
write them to ~/.config/dev-recall/.
"""

ZSH_HOOK = r'''# Recall shell hook — source this in ~/.zshrc
# Installed by: recall init

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/dev-recall}" {
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
'''

BASH_HOOK = r'''# Recall shell hook — source this in ~/.bashrc
# Installed by: recall init

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/dev-recall}"
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
'''

GIT_POST_COMMIT = r'''#!/bin/sh
# Recall git post-commit hook
# Installed globally via: git config --global core.hooksPath ~/.config/dev-recall/git-hooks/

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/devmem}"
REPO_PATH=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
HASH=$(git rev-parse HEAD 2>/dev/null) || exit 0
MSG=$(git log -1 --format="%s" 2>/dev/null)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
FILES=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null | tr '\n' '|' | sed 's/|$//')
AUTHOR=$(git log -1 --format="%an" 2>/dev/null)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

printf '%s\tcommit\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$TS" "$REPO_PATH" "$HASH" "$BRANCH" "$MSG" "$FILES" "$AUTHOR" \
    >> "$__devrecall_dir/git.tsv" 2>/dev/null

exit 0
'''

GIT_POST_CHECKOUT = r'''#!/bin/sh
# Recall git post-checkout hook
# $1=prev ref, $2=new ref, $3=flag (1=branch, 0=file checkout)

# Only record branch switches, not file checkouts
[ "$3" = "1" ] || exit 0

__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/devmem}"
REPO_PATH=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
NEW_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
OLD_BRANCH=$(git name-rev --name-only "$1" 2>/dev/null || echo "unknown")
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

printf '%s\tbranch\t%s\t%s\t%s\n' \
    "$TS" "$REPO_PATH" "$OLD_BRANCH" "$NEW_BRANCH" \
    >> "$__devrecall_dir/git.tsv" 2>/dev/null

exit 0
'''

GIT_PRE_PUSH = r'''#!/bin/sh
# Recall git pre-push hook
# Installed globally via: git config --global core.hooksPath ~/.config/dev-recall/git-hooks/

REMOTE="$1"
__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/devmem}"
REPO_PATH=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

COMMIT_COUNT=0
while IFS=' ' read -r local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
    if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        COUNT=$(git rev-list "$local_sha" --count 2>/dev/null || echo 0)
    else
        COUNT=$(git rev-list "${remote_sha}..${local_sha}" --count 2>/dev/null || echo 0)
    fi
    COMMIT_COUNT=$(( COMMIT_COUNT + COUNT ))
done

printf '%s\tpush\t%s\t%s\t%s\t%d\n' \
    "$TS" "$REPO_PATH" "$REMOTE" "$BRANCH" "$COMMIT_COUNT" \
    >> "$__devrecall_dir/git.tsv" 2>/dev/null

exit 0
'''

GIT_POST_MERGE = r'''#!/bin/sh
# Recall git post-merge hook
# $1=1 if squash merge, 0 otherwise

IS_SQUASH="${1:-0}"
__devrecall_dir="${DEV_RECALL_DATA_DIR:-$HOME/.local/share/devmem}"
REPO_PATH=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

MERGED_BRANCH=$(git reflog show --format="%gs" -1 HEAD 2>/dev/null \
    | sed -n "s/^merge //p" | head -1)

printf '%s\tmerge\t%s\t%s\t%s\t%s\n' \
    "$TS" "$REPO_PATH" "$BRANCH" "$MERGED_BRANCH" "$IS_SQUASH" \
    >> "$__devrecall_dir/git.tsv" 2>/dev/null

exit 0
'''

FISH_HOOK = r'''# Recall shell hook — source this in ~/.config/fish/config.fish
# Installed by: recall init

set -g __devrecall_dir (set -q DEV_RECALL_DATA_DIR; and echo $DEV_RECALL_DATA_DIR; or echo "$HOME/.local/share/devmem")
set -g __devrecall_shell_log "$__devrecall_dir/shell.tsv"
set -g __devrecall_cmd ""
set -g __devrecall_start_ms 0

function __devrecall_preexec --on-event fish_preexec
    set -g __devrecall_cmd $argv[1]
    set -g __devrecall_start_ms (math (date +%s) \* 1000)
end

function __devrecall_postexec --on-event fish_postexec
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
'''
