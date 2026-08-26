# Project roadmap

WorkflowAutomation connects repository-specific LAW tasks into reproducible analysis pipelines.
Repository setup remains independent: a pipeline should require only the repositories used by its
selected stages.

## End-to-end workflow

1. Run the HiggsDNA analyser on NanoAOD samples.
2. Use TIDAL to produce data versus MC or data-driven-background control plots.
3. Train MUFFIN background-estimation models and/or signal-versus-background classifiers.
4. Apply trained models and add their outputs to the analysed events.
5. Use TIDAL to produce statistical-inference datacards.
6. Run statistical inference using those datacards.

Systematic variations form optional branches of the production graph. Productions default to a
quick nominal-only mode while being able to enable a declared set of variations later. Nominal and
systematic outputs must remain distinct and traceable to the production configuration.

## Delivery stages

### Stage 1: analysis and control plots

Automate HiggsDNA processing and TIDAL control-plot production, with an option to include
systematics. Produce a machine-readable plot-quality report, initially including metrics such as
chi-squared per degree of freedom, so a browser-based report can highlight poor data/model
agreement without owning the numerical calculation.

### Stage 2: machine learning

Automate MUFFIN background-model training and a signal-versus-background classifier, then apply
the trained models to Stage 1 outputs and record the resulting augmented datasets.

### Stage 3: inference

Automate TIDAL datacard production and the subsequent statistical-inference runs. Datacards and
fit results should retain provenance back to the exact upstream production, trained models, and
systematic configuration.

## Current scope

Current development is establishing the Stage 1 foundations: reproducible repository setup,
HiggsDNA input discovery and fingerprinting, effective-event preparation, and explicitly gated
batch submission. TIDAL integration follows after the HiggsDNA portion is validated end to end.
