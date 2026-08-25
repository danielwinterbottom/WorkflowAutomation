# Workflow steps: automated and manual

This is the canonical execution guide. Every implemented stage is shown both as a recommended
automated command and as its manual equivalent for debugging and recovery.

WorkflowAutomation has two deliberately separate environment layers:

1. The **controller environment** at `.venv/` contains `law`, Luigi, and WorkflowAutomation.
2. Each **repository environment** is independent under `workspaces/.environments/` and is created
   only when a task requires that repository.

Therefore a workflow unrelated to HiggsDNA does not clone HiggsDNA or create its environment.
Commands below run from the WorkflowAutomation repository root. Nothing implemented so far submits
jobs or invokes a scheduler.

## Overview

| Step | Purpose | Automated command | Changes state? |
| --- | --- | --- | --- |
| 1. Diagnose | Inspect the machine and workspace | `./workflow diagnose` | No |
| 2. Controller | Install `law` for orchestration | `./workflow setup-controller` | Creates `.venv/` if needed |
| 3. Checkout | Prepare one selected repository | `law run workflow_automation.tasks.RepositoryCheckout ...` | Clones only when absent |
| 4. Environment | Prepare one selected repository environment | `law run workflow_automation.tasks.RepositoryEnvironment ...` | Creates only when absent |

`RepositoryEnvironment` depends on `RepositoryCheckout`, so it is normally sufficient to run step
4. Steps 3 and 4 are generic: `--repository` selects an entry from
`config/repositories.json`.

## Prerequisites

- Python 3.9 or newer
- Git
- Network access to Python package sources for controller setup
- Mamba (preferred) or Conda for repository environments
- Access to the Git and package sources required by the selected repository

## Step 1: diagnose

### Automated

```bash
./workflow diagnose
```

For a cluster workspace and JSON output:

```bash
./workflow diagnose \
  --workspace /path/to/group/workspaces \
  --format json
```

This command is read-only. It reports system, tool, environment-indicator, filesystem, and
configured-checkout facts. It neither runs HTCondor tools nor prints environment-variable values.

### Manual equivalent

```bash
python3 --version
command -v git python3 law mamba conda condor_q condor_submit voms-proxy-info
test -r config/repositories.json
test -d workspaces && test -r workspaces
```

For each checkout that already exists:

```bash
git -C workspaces/HiggsDNA rev-parse --verify HEAD
git -C workspaces/HiggsDNA branch --show-current
git -C workspaces/HiggsDNA remote get-url origin
git -C workspaces/HiggsDNA status --porcelain
```

### Completion check

Diagnosis should exit successfully. Missing optional tools or repositories remain reported facts,
not automatic failures.

## Step 2: set up the WorkflowAutomation controller

This environment belongs only to WorkflowAutomation. It does not contain HiggsDNA and should stay
small enough to install on every development machine and cluster login area.

### Automated

```bash
./workflow setup-controller
```

The command creates `.venv`, installs this project editable, installs its declared `law` dependency,
and validates both imports. A valid existing controller is reused without reinstalling.

Activate it for subsequent `law` commands:

```bash
source .venv/bin/activate
```

### Manual equivalent

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --editable .
.venv/bin/python -c 'import law, workflow_automation'
```

### Completion check

```bash
.venv/bin/law --version
.venv/bin/python -c 'import workflow_automation'
```

If controller creation fails, `.venv` may be partial. Inspect the error and move the directory aside
manually before retrying. Automation never deletes or overwrites an invalid controller directory.

## Step 3: prepare a repository checkout with `law`

### Automated

After activating `.venv`:

```bash
law run workflow_automation.tasks.RepositoryCheckout \
  --repository HiggsDNA \
  --local-scheduler
```

On the cluster, select a durable workspace:

```bash
law run workflow_automation.tasks.RepositoryCheckout \
  --repository HiggsDNA \
  --workspace /path/to/group/workspaces \
  --local-scheduler
