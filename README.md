# Mimicry

A portfolio-mirroring service for Interactive Brokers, with an Alpaca paper
comparison runner. It consumes a configured portfolio tracker's holdings API,
resolves instruments through OpenFIGI and IBKR, and plans quantity-based orders.

## Components

- `mirror.py`: tracker client, weight calculation, FIGI lookup, Alpaca paper mirror.
- `mirror_ibkr.py`, `ibkr_contracts.py`, `ibkr_execution.py`: contract resolution,
  account-bound execution, position cleanup, marketable limits, and long options.
- `ibkr_state.py`: SQLite runs, order intents, fills, and reconciliation state.
- `reconcile.py`, `alerts.py`, `deadman_check.py`, `daily_summary.py`: monitoring.
- `deploy/`: Linux systemd templates; `backup_state.py` and shell helpers provide
  encrypted state backups using age and rclone.

This is experimental execution software, not a validated turnkey live-trading
system. Unit tests use simulated broker objects; they do not establish broker
permissions, fill quality, market-data access, or production reliability.

## Local Development

Python 3.10 or newer is required. Runtime and test dependencies are pinned.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

The tests use dummy configuration and do not submit orders. The repository does
not include credentials, browser sessions, holdings snapshots, account ledgers,
contract caches, or private operating notes.

For a deployment, configure an untracked `secrets.env` using the variable names
in `secrets.env.example`. The tracker adapter expects its existing holdings JSON
schema and session-cookie flow; other providers require an adapter. Alpaca
variables are also read by the shared tracker module, even on the IBKR path.
Install Playwright's Chromium browser for the authentication helper.

Do not enable execution or timers until you have reviewed the code, supplied an
explicit account ID, configured alerts, and validated the complete workflow in
paper mode. Paper and live accounts require different confirmation flags.
The live baseline command records account state without submitting orders.

## Linux Deployment Templates

The examples assume a dedicated `mimicry` service user and these generic paths:

| Purpose | Path |
| --- | --- |
| Application | `/opt/mimicry` |
| Python environment | `/opt/mimicry-env` |
| IBC configuration | `/opt/ibc-paper` or `/opt/ibc-live` |

Shell runners support `MIMICRY_HOME` and `MIMICRY_PYTHON`. Edit the systemd units
if using a different layout. `deploy_pi.sh user@host` targets an existing
installation; it is not a host provisioning script. Review `pi_preflight.sh`
and `deploy/install_systemd.sh` before use: the installer changes schedules and
enables services, so it must not be run casually during local development.

Keep the Gateway API private. Restrict file permissions for configuration,
sessions, databases, backups, and logs. Use a single execution node per account.

## Privacy And State Compatibility

The public edition uses the `MIMICRY:` order-reference prefix, `MIMICRY_*`
environment variables, and `mimicry_state.sqlite3`. It is **not an in-place
upgrade** for an installation using another namespace. Changing those identifiers
without migrating state and handling outstanding orders can cause duplicate or
unmanaged orders. Do not deploy this public checkout over an existing strategy
installation without an explicit migration review.

Never commit `secrets.env`, cookies, OTPs, broker account data, exported holdings,
logs, screenshots, SQLite files, or backup archives. The ignore rules are a
convenience, not a substitute for reviewing staged files and scanning Git history.
Logs and encrypted backups should also be treated as sensitive. Report suspected
credential exposure privately and rotate affected credentials before continuing.
