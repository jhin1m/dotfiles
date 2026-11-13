#!/usr/bin/python3
"""Turn herdr's flat Agents panel into a tree.

herdr's sidebar rows only accept tokens, so this watcher computes the tree
itself and reports two custom pane tokens for every agent:

  $tree_head  the space name, only on the first agent of each space
  $tree_line  "├ ◐ title" / "└ ○ title" (non-first agents get a 2-cell pad so
              they line up with the first agent, whose line herdr indents
              because it sits under the header row)

config.toml decides how they look ([ui.sidebar.agents] rows). The order must
match herdr's own "spaces" sort, so agent_panel_sort = "spaces" is required.

Usage: sidebar_tree.py start | stop | restart | once
Python 3.9 stdlib only (runs on macOS /usr/bin/python3).
"""
import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import sys
import time

SOURCE = "sidebar-tree"
# herdr trims leading whitespace from token values; braille blank survives.
PAD = "⠀⠀"
SPINNER = "◐◑◒◓"
ICONS = {"working": "◐", "blocked": "▲", "done": "✓", "idle": "○"}
POLL_SECONDS = 2.0  # titles change without events, so re-read this often
DEBOUNCE_SECONDS = 0.15
GIVE_UP_SECONDS = 60  # exit when the server has been gone this long
EVENTS = [
    "pane.created", "pane.closed", "pane.moved", "pane.exited", "pane.updated",
    "pane.agent_detected", "tab.created", "tab.closed", "tab.moved",
    "workspace.created", "workspace.closed", "workspace.renamed",
    "workspace.moved", "workspace.reordered", "layout.updated",
]


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def state_dir():
    # Same dir herdr passes to hooks, so a manual start shares the hooks' lock.
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr/plugins/sidebar-tree")
    os.makedirs(path, exist_ok=True)
    return path


def lock_path():
    # One watcher per server socket (named sessions have their own socket).
    key = hashlib.sha1(socket_path().encode()).hexdigest()[:10]
    return os.path.join(state_dir(), "watcher-%s.lock" % key)


def log(message):
    try:
        path = os.path.join(state_dir(), "watcher.log")
        if os.path.exists(path) and os.path.getsize(path) > 200_000:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def call(method, params=None, timeout=5.0):
    """One request per connection: send a JSON line, read the JSON reply line."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(socket_path())
        request = {"id": SOURCE, "method": method, "params": params or {}}
        conn.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    reply = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    if "error" in reply:
        raise RuntimeError("%s: %s" % (method, reply["error"]))
    return reply.get("result", {})


def subscribe():
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5.0)
    conn.connect(socket_path())
    request = {"id": SOURCE + "-events", "method": "events.subscribe",
               "params": {"subscriptions": [{"type": t} for t in EVENTS]}}
    conn.sendall((json.dumps(request) + "\n").encode())
    conn.setblocking(False)
    return conn


def icon(agent):
    status = agent.get("agent_status") or ""
    if status == "working":
        title = agent.get("terminal_title") or ""
        # Claude Code puts a spinner frame in its title; keep that frame.
        return title[0] if title[:1] and title[0] in SPINNER else ICONS["working"]
    return ICONS.get(status, "·")


def label(agent):
    name = agent.get("agent") or ""
    text = agent.get("terminal_title_stripped") or agent.get("name") or name or agent.get("pane_id", "")
    if name and name.lower() != "claude" and name.lower() not in text.lower():
        text = "%s · %s" % (name, text)
    return text


def ordered_agents(snap):
    """Space order, then tab order, then pane order inside the tab layout."""
    spaces = {w["workspace_id"]: (w.get("number", 0), i) for i, w in enumerate(snap.get("workspaces", []))}
    tabs = {t["tab_id"]: (t.get("number", 0), i) for i, t in enumerate(snap.get("tabs", []))}
    panes = {}
    for layout in snap.get("layouts", []):
        for i, pane in enumerate(layout.get("panes", [])):
            panes[pane["pane_id"]] = i
    far = (1 << 30, 0)
    return sorted(
        snap.get("agents", []),
        key=lambda a: (spaces.get(a.get("workspace_id"), far), tabs.get(a.get("tab_id"), far),
                       panes.get(a.get("pane_id"), 1 << 30), a.get("pane_id", "")),
    )


def compute(snap):
    names = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in snap.get("workspaces", [])}
    agents = ordered_agents(snap)
    tokens = {}
    i = 0
    while i < len(agents):
        space = agents[i].get("workspace_id")
        j = i
        while j < len(agents) and agents[j].get("workspace_id") == space:
            j += 1
        group = agents[i:j]
        for k, agent in enumerate(group):
            branch = "└" if k == len(group) - 1 else "├"
            head = names.get(space, space) if k == 0 else None
            pad = "" if k == 0 else PAD
            tokens[agent["pane_id"]] = (head, "%s%s %s %s" % (pad, branch, icon(agent), label(agent)))
        i = j
    return tokens


def report(pane_id, head, line):
    call("pane.report_metadata", {"pane_id": pane_id, "source": SOURCE,
                                  "tokens": {"tree_head": head, "tree_line": line}})


def sync(reported):
    """Push only what changed since the last sync; returns the new state."""
    current = compute(call("session.snapshot").get("snapshot", {}))
    failed = []
    for pane_id, (head, line) in current.items():
        if reported.get(pane_id) != (head, line):
            try:
                report(pane_id, head, line)
            except Exception as error:  # pane closed between snapshot and report
                log("report %s failed: %s" % (pane_id, error))
                failed.append(pane_id)
    for pane_id in failed:  # retry on the next sync
        current.pop(pane_id)
    return current


def clear(reported):
    for pane_id in reported:
        try:
            report(pane_id, None, None)
        except Exception:
            pass


def run():
    reported = {}
    events = None
    gone_since = None
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    log("watcher started (pid %d, socket %s)" % (os.getpid(), socket_path()))
    while not stopping:
        try:
            if events is None:
                events = subscribe()
            reported = sync(reported)
            gone_since = None
            deadline = time.time() + POLL_SECONDS
            while not stopping and time.time() < deadline:
                ready, _, _ = select.select([events], [], [], max(0.0, deadline - time.time()))
                if not ready:
                    continue
                data = events.recv(1 << 16)
                if not data:
                    raise ConnectionError("event stream closed")
                # Let a burst of events settle, then drain and resync.
                time.sleep(DEBOUNCE_SECONDS)
                try:
                    while events.recv(1 << 16):
                        pass
                except (BlockingIOError, InterruptedError):
                    pass
                break
        except Exception as error:
            if events is not None:
                events.close()
                events = None
            gone_since = gone_since or time.time()
            if time.time() - gone_since > GIVE_UP_SECONDS:
                log("server unreachable for %ds, exiting: %s" % (GIVE_UP_SECONDS, error))
                return
            time.sleep(2.0)
    clear(reported)
    log("watcher stopped")


def read_pid():
    try:
        with open(lock_path()) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def start():
    lock = open(lock_path(), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # already running
    if os.fork() > 0:
        return 0
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # Daemon: keep the lock fd open for life; it marks "running".
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        run()
    finally:
        os._exit(0)


def stop():
    pid = read_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return 0
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
    return 0


def main(argv):
    command = argv[1] if len(argv) > 1 else "start"
    if command == "start":
        return start()
    if command == "stop":
        return stop()
    if command == "restart":
        stop()
        return start()
    if command == "once":  # debug: print what would be reported
        for pane_id, (head, line) in compute(call("session.snapshot").get("snapshot", {})).items():
            print("%-10s %-20s %s" % (pane_id, head or "", line))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
