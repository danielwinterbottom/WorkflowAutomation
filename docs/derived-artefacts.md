# Derived configuration files

Three files under `scripts/ditau/config/<era>/` are produced from the effective-event output and
kept in the HiggsDNA repository once made:

| File | Produced by | From |
| --- | --- | --- |
| `effective_events.yaml` | `getEffectiveEvents.py` | the per-file counts the batch jobs wrote |
| `Stitching.yaml` | `getStitchingInfo.py` | cross sections and the counts |
| `params.yaml` | `getParams.py` | the sample list, cross sections, counts and filter efficiencies |

The counts are expensive: they exist only because thousands of batch jobs produced them. Nothing
recorded what any of these files were built from, so there was no way to tell a current file from a
stale one, and the safe assumption was always to rebuild.

## What decides whether a file is rebuilt

Each generated file carries a header naming its inputs by SHA-256 of their contents:

```text
# workflow-automation-provenance: {"era":"Run3_2022","inputs":{"generator":"…","sample_manifest":"…","samples_yaml":"…"},…}
```

Rebuilding happens when a recomputed digest differs from the recorded one, and not otherwise.
Timestamps play no part: touching a file, copying the tree, or taking a fresh checkout changes
nothing. Reverting an edit restores the original content and therefore the original digest, so the
file becomes current again rather than being rebuilt for a change that no longer exists.

The header is a comment, so every consumer keeps parsing these files unchanged. Its length depends
on the number of inputs, not their size: the sample manifest is 263KB listing 1530 files and
contributes 64 characters.

## Why contents rather than commits

A commit identifying the last change to a file is a reasonable version marker, but two things rule
it out here. The sample manifest is generated into the workspace and tracked by no repository, so it
has no commit at all, and it is the input most likely to change, since what dCache holds shifts
underneath us. Local edits also have no commit yet, so a file modified but not committed would
report itself current while differing from what the artefact was built from.

Contents cover both, tracked or not, committed or not. The HiggsDNA commit is recorded alongside for
traceability but takes no part in the comparison.

## The program counts as an input

Each artefact also hashes the script that produces it. A fix to how `getEffectiveEvents.py` sums
generator weights changes every count while leaving the sample list and manifest identical; without
this the file would look current and stay quietly wrong.

Each artefact watches only its own script, so editing `getParams.py` does not invalidate the counts.

Rebuilding the counts after such a fix does not mean resubmitting: the batch jobs write per-file
`.txt` counts which remain on disk, and `getEffectiveEvents.py` only sums them. Jobs are needed
again only when that output is gone, or when the sample list or manifest genuinely changed.

## Files this workflow did not write

A file carrying no header was produced by somebody else, by hand or by an older process, and its
contents are the only record of that work. Overwriting one requires `--allow-overwrite`.
Regenerating a file this workflow made itself needs no flag.

The `Run3_2022` artefacts committed in August 2026 predate this and carry no header, so the chain
refuses to touch them until they are either regenerated deliberately or adopted.
