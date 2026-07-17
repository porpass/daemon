# porpass-daemon

The PORPASS processing daemon (`porpass-proc`): a standalone worker that runs on
a separate host, claims queued GRaSP jobs from the PORPASS database, executes
them, and writes results back to shared storage.

It is decoupled from the porpass web app — the two communicate
only through three frozen contracts (schema artifact, per-job config, result
manifest; see the web repo's `docs/processing_contracts.md`), the shared
`porpass-storage` filesystem, and the MariaDB database.

## What it does

For each queued job the daemon:

1. **Claims** it with a race-safe atomic `UPDATE` (safe to run many workers).
2. **Heartbeats** `claimed_at` while it runs; a **reaper** requeues jobs left
   behind by crashed workers.
3. **Resolves inputs** — looks up the observation's files in the DB and stages
   them in a temp dir (downloading `http(s)` archives, or copying locally-mounted
   files).
4. **Renders `job.toml`** from the GRaSP schema defaults + the job's sparse
   config overrides — a byte-reproducible provenance record.
5. **Runs GRaSP** (`grasp run job.toml -v`), streaming output to `run.log`.
6. **Writes `manifest.json`** listing every produced file.
7. **Completes** the job (`succeeded`/`failed`/`cancelled`).

A running job can be **cancelled** — the web sets `cancel_requested`, the daemon
terminates GRaSP, discards partial output, and marks the job `cancelled`.

## Boundaries (hard rules)

- Never modifies the porpass web repo or the database schema.
- Never writes the web-owned columns `output_dir`, `results_deleted`,
  `results_deleted_at`, `rerun_of`, or `cancel_requested` (read-only), and never
  deletes a `processing_jobs` row.
- The DB `processing_jobs.config` is authoritative over the on-disk
  `config.json`.
- No instrument-specific processing is hardcoded — everything derives from the
  schema artifact and the job config.

## Development

```sh
conda env create -f environment.yml
conda activate porpass-proc
pip install -e '.[dev]'

cp .env.example .env      # fill in DB creds + PORPASS_STORAGE_PATH
porpass-daemon            # run the worker loop

pytest                    # run the test suite
```

`GRASP_BIN` may point at the real `grasp` console script or, in tests, a fake
binary. See `deploy/README.md` for systemd installation on `porpass-proc`.
