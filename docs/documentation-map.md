# Documentation map

Updated: 26 August 2026

The group's workflow is split across several repositories, and its documentation is sparse,
scattered, and sometimes stale. This file is the running inventory of what documentation exists,
where it lives, what it actually covers, and where the holes are. It is maintained as we go so it
can later be the source list for a single aggregated documentation site.

## Conventions

- Documentation improvements to **external** repositories go on a branch named `workflowautomation`
  in that repository. The HiggsDNA fork already follows this.
- WorkflowAutomation is our own repository and continues to work on `main`.
- When a document is added or materially changed anywhere, add or update its row here in the same
  change, so this file never drifts behind the repositories it indexes.

## Repository inventory

| Repository | Role | Canonical remote | Branch in use |
| --- | --- | --- | --- |
| WorkflowAutomation | LAW orchestration of the whole chain | <https://github.com/danielwinterbottom/WorkflowAutomation> | `main` |
| HiggsDNA (fork) | Step 1, NanoAOD analysis | <https://gitlab.cern.ch/dwinterb/HiggsDNA> | `workflowautomation`, pinned `4ccff7f3` |
| TIDAL | Steps 2 and 5, control plots and datacards | <https://github.com/Imperial-HiggsTauTau-Group/TIDAL> | see note below |
| REAL (MUFFIN) | Step 3, jet-to-tau background modelling | <https://github.com/IreneAndreou/REAL> | `main` only |
| Inference | Step 6, statistical inference | not yet identified | not yet identified |

TIDAL note: a fork, <https://github.com/Ksavva1021/TIDAL>, is also in active use, and the two are
not obviously in sync. TIDAL carries a submodule `TauAnalysis/ClassicSVfit` from
<https://github.com/Ksavva1021/ClassicSVfit> on branch `pywrapper`.

## Existing documents

### WorkflowAutomation

| Document | Covers |
| --- | --- |
| [`README.md`](../README.md) | Entry point and controller setup |
| [`docs/project-roadmap.md`](project-roadmap.md) | The three delivery stages |
| [`docs/workflow-steps.md`](workflow-steps.md) | Automated tasks and their manual equivalents |
| [`docs/operator-guide.md`](operator-guide.md) | Operating and recovery policy |
| [`docs/ditau-production.md`](ditau-production.md) | CP ditau graph, fingerprints, submission safeguards |
| [`docs/documentation-map.md`](documentation-map.md) | This inventory |

### HiggsDNA fork

| Document | Covers | Status |
| --- | --- | --- |
| `scripts/ditau/Instructions.md` | **The primary ditau workflow document.** Grid proxy setup, sample discovery, the effective-event sequence, resubmission, stitching, the standard workflow, merging, classifier and fake-factor application, parquet-to-ROOT conversion | Current and the most useful document in the chain; the natural spine for the aggregated site |
| `README.md` | Two lines: points at the upstream diphoton readthedocs and at `scripts/ditau/Instructions.md` | Thin, but the pointer is correct |
| `examples/ditau_analysis/example.md` | Running a single ditau selection locally with `run_analysis.py` | Useful for a first local test |
| `docs/lxplus_submission/README_ditau_lxbatch.md` | The opt-in `lxbatch` executor path, kept separate from the default `imperial_condor` | Current and unusually well written |
| `docs/source/*.rst` | Upstream Sphinx sources | Diphoton-oriented upstream material; does not describe the ditau workflow |
| `scripts/postprocessing/README.md` | Upstream diphoton postprocessing | Not part of the ditau chain |

Upstream project documentation lives at <https://higgs-dna.readthedocs.io>. It documents the
Higgs-to-diphoton framework, not our ditau usage, so it should be linked as background only.

#### Naming gotcha: the `_PNet` suffix

`Tau_ID` and `Tau_ID_PNet`, and likewise `Tau_EnergyScale` and `Tau_EnergyScale_PNet`, both apply
**DeepTau2018v2p5** scale factors. The suffix refers to the decay-mode reconstruction the scale
factors are binned against, not to the identification discriminant:

| Correction | Scale-factor source | Binned for |
| --- | --- | --- |
| `Tau_ID` | `JSONs/TauID/<era>/tau_DeepTau2018v2p5_<era>.json.gz` | HPS decay modes |
| `Tau_ID_PNet` | `JSONs/TauID/PNet_CP/tau_sf_pt-dm_DeepTau2018v2p5VSjet_<era>.json.gz` | ParticleNet decay modes |

The CP ditau selection reconstructs decay modes with ParticleNet, selecting on `taus.decayModePNet`
in `higgs_dna/selections/ditau/lepton_selections.py`, so it needs the `_PNet` variants. The
`_PNet` scale factors exist for Run3_2022 through Run3_2023BPix only and require the `VTight` VSjet
working point; `Run3_2024` has HPS-binned scale factors only, which is why
`ditau_analysis_2024.json` is the one config using the unsuffixed names.

