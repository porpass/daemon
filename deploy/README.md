# Deploying porpass-daemon to the DEV server (porpass-proc)

Every configuration step to stand the daemon up on the DEV server, in order.
The target profile (confirmed for DEV):

- **Host / init:** Linux with **systemd**
- **GRaSP:** installed into the **same conda env** as the daemon (`porpass-proc`),
  so the bare command `grasp` is on `PATH`
- **Database:** **remote** MariaDB host (daemon connects over the network)
- **Storage:** `porpass-storage` is a **shared network mount** (NFS/SMB) also
  mounted on `porpass-web`

Anything the daemon needs from another host it reaches only two ways: the
**database** and the **shared filesystem**. There is no HTTP API between web and
daemon. Get those two channels right and the rest is process management.

The daemon implements the full flow end to end: reap stale jobs → atomically
claim the next queued job → download its inputs from the archives → render
`job.toml` → run GRaSP (streaming to `run.log`) → write `manifest.json` → record
the terminal status. The whole pipeline has been validated against real GRaSP,
the real database, the PDS archive, and the shared mount.

---

## 0. Prerequisites

- SSH access to `porpass-proc` with `sudo`.
- A conda/miniconda installation on the host (assumed at `/opt/miniconda3`;
  adjust paths if different).
- Network path from `porpass-proc` to the MariaDB host on port **3306**.
- The `porpass-storage` network export reachable from `porpass-proc`.
- The daemon's git repo reachable (clone URL or a tarball).

---

## 1. Service account and directories

Run the daemon as a dedicated unprivileged user so file ownership on the shared
mount is predictable.

```sh
sudo useradd --system --create-home --shell /usr/sbin/nologin porpass
sudo mkdir -p /opt/porpass-daemon          # code
sudo mkdir -p /etc/porpass-daemon          # config (secrets)
```

**uid/gid alignment (important for the shared mount):** the daemon writes job
output that `porpass-web` reads, and vice-versa. Either give the `porpass`
service user the **same uid/gid** on both hosts, or ensure the export maps both
hosts' service users to a common owner/group with read+write. Mismatched uids
are the most common cause of "web can't read the daemon's results" bugs.

---

## 2. Get the code

```sh
sudo -u porpass git clone <repo-url> /opt/porpass-daemon
cd /opt/porpass-daemon
```

---

## 3. Create the conda env (installs the daemon)

`environment.yml` creates the `porpass-proc` env (Python 3.12) and pip-installs
the daemon in editable mode.

```sh
sudo -u porpass /opt/miniconda3/bin/conda env create -f /opt/porpass-daemon/environment.yml
# verify the console script exists:
/opt/miniconda3/envs/porpass-proc/bin/porpass-daemon --help
```

---

## 4. Install GRaSP into the same env

The daemon needs GRaSP **two ways**, and the same-env model satisfies both:

- **importable** (`import grasp`) — the daemon generates the schema artifacts
  in-process from GRaSP's `grasp.introspection` surface;
- **on `PATH`** (`GRASP_BIN=grasp`) — it shells out to `grasp run` to process a
  job.

```sh
sudo -u porpass /opt/miniconda3/envs/porpass-proc/bin/pip install <grasp-package-or-path>
# verify BOTH: the CLI resolves, and the package imports
/opt/miniconda3/envs/porpass-proc/bin/grasp --help
/opt/miniconda3/envs/porpass-proc/bin/python -c "import grasp; print(grasp.__version__)"
```

Confirm the versions line up: the daemon stamps each schema artifact with the
`grasp_version` it generated from, and the web reads them.

---

## 5. Mount porpass-storage (shared network mount)

Mount the export at a stable path (example `/mnt/porpass-storage`). Example NFS
`/etc/fstab` line:

```
nfs-server:/export/porpass-storage  /mnt/porpass-storage  nfs  rw,hard,noatime,_netdev  0  0
```

