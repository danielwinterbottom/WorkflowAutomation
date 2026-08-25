# WorkflowAutomation

Portable workflow orchestration for local development and cluster production.

## Set up the workflow controller

Create the project-local environment containing `law` and WorkflowAutomation:

```bash
./workflow setup-controller
source .venv/bin/activate
```

The controller is independent of all analysis repositories. Repository environments are created
only when a selected task requires them.

## Prepare a repository with `law`

Prepare HiggsDNA's checkout and isolated environment through the generic task graph:

```bash
law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --local-scheduler
```

`RepositoryEnvironment` depends on `RepositoryCheckout`, so cloning happens automatically when
needed. Definitions live in `config/repositories.json`; future repositories use the same tasks and
remain independent of HiggsDNA. The dependency-free `./workflow bootstrap` command remains
available for recovery before the controller exists.

For a completely fresh integration test, pass a new empty `--workspace` path. This forces the task
graph to clone the configured repository revision and create its environment from nothing; see the
[workflow steps guide](docs/workflow-steps.md) for the cluster command.

## Choose a cluster workspace

Repository checkouts and their environments are stored under `workspaces/` by default. This
directory is ignored by Git. On the group cluster, use a durable user-owned directory rather than
`/tmp` or a login-node-local filesystem. The recommended convention is:

```text
/vols/cms/<username>/WorkflowAutomation/workspaces
```

For example, when WorkflowAutomation is checked out at `/vols/cms/dw515/WorkflowAutomation`:

```bash
WORKSPACE=/vols/cms/dw515/WorkflowAutomation/workspaces

./workflow diagnose --workspace "$WORKSPACE"

law run workflow_automation.tasks.RepositoryEnvironment \
  --repository HiggsDNA \
  --workspace "$WORKSPACE" \
  --local-scheduler
```

This creates the checkout at `$WORKSPACE/HiggsDNA` and its isolated environment at
`$WORKSPACE/.environments/HiggsDNA`. Use a genuinely shared group directory only when multiple
users need it and its ownership, permissions, and update policy have been agreed; user-owned Conda
environments are the safer default.

## Read-only environment diagnostics

Inspect local or cluster prerequisites without changing the filesystem, contacting the scheduler,
or submitting work:

```bash
./workflow diagnose --workspace /path/to/group/workspaces
./workflow diagnose --workspace /path/to/group/workspaces --format json
```

The command reports executable discovery, selected batch-environment indicators, filesystem
metadata, and configured checkout state. Environment-variable values are not printed. Job
submission is deliberately outside the current project scope.

For each automated step alongside its manual equivalent, see
[`docs/workflow-steps.md`](docs/workflow-steps.md). For operating policy, expected behavior, and
troubleshooting, see [`docs/operator-guide.md`](docs/operator-guide.md).

The developing CP ditau production graph, its single-era test scope, credential boundary, and exact
submission-directory records are documented in
[`docs/ditau-production.md`](docs/ditau-production.md).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
