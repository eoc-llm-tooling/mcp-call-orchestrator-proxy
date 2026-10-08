# Deployment

Running the proxy as a `systemd --user` service.

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
