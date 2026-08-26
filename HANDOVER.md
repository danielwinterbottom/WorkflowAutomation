# WorkflowAutomation handover

Updated: 26 August 2026

## Goal

Build modular LAW pipelines for the group's standard analysis workflow:

1. Run HiggsDNA on NanoAOD.
2. Produce TIDAL data-versus-background control plots and quality metrics.
3. Train MUFFIN background models and/or signal-versus-background classifiers.
4. Apply the models and augment the analysed outputs.
5. Produce TIDAL datacards.
6. Run statistical inference.

Systematic variations must be optional production branches. Nominal-only processing should remain
the quick default during early development. The staged roadmap is in
[`docs/project-roadmap.md`](docs/project-roadmap.md).

## Repository and cluster locations

- WorkflowAutomation: <https://github.com/danielwinterbottom/WorkflowAutomation>
- WorkflowAutomation branch: `main`
- WorkflowAutomation implementation baseline before this handover document: `4e32667`
- HiggsDNA fork: <https://gitlab.cern.ch/dwinterb/HiggsDNA>
- HiggsDNA branch: `workflowautomation`
- Pinned HiggsDNA commit: `4ccff7f3de82480a35b578499f26ff08258db274`
- Cluster checkout: `/vols/cms/dw515/WorkflowAutomation`
- Durable workspace: `/vols/cms/dw515/WorkflowAutomation/workspaces`
- Controller environment: `/vols/cms/dw515/WorkflowAutomation/.venv`
- HiggsDNA environment: `workspaces/.environments/HiggsDNA`
- HiggsDNA checkout: `workspaces/HiggsDNA`

All development edits are made locally, committed, and pushed. The cluster receives changes with
`git pull`; do not edit either cluster checkout manually.

## Current test production

`config/productions.json` defines `cp_2022_test`:

- analysis type: `cp`
- era: `Run3_2022` only
- channels: `tt`, `et`, `mt`
- standard output: `output/cp_2022_test`
- effective-event output: `output/effective/cp_2022_test`
- input snapshot label: `2026-08-25`

The snapshot label is a manual invalidation mechanism for remote contents changing in place. Path,
sample-definition, discovery-code, channel, analysis-type, era, or HiggsDNA-commit changes are
fingerprinted automatically.

## Implemented LAW graph

- `RepositoryCheckout`: exact branch/commit checkout with clean-state and origin checks.
- `RepositoryEnvironment`: creates the repository environment and validates runtime imports.
- `GridCredentialCheck`: read-only check for an existing CMS proxy valid for at least five hours.
- `DitauSampleManifest`: strict dCache discovery plus hashed, workflow-owned sample manifests.
- `DitauProductionPlan`: immutable, non-executing standard-analysis command plan.
- `DitauInputPreparation`: production plan plus per-era sample discovery.
- `DitauEffectiveEventPlan`: immutable `Events` and `EventsNotSelected` configurations and commands.
- `DitauEffectiveEventReadiness`: validates all prerequisites without submission.
- `DitauEffectiveEventSubmission`: explicitly gated submission of one tree.
- `DitauEffectiveEventSubmissions`: wrapper for both trees, intended with one worker.

Post-submission status, resubmission, YAML aggregation, standard analysis, ROOT conversion/merge,
TIDAL, MUFFIN, datacards, and inference are not implemented yet.

## Environment decisions and fixes

The HiggsDNA fork uses Python 3.11 and the known student stack including Coffea 0.7.22 and Uproot
4.3.7. Uproot imports the deprecated `pkg_resources` API, so Setuptools is pinned to 70.1.1.

Conda owns all dependencies from `environment.yml`. WorkflowAutomation installs only the editable
HiggsDNA source with pip using `--no-deps --no-build-isolation`. This prevents pip from replacing
Conda packages or downloading a newer PyTorch. Setuptools 70.1.1 is pinned in both the Conda list
and nested `pip:` list because `conda env create` invokes a separate pip resolver.

Validated cluster versions:

- Python 3.11.16
- Setuptools 70.1.1
- Uproot 4.3.7
- Coffea 0.7.22
- `higgs_dna` imports from the managed checkout

Old environment backups currently exist under `workspaces/.environments/`; do not remove them until
the user deliberately decides cleanup is safe.

## Grid setup on the cluster

The site script currently does not put `voms-proxy-info` on `PATH` on `lx06`. The working session
setup was:

```bash
cd /vols/cms/dw515/WorkflowAutomation
source .venv/bin/activate
source /vols/grid/cms/setup.sh
export X509_USER_PROXY="${HOME}/cms.proxy"
VOMS_BIN=/cvmfs/grid.cern.ch/alma9-ui-test/usr/bin
export PATH="$VOMS_BIN:$PATH"
hash -r
```

