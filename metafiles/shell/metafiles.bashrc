# MetaFiles terminal panel integration for bash (based on MetaTerminal's).
# Loaded via `bash --rcfile`; keeps the user's ~/.bashrc, adds project confinement and
# lets the file view change the shell's directory without touching a half-typed command.

if [ -f "$HOME/.bashrc" ]; then
    . "$HOME/.bashrc"
fi

# Report the working directory to the terminal (OSC 7) like gnome-terminal does.
if ! declare -F __vte_osc7 >/dev/null && [ -f /etc/profile.d/vte-2.91.sh ]; then
    . /etc/profile.d/vte-2.91.sh
fi

__mf_root="${METAFILES_ROOT:-}"

if [ -n "$__mf_root" ]; then
    # Checked on every cd and prompt, so toggling confinement in the app applies to
    # already running shells: the app creates/removes the flag file.
    __mf_enforced() {
        if [ -n "${METAFILES_CONFINE_FILE:-}" ]; then
            [ -e "$METAFILES_CONFINE_FILE" ]
        else
            [ "${METAFILES_CONFINE:-1}" = 1 ]
        fi
    }

    __mf_inside() {
        [ "$__mf_root" = / ] && return 0
        case "$1/" in
            "$__mf_root"/*) return 0 ;;
        esac
        return 1
    }

    cd() {
        if ! __mf_enforced; then
            builtin cd "$@"
            return
        fi
        local __mf_prev="$PWD" __mf_prev_old="$OLDPWD"
        if [ $# -eq 0 ]; then
            builtin cd -- "$__mf_root"
            return
        fi
        builtin cd "$@" || return
        if ! __mf_inside "$(builtin pwd -P)"; then
            builtin cd -- "$__mf_prev"
            OLDPWD="$__mf_prev_old"
            printf 'metafiles: cd outside the project (%s) is blocked\n' "$__mf_root" >&2
            return 1
        fi
    }

    __mf_guard() {
        __mf_enforced || return 0
        if ! __mf_inside "$(builtin pwd -P 2>/dev/null)"; then
            builtin cd -- "$__mf_root" 2>/dev/null
            printf 'metafiles: left the project, returned to %s\n' "$__mf_root" >&2
        fi
    }

    # Run first so the directory reported by OSC 7 is already corrected.
    PROMPT_COMMAND="__mf_guard${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
fi

# --- synchronization with the file view ---------------------------------------
# The app writes a directory (or a command) to $METAFILES_SYNC_FILE and sends one of the
# key sequences below, only while bash is in the foreground at the prompt. The macro saves the
# half-typed line, runs an empty accept-line (so the prompt is redrawn for the new directory)
# and then restores the saved line in the new prompt.
if [ -n "${METAFILES_SYNC_FILE:-}" ] && [[ $- == *i* ]]; then
    __mf_saved_line=""
    __mf_saved_point=0

    __mf_sync_cd() {
        __mf_saved_line=$READLINE_LINE
        __mf_saved_point=$READLINE_POINT
        READLINE_LINE=""
        READLINE_POINT=0
        local target
        target=$(<"$METAFILES_SYNC_FILE") || return
        if [ -d "$target" ] && [ "$PWD" != "$target" ]; then
            cd -- "$target"
        fi
    }

    __mf_sync_restore() {
        READLINE_LINE=$__mf_saved_line
        READLINE_POINT=$__mf_saved_point
        __mf_saved_line=""
        __mf_saved_point=0
    }

    __mf_load_command() {
        READLINE_LINE=$(<"$METAFILES_SYNC_FILE")
        READLINE_POINT=${#READLINE_LINE}
    }

    bind -x '"\e[24;3~": __mf_sync_cd'
    bind -x '"\e[24;4~": __mf_sync_restore'
    bind -x '"\e[24;5~": __mf_load_command'
    bind '"\e[24;6~": "\e[24;3~\C-m\e[24;4~"'
    bind '"\e[24;7~": "\e[24;5~\C-m"'
fi
