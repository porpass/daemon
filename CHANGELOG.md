# Changelog

All notable changes to this project are documented here.
The format is based on Keep a Changelog, and this project adheres to PEP 440 versioning.

## [0.1.0a4] - 2026-07-20
- Updated the README and deploy/README due to errors in the installation
  instructions. Generalized the language.

## [0.1.0a3] - 2026-07-19
### Added
- The daemon publishes a runtime version manifest to
  `{PORPASS_STORAGE_PATH}/runtime.json` on every startup, so the web frontend can
  display the live daemon and GRaSP versions without an HTTP endpoint or any
  coupling to daemon uptime. Written atomically (temp file + `os.replace`) and
  mode `0644`, since the web reads it cold on each request as a different user.
  See "Runtime manifest" in the README for the format.

## [0.1.0a2] - 2026-07-17
### Added
- The daemon now owns the Contract A schema generator (`porpass_daemon.schema.generate`),
  extracted from GRaSP so the science library no longer ships web-form scaffolding.
  Web-form curation (titles, dropdown order, visibility rules, default hints) lives
  in `porpass_daemon.schema.curation`; GRaSP is consumed only via its stable
  `grasp.introspection` surface — including `get_defaults`, `MetaDataInfo`, and
  `WINDOW_FIELDS`, so no daemon code reaches outside GRaSP's guaranteed API.
- `porpass-daemon --regenerate-schema --out <dir>` generates the schema artifacts
  into an arbitrary directory; needs neither database nor storage configuration.

### Changed
- Startup schema publishing generates artifacts **in-process** instead of shelling
  out to `grasp export-schema`. GRaSP must now be *importable* by the daemon (it is
  already required on `PATH` for `grasp run`). `GRASP_BIN` no longer affects
  schema publishing.
- `porpass_daemon.schema` is now a package; its public API (`SchemaArtifact`,
  `load_artifact`, `parse_artifact`, `publish_schemas`, `SchemaError`) is unchanged.

## [0.1.0a1] - 2026-07-15
### Added
- Initial alpha of the PORPASS processing daemon: atomic job claim, input
  fetching (PDS/DARTS downloads + local-disk copy), job.toml rendering,
  GRaSP execution with live run.log, Contract C manifest, terminal-status
  completion, heartbeat + crash-recovery reaper, and job cancellation.
- systemd unit and DEV-server deployment guide.