The names read as though they select the tau ID, which they do not. This has already caused one
misdiagnosis and is worth stating wherever these corrections are configured.

### TIDAL

| Document | Covers | Status |
| --- | --- | --- |
| `README.md` | Environment creation, Draw/Multidraw setup, SVFit setup and use, and the CP datacard commands (`makeDatacards.py`, `hadd_cp_datacards.py`) | Covers setup and datacards; see the gap below |
| `Tools/ImpactParameter/README.md` | Impact-parameter tool | Not yet reviewed |
| `Tools/IPCorrection/README.md` | Impact-parameter corrections | Not yet reviewed |

### REAL

REAL stands for "Reweighting Events using Adaptive Learning". It improves the modelling of
jet-to-tau-hadronic backgrounds using BDTs, in place of traditional fake-factor methods.

| Document | Covers | Status |
| --- | --- | --- |
| `README.md` | The whole package: repository layout, three Conda environments, the ROOT dependency, the raw-ntuple-to-parquet data pipeline, BDT training with Optuna, plotting and non-closure checks, and the HTCondor bootstrap machinery for statistical uncertainties | The only document in the repository, but detailed and apparently current. Good raw material for the aggregated site |

Operational notes on REAL:

- `main` is the only branch; at the time of writing HEAD is `33be0a8`.
- The repository is large, over 600 MB even for a shallow clone, because it carries preprocessed
  parquet inputs in `data_January26/` and `data_with_pileup_January26/`. Do not clone it into a
  small filesystem; `/tmp` on `lx06` is under 1 GB and a shallow clone exhausts it.
- `TAU-25-001/` preserves the trainings, plots, and classical fake factors used for the
  CMS-TAU-25-001 paper.

## Documentation coverage by workflow step

| Step | Where it is documented | Assessment |
| --- | --- | --- |
| 1. Run HiggsDNA on NanoAOD | `scripts/ditau/Instructions.md` steps 1-8 | Good |
| 2. TIDAL data-versus-background control plots | Nothing found | **Gap.** The TIDAL README documents datacard production but not the control-plot path. The plotting entry points appear to be `Draw/scripts/HiggsTauTauPlot.py` and configs such as `Draw/scripts/mssm_search/config_plot.yaml`, which are undocumented |
| 3. Train MUFFIN and classifiers | REAL `README.md` | Reasonable for REAL itself. What is missing is the seam: how REAL consumes HiggsDNA output and how its trained models get back into the chain |
| 4. Apply models to outputs | `scripts/ditau/Instructions.md` steps 9-11 | Documented for application, but the classifiers are described only as "currently trained for channel X"; provenance of the trained models is undocumented |
| 5. TIDAL datacards | TIDAL `README.md`, CP Analysis Instructions section | Present but brief; CP-specific and does not generalise |
| 6. Statistical inference | Nothing found | **Gap.** No repository identified yet |
| Systematic variations | `Draw/scripts/systematics/systematics.py` exists in TIDAL | **Gap.** No prose documentation found |

## Open questions

1. **What is the relationship between REAL, MUFFIN, and TIDAL?** The repository is named REAL and
   its README never uses the word MUFFIN, yet the group refers to the method as MUFFIN, and TIDAL
   contains "MUFFIN methods" added in commit `bb8a557` on the `mssm_datacards` branch, touching
   `Draw/python/nodes.py` and `Draw/scripts/mssm_search/config_plot.yaml`. Whether TIDAL reimplements
   the method, imports it, or merely consumes REAL's outputs determines how Stage 2 of the roadmap
   is structured. This is the single most valuable thing to clarify for the documentation.
2. **Which TIDAL remote is authoritative for our work?** The canonical group repository and the
   `Ksavva1021` fork are both checked out locally on different branches.
3. **What runs step 6?** Presumably Combine, but the repository, configuration, and entry point are
   not yet identified.
4. **Where do the trained classifier and fake-factor models referenced in `Instructions.md`
   steps 9-11 come from?** Their training code and provenance are undocumented, and it is not yet
   clear whether they are REAL outputs.

## Local checkouts observed on lx06

These are working areas, recorded so the documentation survey can be reproduced. They are not
managed by WorkflowAutomation.

| Path | Repository | Branch |
| --- | --- | --- |
| `/vols/cms/dw515/WorkflowAutomation/workspaces/HiggsDNA` | HiggsDNA fork (workflow-managed) | `workflowautomation` |
| `/vols/cms/dw515/TIDAL_mssm/TIDAL` | Imperial-HiggsTauTau-Group/TIDAL | `mssm_datacards` |
| `/vols/cms/dw515/Run3_CP_workareas/TIDAL` | Ksavva1021/TIDAL | `main` |
| `/vols/cms/dw515/Run3_CP_workareas/TIDAL_fordatacards/TIDAL` | not a Git checkout | n/a |

No REAL checkout exists on this machine.
