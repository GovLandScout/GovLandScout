"""
GovLandScout model - Supported states

One place naming which states this pipeline runs for, instead of "TX"
hardcoded across fetch_data.py/build_dataset.py/train_model.py/
generate_predictions.py separately. Adding a state means adding an
entry here plus running the pipeline for it (see model/README.md) --
nothing about the modeling code itself is state-specific, it was only
ever the state filter and county-boundary source that were.
"""

STATES = {
    "tx": {"name": "Texas", "abbrev": "TX", "fips": "48"},
    "pa": {"name": "Pennsylvania", "abbrev": "PA", "fips": "42"},
}
