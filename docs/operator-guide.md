# WorkflowAutomation operator guide

This is the practical runbook for setting up and operating the workflows. Update it whenever a
command, prerequisite, output, or recovery procedure changes.

For copyable commands covering each automated stage and its manual equivalent, use the
[workflow steps guide](workflow-steps.md). This operator guide records safety behavior and recovery
policy; the workflow steps guide is the canonical execution sequence.

## Current scope

The implemented operations create the WorkflowAutomation controller, prepare independently selected
repository checkouts and environments through `law`, and inspect the execution environment.
Scientific processing, scheduler queries, and job submission are not implemented yet.

## Prerequisites

- Python 3.9 or newer
- Git
- Mamba (preferred) or Conda for environment creation
- Network access to `gitlab.cern.ch`

The bootstrap command intentionally has no third-party Python dependencies, because it must run
before the law workflow environment exists.

## Local setup

From the WorkflowAutomation checkout, create the independent controller and activate it:

```bash
./workflow setup-controller
source .venv/bin/activate
```

Prepare HiggsDNA only when it is required:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --local-scheduler
```

The task requires the generic checkout task and creates the Conda prefix at
`workspaces/.environments/HiggsDNA`. Other repository workflows declare their own dependencies and
do not implicitly require HiggsDNA. HiggsDNA's environment definition pins Python 3.11 for the
known working legacy `coffea` stack.

## Cluster setup

Check out WorkflowAutomation on the cluster, run `./workflow setup-controller`, and activate
`.venv`. Then prepare the selected repository in a durable workspace appropriate for the group:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace /path/to/group/workspaces \
  --local-scheduler
```

The same repository configuration is used locally and on the cluster. Machine-specific filesystem
paths are supplied at runtime and should not be committed.

To keep environments elsewhere, provide an explicit project-owned root:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace /path/to/group/workspaces \
  --environment-root /path/to/group/environments \
  --local-scheduler
```

Activate the resulting environment when working interactively:

```bash
conda activate /path/to/group/workspaces/.environments/HiggsDNA
```

Environment creation can take several minutes and requires access to the configured Conda
channels and Python package indexes. If creation fails, the task reports the possibly partial prefix
and stops. Inspect it, move it aside when safe, and rerun the task; it never deletes or overwrites
an incomplete prefix automatically.

Repository configuration may declare additional `validation_imports`. HiggsDNA validates
`pkg_resources` and `uproot` as well as the editable `higgs_dna` import, ensuring the legacy runtime
is usable rather than merely installed. Its package metadata constrains Setuptools to 70.1.1 because
Setuptools 82 and later no longer provide the `pkg_resources` API required by this Uproot version.

## Read-only cluster diagnostics

Run diagnostics from the WorkflowAutomation checkout before bootstrapping or after logging into a
different cluster node:

```bash
./workflow diagnose --workspace /path/to/group/workspaces
```

For machine-readable output suitable for attaching to an issue:

```bash
./workflow diagnose --workspace /path/to/group/workspaces --format json
```

The report contains:

- Host platform and Python version.
- Paths found for Git, Python, law, HTCondor client commands, and `voms-proxy-info`.
- Whether selected environment variables are set. Their values are never included.
- Existence and access metadata for the configuration, workspace, and referenced proxy file.
- Current commit, branch, origin-match status, and local-change status for configured checkouts.

The command only examines local process and filesystem metadata and runs read-only Git commands.
It does not create the workspace, alter a checkout, contact HTCondor, validate credentials over the
network, fetch repositories, or submit jobs. A discovered `condor_submit` path only means the
client executable is installed; the executable is never run.

The `writable` field is the operating system's access check for the current process. It is useful as
an early warning but does not prove that a later write will succeed (for example, quotas and ACLs
may still prevent one). The command exits successfully when optional tools or checkouts are absent;
these are diagnostic facts rather than command failures. An unreadable or invalid repository
configuration is reported as `configuration_error` in JSON output.

Before sharing a report, review hostnames, usernames embedded in paths, and checkout locations for
site-specific information. Secret environment values are excluded by design.

## Safe repeat-run behavior

When HiggsDNA is absent, bootstrap clones the configured branch into a temporary directory,
validates it, and then moves the completed checkout into place. If cloning is interrupted, the
temporary checkout is removed and the final destination remains absent.

When HiggsDNA already exists, bootstrap:

1. Confirms the destination is a Git checkout with a valid `HEAD`.
2. Confirms its `origin` matches the configured HiggsDNA URL.
3. Confirms its branch and commit match the configured branch and exact approved commit.
4. Reuses it when clean and current.
5. Fetches and fast-forwards it when clean and behind the configured commit.
6. Stops on local changes, the wrong branch, or ahead/diverged history.

It never resets, cleans, switches an existing checkout's branch, or discards changes. Read-only
diagnostics never fetch and report whether the locally visible branch and commit match the pin.

## Expected output

A new setup prints a clone message followed by a ready message:

```text
[clone] HiggsDNA: https://gitlab.cern.ch/dwinterb/HiggsDNA.git -> .../HiggsDNA
[ready] HiggsDNA: .../HiggsDNA (<commit>, clean)
```

A repeat run prints only the ready message.

A safe fast-forward prints an update followed by a ready message:

```text
[update] HiggsDNA: <old-commit> -> <pinned-commit> on workflowautomation
[ready] HiggsDNA: .../HiggsDNA (<pinned-commit>, clean)
```

## Troubleshooting

### Destination exists but is not a Git checkout

Bootstrap stops rather than overwriting it. Inspect the directory, then move or rename it manually
if it is not needed.

### Unexpected origin

Bootstrap stops because the directory may be a different repository or fork. Check it with:

```bash
git -C /path/to/HiggsDNA remote -v
```

Change `config/repositories.json` if the alternate origin is intentional; otherwise select another
workspace directory.

### Incomplete checkout

An older or manually interrupted clone may contain `.git` but no valid commit. Move that directory
aside, inspect or remove it when safe, and rerun bootstrap.

### Local changes or incompatible branch state

Bootstrap stops without changing the checkout. Inspect `git status`, the current branch, and its
history. Commit or preserve intentional work yourself. Do not clean or reset solely to satisfy the
automation. A newer approved repository version is adopted by reviewing and changing the exact
`commit` pin in `config/repositories.json`.

## Documentation standard for new workflow stages

Add the automated and manual procedures to [`workflow-steps.md`](workflow-steps.md) as part of the
same change that introduces a stage.

Every new stage must document:

- Purpose and prerequisites
- Exact local and cluster commands
- Inputs and outputs
- Completion checks
- Configuration and environment variables
- Expected runtime and resource requirements, when known
- Common failures and safe recovery
- Which repository revisions are recorded for reproducibility
