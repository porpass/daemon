# Contributing

Thanks for your interest in improving the PORPASS processing daemon. Please open
an issue to discuss any substantial change before starting work, so we can agree
on the approach.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md) — by participating,
you agree to uphold it.

## Development setup

```sh
conda env create -f environment.yml
conda activate porpass-proc
pip install -e '.[dev]'
pytest
```

See [README.md](README.md) for how to run the daemon and
[deploy/README.md](deploy/README.md) for deployment.

## Pull request process

1. Run the test suite (`pytest`) and make sure it passes; add or update tests to
   cover your change.
2. Update the docs for any user-facing change — new environment variables,
   configuration, or operational behavior belong in `README.md` and/or
   `deploy/README.md`.
3. Add a `CHANGELOG.md` entry, and bump the version in `pyproject.toml` and
   `src/porpass_daemon/__init__.py` when a change warrants a release. Versioning
   follows [PEP 440](https://peps.python.org/pep-0440/).
4. Open the pull request against `main`; a maintainer will review and merge.

## Reporting security issues

Please do not open public issues for security vulnerabilities — see
[SECURITY.md](SECURITY.md) for how to report them privately.
