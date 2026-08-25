# Ditau production automation

This page records the developing `law` graph for the HiggsDNA ditau production. The initial test
scope is deliberately limited to CP samples and `Run3_2022`. No task currently executes the planned
commands or submits jobs.

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

This writes:

```text
WORKSPACE/.workflow_automation/productions/cp_2022_test/
├── plan.json
└── analysis-configs/
    ├── Run3_2022__tt.json
    ├── Run3_2022__et.json
    └── Run3_2022__mt.json
```

The source HiggsDNA analysis JSON files are not modified. `plan.json` contains argument arrays,
not shell strings, and labels submission commands with `submits_jobs: true`. It also contains the
HiggsDNA commit and an input fingerprint. The planning task never executes these commands and sets
`submission_enabled` to `false`.

## Sample discovery

HiggsDNA sample discovery is now non-interactive and defaults to CP:

```bash
python scripts/ditau/pre_processing/fetch_samples.py \
  --year Run3_2022 \
  --analysis-type cp
```

The alternatives `mssm` and `all` remain available. This command accesses dCache and writes channel
sample JSON files, so it is not part of the read-only planning task yet.

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

## Planned dependency graph

```text
GridCredentialCheck
└── SampleManifest(Run3_2022, cp)
    └── EffectiveEventSubmission(Run3_2022)
        └── EffectiveEventCollection(Run3_2022)
            └── StitchingConfiguration(Run3_2022)
                └── ParameterConfiguration(Run3_2022)
                    └── StandardAnalysis(Run3_2022, channel)
                        └── ROOTConversion(Run3_2022, channel)
                            └── ROOTMerge(Run3_2022, channel)
```

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