```sh
sudo mkdir -p /mnt/porpass-storage
sudo mount /mnt/porpass-storage
# the two subtrees the daemon uses:
#   {mount}/schemas/                      <- daemon publishes here (web reads)
#   {mount}/processing/{user_id}/{job_id} <- web creates, daemon fills in
sudo -u porpass test -w /mnt/porpass-storage && echo "porpass can write: OK"
```

- Use `_netdev` (and `RequiresMountsFor=` in the unit, already set) so the
  daemon doesn't start before the mount is ready.
- Verify the `porpass` user can **read and write** — create and delete a probe
  file as that user.

---

## 6. Database access (remote MariaDB)

The daemon uses the **same schema and credentials model** as porpass-web, just
from a different host. On the DB host, grant the daemon's user access **from the
porpass-proc host**:

```sql
-- least-privilege: the daemon reads observations/files and updates
-- processing_jobs; it never alters schema or deletes job rows.
CREATE USER 'porpass_proc'@'<porpass-proc-ip>' IDENTIFIED BY '<password>';
GRANT SELECT ON porpass_dev.observations       TO 'porpass_proc'@'<porpass-proc-ip>';
GRANT SELECT ON porpass_dev.lrs_files          TO 'porpass_proc'@'<porpass-proc-ip>';
GRANT SELECT ON porpass_dev.sharad_files       TO 'porpass_proc'@'<porpass-proc-ip>';
GRANT SELECT ON porpass_dev.marsis_files       TO 'porpass_proc'@'<porpass-proc-ip>';
GRANT SELECT, UPDATE ON porpass_dev.processing_jobs TO 'porpass_proc'@'<porpass-proc-ip>';
FLUSH PRIVILEGES;
```

- Open port **3306** from `porpass-proc` to the DB host (security group /
  firewall). The daemon connects over TCP to `DB_HOST`.
- Confirm `bind-address` on the DB host allows the remote connection.
- **TLS (recommended for a remote DB):** if the server enforces TLS, we'll need
  to pass CA/cert options to the connection. `pymysql` supports an `ssl` config;
  the daemon does not wire TLS options yet — flag it and we'll add
  `DB_SSL_CA`/related env vars before a TLS-required DEV DB. _(pending)_
- Quick connectivity check from the daemon host (no daemon needed):
  ```sh
  /opt/miniconda3/envs/porpass-proc/bin/python -c \
    "import pymysql,os; pymysql.connect(host=os.environ['DB_HOST'],port=int(os.environ['DB_PORT']),user=os.environ['DB_USERNAME'],password=os.environ['DB_PASSWORD'],database=os.environ['DB_DATABASE']).ping(); print('DB OK')"
  ```

---

## 7. The environment file (all config + secrets)

Create `/etc/porpass-daemon/porpass-daemon.env`. This is the single source of
configuration in production — there is **no `.env` in the deployed repo**; the
daemon reads its process environment, which systemd populates from this file.

```ini
# /etc/porpass-daemon/porpass-daemon.env   (chmod 600, owned by porpass or root)

# Database (remote MariaDB)
DB_HOST=<db-host>
DB_PORT=3306
DB_DATABASE=porpass_dev
DB_USERNAME=porpass_proc
DB_PASSWORD=<password>            # literal; systemd does NOT expand '$'

# Shared storage mount
PORPASS_STORAGE_PATH=/mnt/porpass-storage

# GRaSP (same-env model -> bare name resolves on PATH)
GRASP_BIN=grasp

# Daemon behaviour
DAEMON_POLL_INTERVAL=5
DAEMON_HEARTBEAT_INTERVAL=30
DAEMON_REAPER_STALE_SECONDS=300
DAEMON_WORKER_ID=                 # blank -> {hostname}-{pid}
DAEMON_PUBLISH_SCHEMAS_ON_START=1
```

