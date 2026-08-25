# Ditau production automation

This page records the developing `law` graph for the HiggsDNA ditau production. The initial test
scope is deliberately limited to CP samples and `Run3_2022`. Sample discovery can access dCache,
but no task currently executes an analysis command or submits jobs.

## Initial production configuration

`config/productions.json` defines:

- Production: `cp_2022_test`
- Analysis type: CP
- Era: `Run3_2022`
- Channels for analysis, ROOT conversion, and merging: `tt`, `et`, `mt`
- Output: `output/cp_2022_test`

Additional eras will be appended to the `eras` list only after the 2022 workflow is validated.

## Grid credential prerequisite

Creating a CMS proxy is interactive and remains a manual prerequisite:

```bash
source /vols/grid/cms/setup.sh
voms-proxy-init --rfc --voms cms --valid 192:00 --out "${HOME}/cms.proxy"
export X509_USER_PROXY="${HOME}/cms.proxy"
```

Check it before any sample discovery or batch operation:

```bash
voms-proxy-info --exists --valid 5:00
voms-proxy-info --path
```

WorkflowAutomation must never create, renew, copy, or print proxy credentials automatically.

## Generate the safe production plan

```bash
law run workflow_automation.tasks.DitauProductionPlan \
  --production cp_2022_test \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

This single command first ensures that the configured HiggsDNA revision is checked out and its
isolated Python environment is installed and valid. `law` skips either prerequisite when it is
already complete. It can therefore clone HiggsDNA or create its environment, but it does not access
physics input data or submit analysis jobs.

This writes:

```text
WORKSPACE/.workflow_automation/productions/cp_2022_test/
├── plan.json
└── analysis-configs/
    ├── Run3_2022__tt.json
    ├── Run3_2022__et.json
    └── Run3_2022__mt.json
```

The source HiggsDNA analysis JSON files are not modified. Generated configurations point to the
workflow-owned sample manifests described below, rather than HiggsDNA's checked-in sample JSONs.
`plan.json` contains argument arrays, not shell strings, and labels submission commands with
`submits_jobs: true`. It also contains the HiggsDNA commit and an input fingerprint. The planning
task never executes these commands and sets `submission_enabled` to `false`.

## Prepare production inputs

Inspect the implemented chain without running it:

```bash
law run workflow_automation.tasks.DitauInputPreparation \
  --production cp_2022_test \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --print-deps -1
```

After manually creating and exporting the proxy, run the input stage:

```bash
law run workflow_automation.tasks.DitauInputPreparation \
  --production cp_2022_test \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

This executes sample discovery against dCache but does not submit jobs. It writes outside the
HiggsDNA Git checkout:

```text
WORKSPACE/.workflow_automation/productions/cp_2022_test/sample-manifests/Run3_2022/
├── manifest.json
└── samples/
    ├── samples_MC.json
    ├── samples_tt.json
    ├── samples_et.json
    └── samples_mt.json
```

`manifest.json` records the analysis type, HiggsDNA commit, input fingerprint, generation time, and
SHA-256 hash of every output. Completion requires all expected files and matching hashes. Changes to
the production channels, analysis type, era sample definition, discovery script, or HiggsDNA commit
make the task stale. Strict discovery fails without writing new manifests when `gfal-ls` fails or a
configured sample directory contains no ROOT files.

Manual equivalent, run from the HiggsDNA checkout with its environment Python:

```bash
../.environments/HiggsDNA/bin/python \
  scripts/ditau/pre_processing/fetch_samples.py \
  --year Run3_2022 \
  --analysis-type cp \
  --output-dir ../.workflow_automation/productions/cp_2022_test/sample-manifests/Run3_2022/samples \
  --strict
```

The alternatives `mssm` and `all` remain available in HiggsDNA, while this production is configured
for CP. WorkflowAutomation never creates or renews the proxy.

## Exact submission-directory records

HiggsDNA's Imperial submitter inserts the submission date into its internal job path. When future
submission tasks pass `--submission-manifest-dir`, every successful submission will atomically write
a record such as:

```text
25_08_2026__Run3_2022__mt__Events.json
```

The record includes the requested output, UTC submission timestamp, generated date component,
era, channel, tree, exact job root, jobs directory, and submit files. A failed `condor_submit` raises
an error and does not write a record. Status and resubmission tasks will consume these records rather
than reconstructing paths from the current date.

## Implemented dependency graph

```text
DitauInputPreparation(cp_2022_test)
├── DitauProductionPlan(cp_2022_test)
│   └── RepositoryEnvironment(HiggsDNA)
│       └── RepositoryCheckout(HiggsDNA)
└── DitauSampleManifest(cp_2022_test, Run3_2022)
    ├── RepositoryEnvironment(HiggsDNA)
    │   └── RepositoryCheckout(HiggsDNA)
    └── GridCredentialCheck
```

`GridCredentialCheck` is read-only. It requires `voms-proxy-info --exists --valid 5:00` to succeed
and otherwise stops with manual proxy instructions.

The next planned chain after `DitauSampleManifest` is:

```text
EffectiveEventSubmission
└── EffectiveEventCollection
    └── StitchingConfiguration
        └── ParameterConfiguration
            └── StandardAnalysis(channel)
                └── ROOTConversion(channel)
                    └── ROOTMerge(channel)
```

None of these tasks is implemented yet. In particular, effective-event submission remains outside
the enabled workflow.

Job checking and resubmission will be distinct tasks. A status check must remain read-only;
resubmission will require an explicit operator opt-in.

## Manual command reference

The student's current 2022 analysis commands correspond to three independently planned channel
commands. The future execution tasks will use generated per-channel configurations and avoid
`--run_all_years`:

```bash
python scripts/ditau/processing/run.py \
  --json-analysis GENERATED_CONFIG.json \
  --output output/cp_2022_test \
  --step standard \
  --batch \
  --channels mt \
  --submission-manifest-dir MANIFEST_DIRECTORY
```

The equivalent commands for `et` and `tt` are separate task branches. This makes failures
independently retryable and allows eras to be added through configuration later.
