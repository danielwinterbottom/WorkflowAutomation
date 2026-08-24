# WorkflowAutomation operator guide

This is the practical runbook for setting up and operating the workflows. Update it whenever a
command, prerequisite, output, or recovery procedure changes.

## Current scope

The first implemented operation prepares the HiggsDNA source checkout. Scientific environment
creation and HiggsDNA processing tasks are not implemented yet.

## Prerequisites

- Python 3.9 or newer
- Git
- Network access to `gitlab.cern.ch`

The bootstrap command intentionally has no third-party Python dependencies, because it must run
before the law workflow environment exists.

## Local setup

From the WorkflowAutomation checkout:

```bash
./workflow bootstrap HiggsDNA
```

By default, this creates `workspaces/HiggsDNA`. The `workspaces/` directory is ignored by Git.

## Cluster setup

Check out WorkflowAutomation on the cluster, then choose a durable checkout root appropriate for
the group:

```bash
./workflow bootstrap HiggsDNA --workspace /path/to/group/workspaces
```

The same repository configuration is used locally and on the cluster. Machine-specific filesystem
paths are supplied at runtime and should not be committed.

## Safe repeat-run behavior

When HiggsDNA is absent, bootstrap clones the configured branch into a temporary directory,
validates it, and then moves the completed checkout into place. If cloning is interrupted, the
temporary checkout is removed and the final destination remains absent.

When HiggsDNA already exists, bootstrap:

1. Confirms the destination is a Git checkout with a valid `HEAD`.
2. Confirms its `origin` matches the configured HiggsDNA URL.
3. Reports its commit and whether it contains local changes.
4. Does not fetch, pull, reset, switch branches, or discard changes.

## Expected output

A new setup prints a clone message followed by a ready message:

```text
[clone] HiggsDNA: https://gitlab.cern.ch/dwinterb/HiggsDNA.git -> .../HiggsDNA
[ready] HiggsDNA: .../HiggsDNA (<commit>, clean)
```

A repeat run prints only the ready message.

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

## Documentation standard for new workflow stages

Every new stage must document:

- Purpose and prerequisites
- Exact local and cluster commands
- Inputs and outputs
- Completion checks
- Configuration and environment variables
- Expected runtime and resource requirements, when known
- Common failures and safe recovery
- Which repository revisions are recorded for reproducibility