```sh
sudo install -o porpass -g porpass -m 600 /dev/null /etc/porpass-daemon/porpass-daemon.env
sudo -u porpass editor /etc/porpass-daemon/porpass-daemon.env   # paste the above
```

Notes:
- `chmod 600` — the file holds the DB password. Never commit it; it lives only
  on the host.
- systemd reads env-file values **literally**, so the `$` in the password needs
  no escaping (unlike a shell `.env`).

---

## 8. Install and start the systemd service

```sh
sudo cp /opt/porpass-daemon/deploy/porpass-daemon.service /etc/systemd/system/
# edit the EDIT-ME lines in the unit: conda base path, User/Group, mount point
sudo systemctl daemon-reload
sudo systemctl enable --now porpass-daemon
sudo systemctl status porpass-daemon
```

The unit runs the daemon via `conda run --no-capture-output -n porpass-proc`, so
GRaSP's env is activated and `grasp` is on `PATH`. It restarts on failure and
forwards SIGTERM for graceful shutdown.

---

## 9. Publish schema artifacts

With `DAEMON_PUBLISH_SCHEMAS_ON_START=1`, the service publishes on every start —
so `sudo systemctl restart porpass-daemon` then checking the journal + schemas
dir is the simplest path.

To publish once explicitly with a pass/fail exit code, run it as a transient
unit so systemd parses the env file with the same **literal** semantics as the
service (do not `source` the env file in a shell — that would mangle the `$` in
the password):

```sh
sudo systemd-run --uid=porpass --gid=porpass \
  --property=EnvironmentFile=/etc/porpass-daemon/porpass-daemon.env \
  --wait --pipe \
  /opt/miniconda3/bin/conda run --no-capture-output -n porpass-proc \
  porpass-daemon --publish-schemas

ls -1 /mnt/porpass-storage/schemas/      # expect SHARAD/MARSIS/LRS *.schema.json
```

Verify porpass-web now reads these artifacts (its config forms should reflect
the live GRaSP version).

---

## 10. Verify end to end

```sh
journalctl -u porpass-daemon -f          # follow logs
```

Expected on a healthy start:

```
worker <host>-<pid> starting (storage=/mnt/porpass-storage, poll=5.0s)
database connection ok (porpass_proc@<db-host>:3306/porpass_dev)
published 7 schema artifact(s) to /mnt/porpass-storage/schemas
```

Then submit a job from porpass-web and watch a full run in the journal:

```
claimed job <id> as <host>-<pid>
handling job <id>: SHARAD EDR obs=<n> out=/mnt/porpass-storage/processing/<u>/<id>
downloading https://pds-geosciences.wustl.edu/.../<obs>.LBL -> ...
running GRaSP: grasp run .../job.toml -v
wrote manifest with <k> file(s) to .../manifest.json
job <id> succeeded
```

Inspect the job directory — the daemon owns everything except `config.json`:

```sh
ls -la /mnt/porpass-storage/processing/<user_id>/<job_id>/
#   config.json    (web-written, inspectable)
#   job.toml       (frozen provenance — the exact TOML GRaSP ran)
#   run.log        (full GRaSP output; kind:"log", preserved on results-delete)
#   manifest.json  (Contract C: every product + run.log, deleted:false)
#   <products>     (.img/.csv/.grsp, .bmp/_browse.png, .sgy, .h5, cluttergram)
cat /mnt/porpass-storage/processing/<user_id>/<job_id>/manifest.json
```

Confirm the DB row: `status='succeeded'`, `claimed_at=NULL`, `completed_at` set.