```

The configuration names both a branch and an exact approved commit. The task clones a missing
checkout at that commit. An existing checkout is complete only when its origin, branch, commit, and
clean working-tree state all match. A clean checkout behind the pin is fetched and fast-forwarded;
the task never resets, cleans, or overwrites local changes.

The dependency-free bootstrap command remains available for recovery before the controller exists:

```bash
./workflow bootstrap HiggsDNA
```

### Manual equivalent

Read current values from `config/repositories.json`, then run:

```bash
mkdir -p workspaces
git clone --no-checkout \
  --branch workflowautomation \
  --single-branch \
  https://gitlab.cern.ch/dwinterb/HiggsDNA.git \
  workspaces/HiggsDNA
git -C workspaces/HiggsDNA checkout --detach 39c5d1a9b7052cad51aaa36d3241b7949f61e693
git -C workspaces/HiggsDNA branch --force workflowautomation 39c5d1a9b7052cad51aaa36d3241b7949f61e693
git -C workspaces/HiggsDNA checkout workflowautomation
```

Validate without changing the checkout:

```bash
git -C workspaces/HiggsDNA rev-parse --verify HEAD
git -C workspaces/HiggsDNA branch --show-current
git -C workspaces/HiggsDNA remote get-url origin
git -C workspaces/HiggsDNA status --porcelain
```

### Completion check and recovery

```bash
test -d workspaces/HiggsDNA/.git
git -C workspaces/HiggsDNA rev-parse --verify HEAD
```

An existing non-Git directory, invalid checkout, unexpected origin, wrong branch, ahead/diverged
history, or local changes requires manual inspection. When intentionally approving a newer
HiggsDNA version, update the full `commit` value in `config/repositories.json`, review it, and commit
that WorkflowAutomation change. The branch name alone is not a reproducibility boundary.
Move it aside only after confirming that doing so is safe.

To test the complete dependency chain from scratch without touching an existing checkout or
environment, select a new empty workspace. The configured HiggsDNA revision is cloned automatically:

```bash
SCRATCH_WORKSPACE=/vols/cms/dw515/WorkflowAutomation-scratch/workspaces

test ! -e "$SCRATCH_WORKSPACE" && echo "scratch workspace is unused"

law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace "$SCRATCH_WORKSPACE" \
  --local-scheduler
```

Do not reuse a path unless you have inspected it. A successful scratch run creates both
`$SCRATCH_WORKSPACE/HiggsDNA` and `$SCRATCH_WORKSPACE/.environments/HiggsDNA`.

## Step 4: prepare a repository environment with `law`

### Automated

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --local-scheduler
```

This automatically requires `RepositoryCheckout`. HiggsDNA's environment is created at:

```text
workspaces/.environments/HiggsDNA
```

Cluster example with an alternative project-owned environment root:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace /path/to/group/workspaces \
  --environment-root /path/to/group/environments \
  --local-scheduler
```

The generic task reads `environment_file`, `install_extras`, `pip_install_dependencies`, and
`import_name` from the selected repository configuration. For HiggsDNA it prefers Mamba, falls
back to Conda, and creates the complete dependency environment from `environment.yml`. It then
installs the checkout editable with pip dependency resolution disabled and checks that `higgs_dna`
resolves to the managed checkout. This separation prevents pip from replacing the versions chosen
by Conda. HiggsDNA's environment definition pins Python 3.11 for compatibility with the known
working legacy `coffea` stack. A valid environment makes the task complete without reinstalling.

### Manual equivalent for HiggsDNA

```bash
HIGGSDNA_CHECKOUT="$PWD/workspaces/HiggsDNA"
HIGGSDNA_ENV="$PWD/workspaces/.environments/HiggsDNA"
mkdir -p "$PWD/workspaces/.environments"
```

Create with Mamba:

```bash
mamba env create \
  --yes \
  --prefix "$HIGGSDNA_ENV" \
  --file "$HIGGSDNA_CHECKOUT/environment.yml"