Verify without exposing credential contents:

```bash
voms-proxy-info --file "$X509_USER_PROXY" --exists --valid 5:00
condor_q "$USER"
```

Never create or renew a proxy automatically. The operator does that manually.

## Important bugs fixed

1. Editable pip installation upgraded the Conda dependency stack. Fixed by disabling pip dependency
   resolution for HiggsDNA.
2. Setuptools 84 removed `pkg_resources`, breaking Uproot 4.3.7. Fixed by pinning Setuptools 70.1.1
   for both Conda and its nested pip transaction.
3. HiggsDNA `fetch_samples.py` successfully discovered the special EWKZ files but immediately
   overwrote them with `None`. Fixed at HiggsDNA commit `4ccff7f3` by assigning the result to
   `paths`. The regenerated Run3 2022 EWKZ sample now contains four ROOT files.
4. WorkflowAutomation previously accepted any JSON object as a sample manifest. It now requires a
   non-empty object whose datasets each map to a non-empty list of non-empty strings.
5. Submission failures previously survived only in terminal output. Failed intents now durably
   record `status`, `failed_at`, `error_type`, and `error` and continue to block automatic retry.

The WorkflowAutomation suite currently has 27 passing unit tests.

## Submission safety model

Planning and readiness never submit. Actual submission requires `--allow-submission`.

Before executing HiggsDNA, each per-tree task atomically writes a durable intent. A successful
submission must create or change exactly one HiggsDNA submission record before WorkflowAutomation
writes a receipt. Any intent without a valid receipt blocks retry. There is no automatic
resubmission.

Never move an unresolved intent until all of the following have been checked:

- its plan and command fingerprints;
- `submission-records`;
- `submission-receipts`;
- `condor_q "$USER"`;
- the original or durably recorded error.

Reconciled intents are archived under `effective-events/Run3_2022/reconciled-intents/`, not deleted.

## Current cluster state

The latest `DitauEffectiveEventReadiness` completed successfully after the EWKZ fix. The regenerated
`samples_MC.json` has 51 datasets, with the EWKZ dataset now containing four valid ROOT paths.

No Condor jobs or submission records existed at the last check. `EventsNotSelected` has not been
submitted. Several prior failed intents were archived after confirming zero jobs and zero records.

There is one active failed `Events` intent created before the EWKZ fix:

```text
workspaces/.workflow_automation/productions/cp_2022_test/effective-events/
Run3_2022/submission-intents/Events.json
```

It reports zero new/changed submission records and belongs to the prior malformed plan. At that
time `condor_q "$USER"` showed zero jobs and neither `submission-records` nor
`submission-receipts` existed. It should be archived under a clearly named reconciliation directory
before the next attempt, after rechecking those facts.

## Exact next steps

In the same grid-enabled cluster shell:

1. Inspect the active intent, current readiness fingerprint, records, receipts, and Condor queue.
2. Confirm the active intent belongs to the old plan and there are still no jobs or records.
3. Archive that intent; do not delete it.
4. Retry only the `Events` tree, not the two-tree wrapper.
5. Inspect the durable intent, receipt, submission record, and Condor queue before considering
   `EventsNotSelected`.

The real submission command, only after reconciliation, is:

```bash
law run workflow_automation.tasks.DitauEffectiveEventSubmission \
  --production cp_2022_test \
  --era Run3_2022 \
  --tree Events \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission \
  --local-scheduler
```

This command can genuinely submit Condor jobs. Do not retry automatically if it fails.

## Documentation map

- [`README.md`](README.md): entry point and controller setup.
- [`docs/project-roadmap.md`](docs/project-roadmap.md): full three-stage project goal.
- [`docs/workflow-steps.md`](docs/workflow-steps.md): automated and manual equivalents.
- [`docs/operator-guide.md`](docs/operator-guide.md): operating and recovery policy.
- [`docs/ditau-production.md`](docs/ditau-production.md): CP ditau graph, fingerprints, readiness,
  and submission safeguards.

## Notes for the next assistant

- Read the documentation and inspect current Git/cluster state before changing anything.
- Preserve user changes and generated state.
- Keep repository setup generic; not every future workflow will use HiggsDNA.
- Keep repository-specific execution in repository-specific tasks and connect them through higher
  level pipelines.
- Add systematics as explicit optional graph branches, not hidden behavior.
- Prefer read-only checks before any operation that submits, resubmits, moves, or deletes state.
- Continue the one-era `Run3_2022` test until the complete path is validated.
