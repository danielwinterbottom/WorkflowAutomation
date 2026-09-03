# Batch failures and resubmission

Every stage of the analysis submits Condor jobs, so how a failed job is diagnosed and what it is
given on a retry lives in one place: [`src/workflow_automation/batch.py`](../src/workflow_automation/batch.py),
configured by [`config/batch.json`](../config/batch.json). Nothing in either is specific to HiggsDNA
or to effective events.

## Why a job failed decides whether it is retried at all

Resubmitting is not resilience. A job that failed because the code or configuration is wrong will
fail again in exactly the same way, and a fleet of such retries occupies the farm while producing
activity that resembles progress. On 26 August 2026 this project submitted 1530 jobs that all died
immediately on a missing import; a blanket retry policy would have run them all again.

| Cause | Recognised from | Retried |
| --- | --- | --- |
| `memory` | Condor holding or killing the job for memory, or an out-of-memory message | yes, with more memory |
| `walltime` | Condor removing the job for exceeding `MaxRuntime` | yes, with more time |
| `infrastructure` | eviction, shadow exceptions, transfer failures | yes, unchanged |
| `incomplete` | started and produced output, never reached its completion marker, gave no reason | yes, unchanged |
| `application` | a Python traceback, import error, or syntax error | **no** |

Resource limits are checked before application errors on purpose. A job killed for exceeding its
memory often dies part-way through unwinding a traceback, and reading that traceback as a code fault
would stop us retrying something a larger slot would finish.

Two further causes are recognised and deliberately not retried:

| Cause | Recognised from | Why not retried |
| --- | --- | --- |
| `attempts` | "exceeding max run count" | the farm has already started it three times |
| `stalled` | held for running over an hour below 2% CPU | it was alive and doing nothing; running it again will not explain why |

## What this farm actually does, and how we know

The classifier is written against observed behaviour, not against what the manual suggests. Two
things were established directly and both contradicted earlier guesses.

Submitting one job with a sixty second limit and a ten minute sleep showed that a wall clock kill
arrives as a **hold**, reading `Job held by SYSTEM_PERIODIC_HOLD due to wall time exceeded`. Neither
`MaxRuntime` nor the word "maximum" appears, so the patterns originally written for it never matched.
That log is kept as a fixture in `tests/fixtures/`.

Reading the schedd's own policy with `condor_config_val -schedd SYSTEM_PERIODIC_HOLD` gives the
authoritative list of what this site holds jobs for:

```text
(runtime > MaxRuntime)                          -> "wall time exceeded"
|| (JobRunCount > 3)                            -> "exceeding max run count"
|| (runtime > 3600 && cpu efficiency < 0.02)    -> no custom message
```

**There is no memory condition in that policy** — but memory is not unenforced. It is applied by the
startd through a cgroup limit, which holds the job with its own message:

```text
Job has gone over cgroup memory limit of 87707 megabytes.
Last measured usage: 85348 megabytes. Consider resubmitting with a higher request_memory.
```

That says neither "exceeded" nor "out of memory", so patterns written from the schedd policy alone
all missed it, and a memory kill would have been reported as an unreadable hold and never retried
with more memory. Reading one source and concluding the other did not exist was the mistake; both
have to be read.

Memory is therefore recognised from that message and from what the job itself reports:
`MemoryError`, `std::bad_alloc`, a bare `Killed`, or an allocation failure.

The run count limit also bounds resubmission from the outside: a job the schedd has started more
than three times is held regardless of what we would like to do with it.

A hold whose reason matches none of the known causes is reported as `unknown` rather than treated as
retryable, on the grounds that it is a site limit we have not learned to read, and retrying into it
would meet it again.

## Only observed failures raise what a job is given

A job's first slot provides whatever that node class happens to offer. That is not a measurement of
what the job needs. Treating it as one is how a job that merely ran out of *time* ends up being
given a second core to carry memory it never used.

So each failure raises a floor only on the axis it actually demonstrated:

- a job that overran shows it needs more time, and nothing about its memory;
- a job that ran out of memory shows it needs more memory, and nothing about its wall clock.

Floors persist across later escalations. Memory demonstrated by a real out-of-memory kill survives a
subsequent escalation for time, even though it then costs a core.

## Choosing a slot

Rather than a hand-written table of time against memory, which will not stay correct as the farm
changes, the site's real options are enumerated: every node class combined with every permitted core
count. They are ordered cheapest first, and the cheapest one satisfying the job's demonstrated needs
wins.

Cheapest means, in order: fewest cores, then shortest node class, then smallest slot. Cores lead
because a single-threaded job handed two slots wastes one. Class length comes next so a job needing
ten hours is not sent to the forty-eight hour queue for no reason.

Two behaviours follow from that ordering rather than from special cases:

- a timeout tries `medium` before `long`;
- extra memory is bought from a roomier node class before it is bought with a core.

## Imperial's classes

Memory is per slot, so wall time and memory are not independent. A three hour job can have 12GB on
one core, but asking for ten hours restricts it to classes whose slots are 4GB.

| Class | Maximum time | Memory per slot |
| --- | --- | --- |
| `short` | 3 hours | 8GB |
| `short-highmem` | 3 hours | 12GB |
| `medium` | 10 hours | 4GB |
| `long` | 48 hours | 4GB |

Requesting more memory than one slot provides means requesting more cores. `+MaxRuntime` sets the
wall clock.

## Worked examples

A job that keeps running out of time:

```text
short x1  (3h, 8GB)  ->  medium x1 (10h, 4GB)  ->  long x1 (48h, 4GB)  ->  stop
```

It stays on one core throughout, because overrunning demonstrated nothing about memory.

A job that times out and then runs out of memory:

```text
short x1 (3h, 8GB)  ->  medium x1 (10h, 4GB)  ->  medium x2 (10h, 8GB)
```

It keeps the ten hours it demonstrated it needed and doubles its memory with the second core. It
does not move to `long`, because only memory was the new problem.

## The core cap is a decision, not a farm limit

`max_cpus_per_job` is set to 2. The farm permits more. Needing more than two cores means the memory
per job is too high, and the work should be split differently rather than handed a bigger slot, so
escalation stops there and says so. Raising it in the configuration stays available to someone who
has decided that is the right trade.

The reachable dead end is a job needing more than 8GB within ten hours: at two cores no slot provides
it. Such a job deserves a human look.