```

Or replace `mamba` with `conda`. Then install and validate:

```bash
"$HIGGSDNA_ENV/bin/python" -m pip install \
  --no-deps \
  --no-build-isolation \
  --editable "$HIGGSDNA_CHECKOUT[dev]"
"$HIGGSDNA_ENV/bin/python" -c \
  'import higgs_dna; print(higgs_dna.__file__)'
```

The printed module path must be inside `workspaces/HiggsDNA`. Do not omit `--no-deps`: all
HiggsDNA dependencies are managed by `environment.yml`, and resolving them again with pip can
replace that tested environment.

### Completion check and recovery

Rerun the `law` command; Luigi should report the task complete without executing it. If creation or
installation fails, the prefix may be incomplete. Preserve the error, inspect the prefix, and move
it aside manually when safe before retrying. Neither the task nor launcher deletes it.

To perform a genuinely clean rebuild, first confirm the exact generated prefix and then move it
aside (the automation deliberately never deletes environments):

```bash
cd /vols/cms/dw515/WorkflowAutomation
mv workspaces/.environments/HiggsDNA \
  "workspaces/.environments/HiggsDNA.before-clean-rebuild"
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

The moved environment is retained for recovery until the clean environment has been validated.

## Adding another repository

Add an entry to `config/repositories.json` with its URL, branch revision, exact approved commit,
checkout directory, environment file, editable-install extras, and validation import. Then select
it with the same generic task:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository AnotherRepository \
  --local-scheduler
```

A downstream task should require only the `RepositoryEnvironment` instances it actually uses.
Unrelated repositories remain untouched.

## Current boundary: input discovery, but no processing or submission

The initial CP production is configured in `config/productions.json` for `Run3_2022` only. Generate
an inspectable plan with:

```bash
law run workflow_automation.tasks.DitauProductionPlan \
  --production cp_2022_test \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

The plan and generated per-era/channel analysis configurations are written below
`WORKSPACE/.workflow_automation/productions/cp_2022_test/`. The plan labels commands that would
submit jobs, but does not execute them (`submission_enabled` is `false`). Additional eras are added
to the production's `eras` list after the 2022 test is validated.

This task depends on `RepositoryEnvironment(HiggsDNA)`, which itself depends on the checkout task.
The one production command therefore clones HiggsDNA and creates its environment when needed, while
reusing valid existing setup.

The next implemented wrapper adds credential validation and dCache sample discovery:

```bash
law run workflow_automation.tasks.DitauInputPreparation \
  --production cp_2022_test \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

Create the CMS proxy manually first, as documented in `ditau-production.md`. This task queries
dCache with `gfal-ls` and writes fingerprinted sample JSONs under the workflow state directory; it
does not modify HiggsDNA. It does not run analysis processing, query HTCondor, or submit jobs.

Plan the next effective-event stage without executing its commands:

```bash
law run workflow_automation.tasks.DitauEffectiveEventPlan \
  --production cp_2022_test \
  --era Run3_2022 \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --local-scheduler
```

This produces separate `Events.json` and `EventsNotSelected.json` configurations plus an inspectable
`effective-events/Run3_2022/plan.json`. Its command arrays would submit through Imperial Condor if
manually executed, so the plan labels them `submits_jobs: true`; WorkflowAutomation keeps
`submission_enabled` false and never invokes them.

The separately gated execution task is documented in `ditau-production.md`. It requires a readiness
report, an explicit `--allow-submission` flag, one worker, and durable per-tree intent and receipt
files. An interrupted or ambiguous attempt blocks automatic retry. Job-status checking, collection,
stitching, and parameter generation are not implemented yet.

## Documentation requirements for future steps

Each new stage must be added here in the same change, including its purpose, prerequisites,
automated and manual commands, inputs, outputs, completion check, expected resources, safe recovery,
and whether it accesses remote data or submits jobs.