**Failure path.** If GRaSP exits non-zero (or inputs can't be resolved), the row
goes to `status='failed'` with a concise `error_message` (full detail in
`run.log`), and `manifest.json` still lists `run.log` (`kind:"log"`). Nothing is
left `running`.

**Crash recovery.** If a worker dies mid-run, its `claimed_at` stops advancing;
after `DAEMON_REAPER_STALE_SECONDS` the reaper returns the job to `queued` (watch
for `requeued abandoned job <id>`). `porpass-daemon --reap-once` runs the reaper
standalone.

**Cancellation.** When the web sets `processing_jobs.cancel_requested = 1` on a
running job, the daemon (which polls the flag every `DAEMON_CANCEL_POLL_INTERVAL`
seconds) terminates GRaSP (SIGTERM → grace → SIGKILL), discards the partial
products (keeping `run.log`, `job.toml`, `config.json`), writes the manifest, and
sets `status='cancelled'`. This requires the schema to carry a
`cancel_requested TINYINT(1)` column and a `'cancelled'` value in the `status`
ENUM; without them cancellation is simply unavailable (the daemon logs `cancel
disabled this run` and keeps working). The web owns the `cancel_requested` flag
and the Cancel UI; the daemon only reads the flag and owns the `cancelled`
transition. The existing `SELECT`+`UPDATE` grant on `processing_jobs` already
covers it — no grant change needed.

**Boundaries the daemon honors** (useful when auditing): it never writes
`output_dir`, `results_deleted`, `results_deleted_at`, or `rerun_of`; never
deletes a `processing_jobs` row; and always treats the DB `config` column as
authoritative over the on-disk `config.json`. The DB grant in step 6 enforces
this at the privilege level.

---

## Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DB_HOST` | yes | `localhost` | MariaDB host (remote on DEV) |
| `DB_PORT` | no | `3306` | MariaDB port |
| `DB_DATABASE` | yes | `porpass` | Database name (`porpass_dev` on DEV) |
| `DB_USERNAME` | yes | `porpass` | DB user (`porpass_proc`) |
| `DB_PASSWORD` | yes | — | DB password (literal in env file) |
| `PORPASS_STORAGE_PATH` | **yes** | — | Shared storage mount root; daemon exits if unset |
| `GRASP_BIN` | no | `grasp` | GRaSP executable used for `grasp run` (bare name in same-env model). Not used for schema generation, which is in-process. |
| `DAEMON_POLL_INTERVAL` | no | `5` | Seconds between polls when idle |
| `DAEMON_HEARTBEAT_INTERVAL` | no | `30` | Seconds between `claimed_at` heartbeats |
| `DAEMON_REAPER_STALE_SECONDS` | no | `300` | Age at which a `running` job is requeued |
| `DAEMON_CANCEL_POLL_INTERVAL` | no | `5` | Seconds between checks of a running job's `cancel_requested` flag |
| `DAEMON_WORKER_ID` | no | `{hostname}-{pid}` | `claimed_by` identity |
| `DAEMON_PUBLISH_SCHEMAS_ON_START` | no | `1` | Publish schemas on startup |

## Operations quick reference

```sh
systemctl status porpass-daemon
systemctl restart porpass-daemon
journalctl -u porpass-daemon -f
journalctl -u porpass-daemon --since "1 hour ago"

# one-off maintenance (inside the porpass-proc env)
porpass-daemon --publish-schemas               # regenerate into the storage mount
porpass-daemon --regenerate-schema --out /tmp/x  # generate anywhere; no DB/storage needed
porpass-daemon --reap-once                     # requeue stale 'running' jobs
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `schema publish skipped: GRaSP is not importable …` | The daemon generates artifacts in-process, so GRaSP must be **importable** in the env the unit runs under — install it into `porpass-proc` (step 4). Publish failures are non-fatal (logged as a warning); the daemon keeps claiming jobs. |
| `GRaSP executable 'grasp' not found` during a job run | Separate from the above: `grasp run` needs the CLI on `PATH`. The unit runs via `conda run -n porpass-proc`; install GRaSP there or set `GRASP_BIN` to an absolute path. |
| Job fails with a `PermissionError` writing `job.toml`/products | uid/gid or mount permissions: the service user can't write the web-created job dir. Align uids or make the export group-writable (step 1 / step 5). |
| `configuration error: PORPASS_STORAGE_PATH is required` | The env file wasn't loaded or the var is blank. Check `EnvironmentFile=` path and that the unit was reloaded (`systemctl daemon-reload`). |
| `Can't connect to MySQL server` / timeout | Remote DB unreachable: firewall/security group on 3306, `bind-address`, or the grant isn't for the `porpass-proc` host IP (step 6). |
| DB requires TLS | Not yet wired — the daemon needs `pymysql` `ssl` options added (`DB_SSL_CA` etc.) before a TLS-enforcing DB. Flag it and we'll add it. |
| A job is stuck `running` after a crash | Expected until `DAEMON_REAPER_STALE_SECONDS` elapses, then the reaper requeues it. Force it now with `porpass-daemon --reap-once`. |
| Cancel button does nothing; log shows `cancel disabled this run` | The `cancel_requested` column / `cancelled` status isn't in the DB yet. Apply the schema change; the daemon degrades gracefully until then. |
| Job fails but GRaSP's error is a value it *lists as valid* | Likely a GRaSP-side validator/case mismatch, not a daemon issue — the daemon renders the canonical Contract B value verbatim. Check `run.log`; fix on the GRaSP side. |

## Security checklist

- `/etc/porpass-daemon/porpass-daemon.env` is `chmod 600`; the DB password
  never enters the repo or the journal.
- DB grants are least-privilege: `SELECT` on observation/file tables,
  `SELECT`+`UPDATE` on `processing_jobs` only. No `DELETE`, no DDL.
- Port 3306 open only from `porpass-proc` to the DB host.
- The daemon never writes the web-owned columns (`output_dir`,
  `results_deleted`, `results_deleted_at`, `rerun_of`) and never deletes job
  rows — the least-privilege grant does not need to allow those anyway.

## Multi-worker note

The atomic claim is race-safe, so you can run more than one instance later
(e.g. a templated `porpass-daemon@.service`) once throughput demands it. Each
gets a distinct `claimed_by`. Start with a single instance on DEV.

---

## Appendix: local dev (macOS) storage permissions

This is **macOS-local-dev only** — the DEV/production server uses the NFS mount
plus uid/gid alignment described above, not this ACL method.

On a single Mac the web app (Apache/PHP under XAMPP, running as `daemon`) and the
daemon (running as your login user) are two different uids sharing one local
`porpass-storage` directory. The web creates each job dir; the daemon writes
`job.toml` / `run.log` / `manifest.json` / products into it. Give both users
write access via a shared group and an **inherited ACL**, so new job dirs the web
creates are group-writable regardless of Apache's umask:

```sh
# Shared group with both users
sudo dseditgroup -o create porpass
sudo dseditgroup -o edit -a "$(id -un)" -t user porpass    # your login user
sudo dseditgroup -o edit -a daemon      -t user porpass    # XAMPP's user

# Group-own the tree; setgid so new dirs inherit the group
sudo chgrp -R porpass /Users/Shared/porpass-storage
sudo chmod -R g+rwX   /Users/Shared/porpass-storage
sudo find /Users/Shared/porpass-storage -type d -exec chmod g+s {} +

# Inherited ACL — the key step: new files/dirs under the tree automatically
# grant the porpass group full access, so umask can't strip group-write.
sudo chmod -R +a "group:porpass allow read,write,execute,delete,add_file,add_subdirectory,file_inherit,directory_inherit" /Users/Shared/porpass-storage
```

Notes:
- Group membership is only picked up by **new** login sessions — open a fresh
  terminal before running the daemon.
- Verify: `ls -led /Users/Shared/porpass-storage` should show `drwxrwsr-x+`
  (group `porpass`, setgid `s`, ACL `+`) with a `group:porpass … file_inherit,
  directory_inherit` ACE.
- The inherited ACL covers **new** files; if you ever recreate or restore the
  `porpass-storage` tree from scratch, re-run the `chmod +a` step.
```
