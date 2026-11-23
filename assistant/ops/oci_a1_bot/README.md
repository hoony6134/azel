# a1bot — Always Free A1 launcher

Retries a launch of one `VM.Standard.A1.Flex` (2 OCPU / 12 GB, `kepler`, Ubuntu 24.04 aarch64,
public IP) in OCI Chuncheon until capacity appears, then writes `~/.local/state/a1bot/DONE`, notifies the
ntfy `infra` topic and never runs again. Runs on sadr as a user service.

- Free-tier guard before every attempt: A1 OCPU/memory and boot+block GB are summed over all compartments;
  it refuses (exit 3, no restart) if the launch would pass 2 OCPU, 12 GB or 200 GB. Boot volume is 50 GB or
  the remainder, never under 47.
- Subnet, AD and SSH public keys are copied from the `main-amd` instance. Attempts cycle no-FD, FD-1/2/3
  every ~60 s (+jitter); 429 backs off 60 s → 15 min.
- `a1bot dry-run` resolves and prints everything without launching; `a1bot selfcheck` tests the guard math;
  `a1bot status` shows attempts, last error and time since service start.

Deploy: `scp bot.py a1bot ubuntu@sadr:~/a1bot/`, unit to `~/.config/systemd/user/`, then
`systemctl --user daemon-reload && systemctl --user enable --now a1bot`.
Logs: `journalctl --user -u a1bot`, `~/.local/state/a1bot/a1bot.log`.

## Limit-watch mode

The free tenancy has A1 availability 0 and cannot file a limit request, so the bot first only watches: every
~60 min it reads `standard-a1-core-count`/`-memory-count` availability and logs one line, with no launch call.
When availability appears it sends one ntfy ("A1 한도가 생겼어요, 생성 시도 시작") and switches to the launch loop,
launching the largest shape that fits the limit and the free-tier caps (e.g. 1/6) and saying so on success.
`a1bot status` shows mode, last check, limit and attempts.

## kepler bootstrap and notifications

- `kepler-init.sh` is passed as cloud-init `user_data` (deneb's public keys prepended as `KEYS`) and can be
  re-run by hand with `sudo bash kepler-init.sh` (idempotent, logs to `/var/log/kepler-init.log`). It sets
  hostname/timezone, installs packages, docker, node-exporter, uv, Tailscale (**not joined**: the user runs
  `sudo tailscale up --hostname kepler` with an auth key), key-only SSH, iptables with public SSH only
  (node-exporter is reachable over `tailscale0` only) and the motd.
- On success: ntfy first (title "kepler 생성됨", high), then `POST https://azel.scian.xyz/v1/calls` with the
  gateway token from `~/.config/azel/token` (0600, not in the repo). Both are always tried; only HTTP statuses
  are logged. `dry-run` stats the token and does an unauthenticated OPTIONS on both endpoints; it never calls.
- Idle reclaim: Oracle may reclaim an A1 whose 7-day p95 is under 20% for CPU, network and memory. Nothing
  fake is run; the gateway is meant to be the real load. Watch it if kepler sits idle for a week.
