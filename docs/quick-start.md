# Quick start

Commands only. What each one means is in [`ditau-production.md`](ditau-production.md),
[`batch-resubmission.md`](batch-resubmission.md) and [`derived-artefacts.md`](derived-artefacts.md).

Everything here is for the `cp_2022_test` production and the `Run3_2022` era. Substitute your own.

## Once per shell

```bash
cd /vols/cms/dw515/WorkflowAutomation
source .venv/bin/activate
source /vols/grid/cms/setup.sh
export X509_USER_PROXY="${HOME}/cms.proxy"
```

## Once every few days: a grid proxy

Nothing in the workflow creates or renews one; it only checks.

```bash
voms-proxy-info --file "$X509_USER_PROXY" --timeleft        # 0 means expired
voms-proxy-init --rfc --voms cms --valid 192:00 --out "${HOME}/cms.proxy"
```

## The whole chain

Each step is safe to rerun: a task that is already satisfied does nothing.

```bash
# 1. discover samples, build the plan, check everything is ready. Submits nothing.
law run workflow_automation.tasks.DitauEffectiveEventReadiness \
  --production cp_2022_test --era Run3_2022 \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces --local-scheduler

# 2. submit the effective-event jobs, one tree at a time.
#    --skip-completed omits datasets that already finished, so widening a
#    production costs only the jobs it adds.
law run workflow_automation.tasks.DitauEffectiveEventSubmission \
  --production cp_2022_test --era Run3_2022 --tree Events \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission --skip-completed --local-scheduler

law run workflow_automation.tasks.DitauEffectiveEventSubmission \
  --production cp_2022_test --era Run3_2022 --tree EventsNotSelected \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission --skip-completed --local-scheduler

# 3. did they actually run? Reports per dataset and per failure cause. Changes nothing.
law run workflow_automation.tasks.DitauEffectiveEventStatus \
  --production cp_2022_test --era Run3_2022 --tree Events \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces --local-scheduler

# 4. only if step 3 reported failures: resubmit them with the resources their
#    failure earned. Application errors are never resubmitted.
law run workflow_automation.tasks.DitauEffectiveEventResubmission \
  --production cp_2022_test --era Run3_2022 --tree Events \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission --local-scheduler

# 5. build the counts, stitching and params files.
law run workflow_automation.tasks.DitauStitchingAndParams \
  --production cp_2022_test --era Run3_2022 \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces --local-scheduler
```

## The standard analysis, per channel

```bash
# submit one channel
law run workflow_automation.tasks.DitauStandardAnalysisSubmission \
  --production cp_2022_test --era Run3_2022 --channel tt \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission --local-scheduler

# did those jobs run? same reporting as the effective-event trees
law run workflow_automation.tasks.DitauStandardAnalysisStatus \
  --production cp_2022_test --era Run3_2022 --channel tt \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces --local-scheduler

# only if that reported failures
law run workflow_automation.tasks.DitauStandardAnalysisResubmission \
  --production cp_2022_test --era Run3_2022 --channel tt \
  --workspace /vols/cms/dw515/WorkflowAutomation/workspaces \
  --allow-submission --local-scheduler
```

Channels are submitted one at a time on purpose: each gets its own intent, receipt and record, so
a failure in one is contained and diagnosable rather than mixed into the others.

## Two flags that mean something

| Flag | Why it exists |
| --- | --- |
| `--allow-submission` | nothing reaches the batch system without it |
| `--allow-overwrite` | required to replace a generated file that carries no provenance header, i.e. one this workflow did not write |

## When something looks wrong

```bash
# our jobs only, not the account's other work
condor_q "$USER" -af ClusterId Iwd Cmd | grep -iE 'higgsdna|workflowautomation'

# why a held job is held
condor_q "$USER" -constraint 'JobStatus==5' -af ClusterId HoldReason

# the last status report, including the cause of every failure
python3 -m json.tool \
  workspaces/.workflow_automation/productions/cp_2022_test/effective-events/Run3_2022/status/Events.json
```

An unresolved submission intent blocks any retry on purpose. Before clearing one, check that no
jobs are running, that there is no receipt, and read
[`ditau-production.md`](ditau-production.md#reconciling-an-unresolved-intent).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```
