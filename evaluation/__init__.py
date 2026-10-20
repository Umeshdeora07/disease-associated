"""GE-EHR evaluation harness (SPEC §10, paper §5).

``evaluation.cohort`` (Cohort work package) provides the synthetic patient cohorts — Synthea CSV
conversion, the compact cohort files in ``data/cohorts/``, a deterministic pure-Python generator,
family grouping and the patient VCF writer. The knowledge changes, strategies, metrics and the
runner (``python -m evaluation.run``) belong to the Eval work package.
"""
