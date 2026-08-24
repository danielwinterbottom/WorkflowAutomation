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

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
