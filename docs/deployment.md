# Deployment

Running the proxy as a background service: as a `systemd --user` service, or
under a per-user `supervisord` where systemd user services are unavailable
(containers, hosts without a user session manager).

## systemd user service

This is the intended deployment: the proxy starts at login (or boot, with
lingering enabled — see below) and stays up regardless of whether the backend is
running yet. Templates are in [`deploy/systemd/`](../deploy/systemd/).

1. **Build the venv** (if you haven't already):

   ```bash
   uv sync
   ```

2. **Configure the backend secret**, kept outside the repo and outside git.
   Everything else (timeouts, host/port, and even the path to this file) lives
   in the unit itself — see step 3 — so this is the only file you need:

   ```bash
   mkdir -p ~/.config/mcp-call-orchestrator-proxy
   cp deploy/systemd/backend.example.json \
      ~/.config/mcp-call-orchestrator-proxy/backend.json
   chmod 600 ~/.config/mcp-call-orchestrator-proxy/backend.json
   $EDITOR ~/.config/mcp-call-orchestrator-proxy/backend.json   # set url + auth
   ```

3. **Install the unit**, substituting this repo's absolute path for the
   `__REPO_ROOT__` placeholder:

   ```bash
   mkdir -p ~/.config/systemd/user
   sed "s|__REPO_ROOT__|$(pwd)|g" \
      deploy/systemd/mcp-call-orchestrator-proxy.service \
      > ~/.config/systemd/user/mcp-call-orchestrator-proxy.service
   ```

4. **Enable and start it:**

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now mcp-call-orchestrator-proxy.service
   ```

5. **Check it's alive and watch logs:**

   ```bash
   systemctl --user status mcp-call-orchestrator-proxy.service
   journalctl --user -u mcp-call-orchestrator-proxy.service -f
   ```

   It's fine — expected, even — for the log to start with the backend
   unreachable if you haven't launched it yet. Launch the backend whenever you
   like; the proxy reconnects on its own.

6. **After editing the config or updating the code:**

   ```bash
   systemctl --user restart mcp-call-orchestrator-proxy.service
   ```

7. **(Optional) Keep it running without an active login session** — a plain
   `systemctl --user` service normally stops when your last session ends. To have
   it run from boot / survive logout (e.g. on a headless box), enable lingering
   once:

   ```bash
   loginctl enable-linger "$USER"
   ```

## Alternative: settings in a separate env file

The default unit above sets everything except the backend secret directly as
`Environment=` lines, since that's non-secret configuration and keeping it in
the unit means one less file to manage. If you'd rather use the raw
`MCP_PROXY_BACKEND_API_KEY` / `MCP_PROXY_BACKEND_MCP_URL` fields (option 2 in the
[README's configuration](../README.md#configuration)) instead of a JSON backend
config, or just prefer keeping *all* settings out of the unit file, use
[`mcp-call-orchestrator-proxy-envfile.service`](../deploy/systemd/mcp-call-orchestrator-proxy-envfile.service)
instead — it reads everything from an `EnvironmentFile=`:

```bash
cp deploy/systemd/mcp-call-orchestrator-proxy.env.example \
   ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env
chmod 600 ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env
$EDITOR ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env

sed "s|__REPO_ROOT__|$(pwd)|g" \
   deploy/systemd/mcp-call-orchestrator-proxy-envfile.service \
   > ~/.config/systemd/user/mcp-call-orchestrator-proxy.service
systemctl --user daemon-reload
systemctl --user enable --now mcp-call-orchestrator-proxy.service
```

Editing that env file's contents afterward only needs a `restart`, not a
`daemon-reload`, since the unit's reference to the file (its path) hasn't
changed — the tradeoff is that specifiers like `%h` aren't expanded inside the
file's content, so any paths in it must be written out in full. See the
comments in the `.env.example` for details.

## supervisord user instance

A per-user `supervisord` plays the role of `systemd --user`: it runs as the
user, keeps one `[program:x]` file per service in `~/.config/supervisor/conf.d/`
(as `~/.config/systemd/user/` holds one unit per service), restarts the proxy
when it crashes, and logs to files. Templates are in
[`deploy/supervisor/`](../deploy/supervisor/).

It is separate from any system-wide `supervisord`, such as a container's
PID 1: it has its own config, socket and processes, and needs no root.

1. **Install supervisor** — the distribution package (`apt install supervisor`)
   or `uv tool install supervisor`. Only the `supervisord` and `supervisorctl`
   commands are used; a system-wide instance the package may enable is not
   involved.

2. **Build the venv and configure the backend** — steps 1 and 2 of the
   systemd section above.

3. **Configure the settings.** supervisord has no `EnvironmentFile=`, so the
   program sources an env file through `sh`; `$HOME` expands inside it, unlike
   in systemd's env files:

   ```bash
   cp deploy/supervisor/mcp-call-orchestrator-proxy.env.example \
      ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env
   chmod 600 ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env
   $EDITOR ~/.config/mcp-call-orchestrator-proxy/mcp-call-orchestrator-proxy.env
   ```

4. **Install the configs**, substituting this repo's absolute path for the
   `__REPO_ROOT__` placeholder:

   ```bash
   mkdir -p ~/.config/supervisor/conf.d ~/.local/state/supervisor
   cp deploy/supervisor/supervisord.conf ~/.config/supervisor/
   sed "s|__REPO_ROOT__|$(pwd)|g" \
      deploy/supervisor/mcp-call-orchestrator-proxy.conf \
      > ~/.config/supervisor/conf.d/mcp-call-orchestrator-proxy.conf
   ```

5. **Point `supervisorctl` at this instance.** Without `-c`, `supervisorctl`
   searches the working directory and `/etc` for a config, finds the
   system-wide one (or none), and fails on its root-only socket. A wrapper on
   `PATH` makes the choice explicit:

   ```bash
   mkdir -p ~/.local/bin
   cat > ~/.local/bin/supervisorctl-user <<'EOF'
   #!/bin/sh
   exec supervisorctl -c "$HOME/.config/supervisor/supervisord.conf" "$@"
   EOF
   chmod +x ~/.local/bin/supervisorctl-user
   ```

6. **Start it:**

   ```bash
   supervisord -c ~/.config/supervisor/supervisord.conf
   supervisorctl-user status
   ```

   `supervisord` daemonizes and starts every program in `conf.d/`. A program
   that dies within `startsecs` shows `BACKOFF`, then `FATAL`;
   `supervisorctl-user tail mcp-call-orchestrator-proxy` shows why (a missing
   env file, venv or backend config).

7. **Start it at login.** Nothing starts a user `supervisord` by itself. In a
   desktop session, an XDG autostart entry does it:

   ```ini
   # ~/.config/autostart/supervisord-user.desktop
   [Desktop Entry]
   Type=Application
   Name=supervisord (user)
   Exec=sh -c 'mkdir -p "$HOME/.local/state/supervisor" && exec supervisord -c "$HOME/.config/supervisor/supervisord.conf"'
   NoDisplay=true
   ```

   Elsewhere, a login-shell profile or a `@reboot` crontab entry running the
   same command serves.

8. **Watch logs** — files in `~/.local/state/supervisor/`, one per program
   (rotated at 50 MB, 10 backups — supervisord's defaults):

   ```bash
   supervisorctl-user tail -f mcp-call-orchestrator-proxy
   ```

9. **After editing the env file or updating the code**, restart the program;
   after adding, removing or editing a file in `conf.d/`, reload the configs:

   ```bash
   supervisorctl-user restart mcp-call-orchestrator-proxy
   supervisorctl-user reread && supervisorctl-user update
   ```

| `systemctl --user` | `supervisorctl-user` |
|---|---|
| `daemon-reload` | `reread` |
| `enable --now <unit>` | `update` (starts new and changed programs) |
| `status` | `status` |
| `restart <unit>` | `restart <program>` |
| `journalctl --user -u <unit> -f` | `tail -f <program>` |

The program mirrors the systemd unit's behaviour: `autorestart=unexpected`
restarts on a non-zero exit or a kill (`Restart=on-failure`), and
`stopwaitsecs=10` bounds the stop (`TimeoutStopSec=10`). supervisord has no
restart delay (`RestartSec=`), no sandboxing (`NoNewPrivileges=`,
`PrivateTmp=`) and no cgroup. A stdio backend runs in a session of its own,
which the MCP SDK starts it in, so no signal from supervisord reaches it, with
or without `stopasgroup`/`killasgroup`; it exits when its stdin closes, which
happens when the proxy stops or dies. A backend that keeps running after end
of input would outlive a proxy killed with `SIGKILL`, where systemd's cgroup
would stop it.

### Several instances

One proxy fronts one backend, so each backend gets its own program file, env
file and backend config. In the copy of the program file, rename the
`[program:...]` section and point the `command=` at that instance's env file;
in the env file, set a distinct `MCP_PROXY_LISTEN_PORT` and
`MCP_PROXY_SERVER_NAME`. Two instances that share a port, so that only one
runs at a time, take `autostart=false` on the one started by hand.
