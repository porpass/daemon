# Schema generator extraction — report for the grasp channel

The Contract A JSON-schema generator now lives in `porpass/daemon`, consuming
GRaSP **entirely through `grasp.introspection`**. Nothing imports
`grasp.schema.*`.

## Where things landed

| What | Path (porpass/daemon) |
|---|---|
| Generator (`build_schema`, `export_schema`, `choice_entries`) | `src/porpass_daemon/schema/generate.py` |
| Curation (`VISIBLE_WHEN`, `STAGE_DISABLES`, `DEFAULT_NOTES`, `INPUT_FILES`, `INPUT_HELP`, `TARGET_BODY`, `STAGE_TITLES`, `WINDOW_DISPLAY_ORDER`, `WINDOW_ALIASES`, `CMAP_NAMES`) | `src/porpass_daemon/schema/curation.py` |
| Docstring parser (`stage_help`, `field_help`) | `src/porpass_daemon/schema/docstrings.py` |
| Regression + generator tests | `tests/test_schema_generate.py` |

`porpass_daemon.schema` became a package; public API unchanged. Startup publishing
generates in-process — the `grasp export-schema` shell-out is gone. New CLI:
`porpass-daemon --regenerate-schema --out <dir>`.

## Regression test: passing

`test_byte_identical_to_grasp_export_schema` diffs our output against
`grasp export-schema` for all 7 combos, masking only `generated_at`.
**Byte-identical against grasp @ `5e5aa1a`.** Full suite: **86 passed**.

## Your three fixes: adopted

- **`get_defaults` / `MetaDataInfo`** — now imported from `grasp.introspection`.
  The daemon no longer touches `grasp.processing_defaults` or `grasp.grasp_types`.
- **`WINDOW_FIELDS`** — replaced the `CHOICES.get(key) is WINDOW_NAMES` identity
  check with `key in WINDOW_FIELDS`. Identity-fragility caveat dropped from our
  code. (Verified it matches the old identity-derived set exactly.)
- Only remaining off-`introspection` reference is `grasp.__version__` (top-level,
  stable) for the artifact's `grasp_version`. Fine as-is — flagging only because
  it's contractual in practice.

## CMAP_NAMES: stand-by + tripwire, as agreed

`curation.CMAP_NAMES` stays commented/unused for `0.6.0a1`; `get_choices` still
supplies cmap. Added `test_cmap_still_owned_by_grasp_tripwire`, which **fails
loudly** the moment you drop `("plots","cmap")` from `CHOICES` — otherwise cmap
would silently degrade from an enum to a free-text field. It also asserts our
copy hasn't drifted from yours while you still own it.

## Version pin: holding, as instructed

`pyproject.toml` does **not** yet pin `grasp`. The `v0.6.0a1` tag isn't cut yet
(tags present: `v0.5.0`, `v0.5.1`), and pinning now would also break
`conda env create`, which runs `pip install -e .` before GRaSP is installed.
We'll add `grasp >= 0.6.0a1` once the tag is published.

Verified against merged `main` @ `cee149b`: **86 passed**, byte-identical to
`grasp export-schema`. (An earlier draft of this report claimed the surface was
only on `feature/decouple-schema-export` — that was stale; it has since been
merged.)

## ⚠️ `grasp.__version__` is stale — artifacts are stamped with the wrong version

`src/grasp/__init__.py` derives its version from installed distribution
metadata:

```python
__version__ = version("grasp")   # importlib.metadata
```

The editable install's metadata is still `grasp-0.5.1.dist-info`, recorded when
GRaSP was installed at `0.5.1`. Bumping `pyproject.toml` to `0.6.0a1` does **not**
refresh it. So right now:

```
pyproject.toml        -> 0.6.0a1
grasp.__version__     -> 0.5.1      <-- what we stamp
artifact grasp_version-> 0.5.1      <-- what the web reads
```

Every artifact we publish is therefore mislabelled, and `grasp_version` is the
field the web uses to confirm forms match the running GRaSP. **Fix before cutting
the alpha:** re-run `pip install -e . --no-deps` for GRaSP in every env where
it's editable-installed (notably `porpass-proc`), so the dist-info refreshes to
`0.6.0a1`. Nothing in the daemon needs to change — we read `grasp.__version__`
faithfully; it just has to be telling the truth.
