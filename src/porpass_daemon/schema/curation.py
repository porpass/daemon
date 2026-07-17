"""Web-form curation for the Contract A schema artifact.

This is the presentation layer of the schema: display orderings, dropdown
short-lists, visibility rules, human-readable default hints, stage titles, and
per-combo input-file requirements. None of it is science — it exists so the
PORPASS web app can render a job form — which is why it lives here and not in
``grasp``.

GRaSP remains the source of truth for *correctness*: which stages exist, which
values its validator accepts, and the per-(instrument, product) support matrix.
Those come from ``grasp.introspection``. Everything in this module is the
daemon's own editorial layer on top.

Values were lifted verbatim from GRaSP's former ``grasp.schema`` module when the
generator moved here; keep them in sync with GRaSP's behaviour by way of the
byte-identity regression test in ``tests/test_schema_generate.py``.
"""

from __future__ import annotations

from typing import Any

# Human-friendly stage titles for form headers, keyed by short stage attribute.
STAGE_TITLES: dict[str, str] = {
    "preprocessing":     "Preprocessing",
    "range_compression": "Range Compression",
    "emi_suppression":   "EMI Suppression",
    "iono_comp":         "Ionospheric Compensation",
    "sar":               "SAR Processing",
    "mlk":               "Multilooking",
    "csim":              "Clutter Simulation",
    "general":           "General",
    "output":            "Output Parameters",
    "plots":             "Plot Parameters",
    "final_output":      "Final Output",
}

# Fields whose visibility in the form depends on the value of a sibling.
VISIBLE_WHEN: dict[tuple[str, str], dict[str, Any]] = {
    ("range_compression", "window_alpha"): {"field": "window",           "equals": "TUKEY"},
    ("sar",               "window_alpha"): {"field": "window",           "equals": "TUKEY"},
    ("mlk",               "window_alpha"): {"field": "window_type",      "equals": "TUKEY"},
    ("iono_comp",         "window_alpha"): {"field": "window",           "equals": "TUKEY"},
    ("emi_suppression",   "interp_pad"):   {"field": "replace_strategy", "equals": "INTERP"},
    ("iono_comp",         "contrast_L_eq"):    {"field": "method", "equals": "CONTRAST"},
    ("iono_comp",         "campbell_b"):       {"field": "method", "equals": "CAMPBELL"},
    ("iono_comp",         "campbell_n_phase"): {"field": "method", "equals": "CAMPBELL"},
    ("iono_comp",         "campbell_delta"):   {"field": "method", "equals": "CAMPBELL"},
    ("iono_comp",         "sgn"):              {"field": "method", "equals": "CAMPBELL"},
    ("sar",               "number_of_looks"):  {"field": "method", "equals": "BACKSCATTER"},
}

# Stage-level: selecting this value for this field disables the listed stages.
STAGE_DISABLES: dict[tuple[str, str, str], list[str]] = {
    ("sar", "method", "BACKSCATTER"): ["multilooking"],
}

# Human-readable descriptions of runtime-computed defaults. Fields that get no
# value from the per-instrument defaults table are emitted with
# ``"default": null`` plus one of these hints, so the form can show a
# placeholder instead of an empty box.
DEFAULT_NOTES: dict[tuple[str, str], str] = {
    ("general",       "out_dir"):         "supplied by user at job-load time",

    ("preprocessing", "n_center"):        "n_samp // 2",

    ("iono_comp",     "metric"):          "PEAK_SNR (Campbell) / L4 (Contrast)",
    ("iono_comp",     "n_take"):          "8192 // presum (SHARAD); method default otherwise",

    ("sar",           "lamb"):            "C / RadarParams.f_cen[0]",
    ("sar",           "aperture_length"): "computed from geometry",
    ("sar",           "sgn"):             "+1 (SHARAD mix-up); -1 elsewhere",

    ("csim",          "target"):          "instrument SPICE target",
    ("csim",          "bin_size"):        "RadarParams.dt",
    ("csim",          "n_samples"):       "instrument-derived (n_samp or nfft)",
    ("csim",          "n_center"):        "n_samples // 2",
    ("csim",          "at_step"):         "computed from geometry when SAR is enabled",
    ("csim",          "at_dist"):         "computed from geometry when SAR is enabled",
    ("csim",          "dem_path"):        "supplied by user at job-load time",
}

# Per-(instrument, product) input-file requirements. Emitted under ``inputs`` so
# the web knows which upload widgets to show, and so the daemon's fetcher knows
# which roles to resolve.
INPUT_FILES: dict[tuple[str, str], tuple[str, ...]] = {
    ("SHARAD", "EDR"):    ("label", "science", "auxiliary"),
    ("SHARAD", "RDR"):    ("label", "science", "auxiliary"),
    ("SHARAD", "US_RDR"): ("label", "science", "auxiliary"),
    ("MARSIS", "EDR"):    ("label", "science", "auxiliary"),
    ("MARSIS", "RDR"):    ("label", "science", "auxiliary"),
    ("LRS",    "EDR"):    ("label", "science"),
    ("LRS",    "RDR"):    ("label", "science"),
}

INPUT_HELP: dict[str, str] = {
    "label":     "PDS label (PDS3/PDS4)",
    "science":   "PDS science file",
    "auxiliary": "PDS auxiliary file",
}

TARGET_BODY: dict[str, str] = {
    "SHARAD": "MARS",
    "MARSIS": "MARS",  # Phobos/transit MARSIS observations aren't supported yet
    "LRS":    "MOON",
}

# Canonical display order for window-typed fields. These are the values the web
# form advertises, in this order. Every other name GRaSP's validator accepts is
# emitted as a hidden alias (see WINDOW_ALIASES).
WINDOW_DISPLAY_ORDER: tuple[str, ...] = (
    "RECTANGLE",
    "HANN",
    "HAMMING",
    "BLACKMAN",
    "BLACKMAN-HARRIS",
    "NUTTALL",
    "BARTLETT",
    "COSINE",
    "FLATTOP",
    "TUKEY",
)

# Non-canonical -> canonical mapping for window aliases. Mirrors the
# equivalence classes GRaSP's window builder accepts.
WINDOW_ALIASES: dict[str, str] = {
    "NONE":            "RECTANGLE",
    "HANNING":         "HANN",
    "BLACKMAN_HARRIS": "BLACKMAN-HARRIS",
    "BH":              "BLACKMAN-HARRIS",
    "SINE":            "COSINE",
    "RAISED_COSINE":   "COSINE",
}

# Curated matplotlib colormap short-list, kept small so the web renders a
# manageable dropdown.
#
# STAND-BY — not yet consulted. Through grasp 0.6.0a1, GRaSP's
# CHOICES[("plots", "cmap")] still carries this same set, so the generator
# resolves cmap through ``grasp.introspection.get_choices`` like every other
# enum. In GRaSP Phase 3 (~0.6.0a2) that entry is dropped, ``get_choices``
# returns None for cmap, and this constant becomes authoritative — at which
# point the generator must fall back to it for ("plots", "cmap").
#
# ``tests/test_schema_generate.py::test_cmap_still_owned_by_grasp_tripwire``
# fails the moment GRaSP drops the entry, so the hand-off is loud rather than a
# silent degradation of cmap from an enum to a free-text field.
CMAP_NAMES: frozenset[str] = frozenset({
    "gray", "gray_r", "viridis", "plasma", "inferno", "magma", "cividis",
    "hot", "jet", "turbo", "bwr", "seismic", "coolwarm", "RdBu", "RdBu_r",
    "cubehelix",
})
