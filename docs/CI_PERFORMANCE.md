# CI and loop-efficiency baseline

Captured **2026-11-14, before the parallel-CI PR was pushed**.
The immutable data is [ci-baseline-2026-11-14.json](benchmarks/ci-baseline-2026-11-14.json).
It records 20 successful completed runs, their exact commits, attempts, public
run URLs, job start/end timestamps and selected test/build step durations.
The workflow before this change is retained in Git at
`aa2c71cf977996a2b895cd61cd9dc9d904a2d077`.

## Before

Times are medians, minutes:seconds. Required-check wall time is from workflow
creation to the latest completed required job; all-jobs wall time additionally
includes optional Python compatibility and post-merge macOS acceptance.

| Profile | First-attempt samples | Required-check wall | All-jobs wall |
|---|---:|---:|---:|
| Develop push, full platforms | 5 | 19:02 | 30:59 |
| PR, fast Windows, no browser suite | 2 | 6:10 | 6:40 |
| PR, fast Windows, browser suite | 8 | 10:58 | 10:58 |
| PR, full Windows, no browser suite | 4 | 17:44 | 17:44 |

One successful rerun is retained in the JSON but excluded from these aggregates:
its original creation timestamp includes intervening retry/human delay. The
60-run listing at capture contained 46 successes, 12 failures and 2 unfinished
runs; this is an operational snapshot, **not** a randomized failure-rate study.
Do not pool fast and full Windows jobs when comparing their medians.

The macOS job's median execution time was **30:57**. Parallelizing Windows or
starting Chromium earlier does not make macOS faster, so a shorter PR critical
path need not shorten full post-merge acceptance. Batching changes how often the
orchestrator waits for that acceptance; it does not remove the checks.

### Local context, not a statistical baseline

One contended macOS run of candidate `6cc0b911c`:

- Python 3.13.17: 15,920 backend tests passed, 142 existing platform/optional skips,
  **441.44 seconds**.
- 369 frontend files / 6,220 tests passed, **55.52 seconds**.
- Vite reported **4.92 seconds**, excluding the preceding TypeScript check.
- Lock waiting happened but was not timed; do not infer a lock-wait number.

## Compare after merge

1. Collect the next 20 successful **first-attempt** `ci.yml` runs after the merge,
   retaining run IDs, commits, event, full-Windows status and browser-suite status.
   Report sample counts per profile; a missing profile is unmeasured, not faster.
2. Use the same wall-time definitions above, including dependency/runner waits.
   Compare medians within a profile and show the spread; do not sum parallel
   job durations as if they were elapsed time.
3. The old `windows` job maps to the new `windows-backend`, `windows-web` and
   stable `windows` summary. Compare the elapsed Windows acceptance window, not
   the nearly empty summary's own execution duration.
4. For browser PRs compare both `linux-e2e` execution and its start delay from
   workflow creation. The intended improvement is removing its wait on
   `linux-web`, not claiming that Playwright itself runs faster.
5. Separately report failed/cancelled runs, reruns, runner minutes and test
   counts. PR cancellation saves obsolete work but is not a successful test.
   More concurrent jobs may cost more runner minutes even when users wait less.
6. For complete loop runs record user continuation-only nudges, duplicate full
   gate executions on unchanged inputs, lock-wait time, CI-wait time and
   approved-graph completion. These were **not quantified** in this historical
   CI sample; archived chats establish examples, not a trustworthy numeric rate.
7. Keep a dated after-data file beside the baseline; do not overwrite the before
   data or treat this same PR's one CI run as proof of a sustained improvement.

Read-only retrieval:

```bash
gh run list --repo raffaelefarinaro/ciaobot --workflow ci.yml --limit 60 \
  --json databaseId,event,headSha,status,conclusion,createdAt,updatedAt
gh api repos/raffaelefarinaro/ciaobot/actions/runs/RUN_ID
gh api 'repos/raffaelefarinaro/ciaobot/actions/runs/RUN_ID/attempts/ATTEMPT/jobs?per_page=100'
```

The attempt endpoint matters: combining jobs from different attempts can produce
a falsely complete green set. Paginate if a future workflow exceeds 100 jobs.
