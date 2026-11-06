#!/usr/bin/env bash
# Free the add-in port before a new MCP session.
#
# sshd here does not notice a client that vanished (no ClientAliveInterval),
# so the previous session's reverse tunnel keeps the port and the new
# `ssh -R ... ExitOnForwardFailure=yes` fails.  fuser cannot see sshd's
# sockets, so the stale session is found through its MCP server: every
# fusion360-mcp-server of this user is stopped together with the sshd
# session that started it.  One session at a time is the supported setup.
set -u
port="${1:-9876}"

session_of() {  # nearest sshd ancestor of a pid
    local pid="$1"
    while [ "$pid" -gt 1 ]; do
        case "$(ps -o comm= -p "$pid")" in sshd*) echo "$pid"; return ;; esac
        pid="$(ps -o ppid= -p "$pid" | tr -d ' ')"
    done
}

own="$(session_of $$)"
for server in $(pgrep -u "$(id -u)" -f 'bin/fusion360-mcp-server'); do
    session="$(session_of "$server")"
    [ -n "$own" ] && [ "$session" = "$own" ] && continue
    kill "$server" ${session:+"$session"} 2>/dev/null
done

for _ in $(seq 25); do
    ss -Hltn "sport = :${port}" | grep -q . || exit 0
    sleep 0.2
done
echo "port ${port} is still held (not by an MCP session)" >&2
exit 1
