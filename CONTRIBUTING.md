# Contributing to porpass/daemon

Thanks for your interest in improving PORPASS. Bug reports, feature
requests, and pull requests are all welcome.

## Before you start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md).
- For substantial changes, open an issue first so we can align on
  the approach before you invest coding time.
- Security-sensitive issues follow the process in
  [SECURITY.md](SECURITY.md) — please do not open a public issue
  for those.

## Reporting issues

Bugs and feature requests are filed through GitHub Issues. Opening a
new issue offers two forms:

- **Bug Report** — something is broken, incorrect, or not behaving
  as expected.
- **Feature Request** — new functionality or an improvement to what
  already exists.

Please search [existing issues](../../issues) first to avoid
duplicates, and file one issue per report. Blank issues are
disabled; if neither form fits, the chooser links to alternatives.

A good bug report includes the exact steps to reproduce, what you
expected versus what happened, any error text or traceback (copied,
not paraphrased), the relevant daemon logs, and your daemon version,
Python version, and OS. The form prompts for each of these. Please
redact database credentials and internal hostnames from anything you
paste.

## Development setup

See the "Development" section of [README.md](README.md) for
installation instructions. For production deployment and the
development (ad-hoc) workflow, see
[deploy/README.md](deploy/README.md).

## The porpass/* organization

This repo is one of four under [github.com/porpass](https://github.com/porpass):

- `porpass/web` — the PHP web application
- `porpass/daemon` — this repo, the processing worker
- `porpass/grasp` — the radar processing library
- `porpass/db` — database schema and provisioning

## Branches and pull requests

This repo follows a Git Flow model with two long-lived branches:

- `main` holds released versions only. It changes only when a
  release or hotfix is merged in, and every commit on it is tagged
  and deployable. Production is deployed from a version tag on
  `main`.
- `develop` is the integration branch where ongoing work
  accumulates. It is the default target for everyday changes.
- Feature branches: `feature/<short-name>`, off `develop`.
- Bug fixes: `debug/<short-name>` or `fix/<short-name>`, off
  `develop`.
- Release branches: `release/<version>` (e.g. `release/0.2.0`) —
  short-lived, for stabilizing a version before it ships.
- Hotfix branches: `hotfix/<short-name>` — urgent fixes to the
  released production version.

### Everyday work

Branch `feature/*` or `fix/*` off `develop`, describe the change in
the PR body, and squash-merge back into `develop`. Small,
independent commits are fine - the squash collapses them.
Squash-merging applies to these short-lived branches only; the
release and hotfix merges below do not squash.

### Cutting a release

When `develop` is feature-complete for the next version, cut
`release/<version>` from `develop`. Only stabilization commits land
there — bug fixes, the version bump, and final prep. Tag
pre-releases on the branch as you go (e.g. `v0.2.0a1`,
`v0.2.0b1`). When it's ready:

1. Merge `release/<version>` into `main` with a normal merge (not
   squashed), tag `main` with the final version (e.g. `v0.2.0`), and
   deploy that tag.
2. Merge `release/<version>` back into `develop` so the
   stabilization fixes aren't lost.
3. Delete the release branch.

### Hotfixes

For an urgent fix to the live version, branch `hotfix/<short-name>`
from the released tag on `main`, commit the fix, and merge it back
into `main` with a normal merge. Tag the patch release (e.g.
`v0.2.1`) and deploy. Merge the hotfix into `develop` as well so the
fix carries forward.

## Commit style

The project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(scope): summary
fix(scope): summary
refactor(scope): summary
docs(scope): summary
chore(scope): summary
```

Common scopes: `worker`, `db`, `schema`, `inputs`, `render`,
`grasp`, `manifest`, `reaper`, `cancel`, `runtime`, `config`,
`deploy`.

## Versioning

The daemon uses [PEP440](https://peps.python.org/pep-0440/)
versioning consistent with Python standards.
See [CHANGELOG.md](CHANGELOG.md) for the release log in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

The version is declared in **two** places, which must be bumped
together:

- `pyproject.toml` — the packaging metadata
- `src/porpass_daemon/__init__.py` — `__version__`, which is what the
  daemon reports in the runtime manifest it publishes

Git tags are `v`-prefixed and match the declared version (e.g.
`v0.1.0a4`).

## Reviews and merging

The maintainers will review PRs; there is no formal multi-reviewer
sign-off requirement during the alpha.

---

<sub>Portions of this documentation were drafted with assistance from Claude Opus 4.8 (Anthropic), July 2026.</sub>
