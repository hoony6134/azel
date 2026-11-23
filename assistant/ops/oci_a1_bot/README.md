# a1bot — Always Free A1 launcher

Retries a launch of one `VM.Standard.A1.Flex` (2 OCPU / 12 GB, `chuncheon-a1`, Ubuntu 24.04 aarch64,
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
