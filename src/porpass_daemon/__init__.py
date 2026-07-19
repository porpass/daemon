"""PORPASS processing daemon (porpass-proc).

A standalone worker that claims queued GRaSP jobs from the porpass database,
renders each to a GRaSP ``job.toml``, runs GRaSP, and writes results + a
Contract C manifest back to shared storage. All coupling with porpass-web is
through the three frozen contracts and the shared filesystem.
"""

__version__ = "0.1.0a2"
