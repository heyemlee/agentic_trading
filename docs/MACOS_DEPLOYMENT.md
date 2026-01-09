# macOS always-on deployment

This deployment keeps the UI and API on `127.0.0.1:8787`, starts them after login, runs the
configured weekly research trigger, and creates a verified SQLite backup every day. It does
not enable real-money execution.

Keep the deployed checkout outside macOS privacy-protected folders such as Desktop, Documents
and Downloads. Background LaunchAgents cannot reliably read those folders. `~/Projects` is a
suitable location; a Desktop symlink may point to it for convenience.

## One-time local setup

Stop any terminal-run `ct-api`, then migrate and inspect the state before installing the jobs:

```bash
uv run ct-state migrate
uv run ct-state status
uv run ct-state backup
uv run ct-auth
./deploy/macos/install.sh
```

`ct-auth` asks for the owner password without echoing it. Do not put that password in a shell
script, `.env`, source file or chat message. The password file and broker token must stay mode
`0600`.

The installed user agents are:

- `com.local.agentic-trading.api`: run at login and restart after failure;
- `com.local.agentic-trading.research-weekly`: at the time in
  `risk_profile.research_schedule.launchd` (Friday 13:30 local by default). The plist is
  generated from that configuration, so the console's "next run" and the installed job cannot
  drift apart;
- `com.local.agentic-trading.backup-daily`: 03:15 local, with an integrity check and 30-day
  local retention;
- `com.local.agentic-trading.maintenance-daily`: 03:45 local — log rotation, run-artefact
  retention and a free-disk check.

They are LaunchAgents, so **after a full reboot somebody must log in to this Mac once** before
any of it starts. Nothing runs at the login window. Keep the Mac connected to power. Display
sleep is fine; system sleep is not. Logs are under `runs/service/` and are rotated by the
maintenance job.

Rerunning `./deploy/macos/install.sh` with no argument keeps the remote HTTPS origin that is
already installed. Pass an origin to change it, or `--clear` to go loopback-only on purpose.

## Private phone access

Install the standalone Tailscale app on this Mac and the Tailscale app on the phone, and sign
both into the same tailnet. Keep `ct-api` on loopback. After Tailscale is connected, configure a
private HTTPS reverse proxy:

```bash
tailscale serve --bg 127.0.0.1:8787
tailscale serve status
```

Take the displayed `https://...ts.net` origin and reinstall the launch jobs with it:

```bash
./deploy/macos/install.sh https://your-device.your-tailnet.ts.net
```

Open `https://your-device.your-tailnet.ts.net/ui/` on the phone and sign in with the console
owner account. Use Tailscale Serve, not Funnel: Serve stays inside the tailnet while Funnel is
public. Restrict the tailnet policy to the owner's devices.

## Releases: keeping the service off the development checkout

Running the service straight out of a working checkout means every save is a deployment: an
edit changes what the API loads on its next restart, and a worker spawned mid-edit can run a
different version from the process that spawned it. `release.sh` separates the two.

A release is an immutable export of a git commit with its own virtual environment, under
`~/Library/Application Support/agentic-trading/releases/<timestamp>-<sha>`. A `current` symlink
says which one the launchd jobs run. **Config, state and runs stay in the data directory and are
never copied into a release**, so publishing and rolling back never touch a ledger.

```bash
deploy/checks.sh                          # lint, tests, frontend cases, secret scan
deploy/macos/release.sh publish           # from a committed tree; --data-dir if it differs
deploy/macos/release.sh list              # * marks the running release
deploy/macos/release.sh rollback          # back to the previous release
```

`publish` refuses to run over an order the broker still owns, backs up the database first, and
if the new release does not answer on loopback it puts the previous one back automatically.

Rollback moves the **code** only. The database is not rolled back, so a release that migrated
it must be restored with `ct-state restore` before an older release can read it.

To develop while the service runs, keep editing the git checkout: the running release is a
separate copy and is unaffected until the next `publish`.

## Operations

```bash
deploy/macos/selfcheck.sh                 # after a reboot: jobs, API, database, Tailscale
launchctl print gui/$UID/com.local.agentic-trading.api
uv run ct-state status                    # integrity, counts, unsettled orders
uv run ct-ops status                      # activity heartbeats and free disk
uv run ct-trigger scheduled --dry-run     # what the schedule would do, starting nothing
tail -n 100 runs/service/api.stderr.log
```

To change the research schedule, edit `research_schedule.launchd` in `config/risk_profile.yaml`
and rerun the installer. To stop and remove all four jobs without deleting state:

```bash
./deploy/macos/uninstall.sh
```

## Backups off this machine

Local backups protect against a bad write, not against losing the Mac. Point the daily job at a
second destination and give it a passphrase file; the copy that leaves this machine is
encrypted with AES-256 because it lands on hardware whose physical security is not this Mac's.

```bash
umask 077 && head -c 48 /dev/urandom | base64 > ~/.config/ct-backup.key   # keep a copy elsewhere
launchctl setenv CT_BACKUP_MIRROR /Volumes/Backup/agentic-trading
launchctl setenv CT_BACKUP_KEY_FILE "$HOME/.config/ct-backup.key"
```

Store the passphrase somewhere that does **not** depend on this Mac. A backup you cannot decrypt
is not a backup. Never sync or copy the live `trading_state.sqlite3` file; use `ct-state backup`.

Restoring is never only a file copy:

```bash
uv run ct-state restore --source <backup.sqlite3[.enc]> --into /path/to/state [--key-file <f>]
```

It refuses a damaged snapshot, revokes every console session it contains (they were valid when
the snapshot was taken, not now), releases stale order reservations, and prints the orders whose
real fate is known only to the broker. Check those against the broker before allowing execution
again, and record the data-loss window: everything after the backup's timestamp is gone.
