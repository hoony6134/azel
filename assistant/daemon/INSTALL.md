# Biseo agent daemon: install

1. `mkdir -p ~/.config/biseo && cp daemon.example.toml ~/.config/biseo/daemon.toml && chmod 600 ~/.config/biseo/*`
   and edit `id`, `name`, `host` (this host's Tailscale IP), `allowed_roots`. The token file must already exist.
2. `uv sync` (on this Mac: `env -u CONDA_PREFIX uv sync`), then try it: `uv run python biseo_daemon.py`.
   For the `browse` tool: `uv run playwright install chromium` (downloads ~100 MB into the Playwright cache in
   your home, `~/Library/Caches/ms-playwright` on macOS, `~/.cache/ms-playwright` on Linux; Linux may also need
   `uv run playwright install-deps chromium`). Without it `browse` returns an error; other tools are unaffected.
   The `browser` tool (Mac only) uses the same Chromium: set `browser_enabled = true` in `daemon.toml`. Its profile is
   `~/.config/biseo/browser-profile` (0700, never for logins: cookies are cleared at every browser start); downloads, PDFs and screenshots go to `~/Downloads/azel`. Sites such as Naver
   and Google may show a human check to a headless browser: the tool reports `bot_check` and stops; open the page with
   `show=true` and pass the check yourself.
3. macOS, from this directory: fill in the checkout path, then load it. Logs: `~/Library/Logs/biseo-daemon.log`.
   ```sh
   sed "s|__DAEMON_DIR__|$PWD|g" xyz.scian.biseo-daemon.plist > ~/Library/LaunchAgents/xyz.scian.biseo-daemon.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/xyz.scian.biseo-daemon.plist
   ```
4. Linux: see the header of `biseo-daemon.service` (systemd user unit; expects the checkout at `~/biseo/daemon`).
5. Tests: `uv run pytest`.
