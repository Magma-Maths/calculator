# `.github/workflows/`

One line per workflow. Read the file itself for details.

| File | Trigger | Purpose |
|---|---|---|
| `ci.yml` | push to `main`, PR | `test` runs the shared fake-only selection. `build` creates one candidate artifact, then canary and reusable containment verify it. `publish` waits for both checks and publishes that candidate only on a Magma-Maths `main` push. |
| `containment.yml` | reusable workflow call | Loads the calculator AppArmor profile and runs real-image containment checks against the verified candidate artifact prepared by `ci.yml`. |
| `promote.yml` | push of a `v*` tag (Magma-Maths only) | Retags the tagged commit's `sha-<short>` image as the version tag without rebuilding. Fails if the commit has no image; refuses if the version tag is already published. |

## The `sha-<short>` coupling

The canary and containment jobs load the same checked-in AppArmor profile on their
separate Ubuntu runners. Each job prints kernel denials on failure. Profile loading
and container delegation remain runtime prerequisites; missing support fails the job.

`ci.yml` tags each image with the first 7 hex characters of the commit, and
`promote.yml` looks that tag up after peeling the git tag to its commit. The two
files must agree on the length; change one, change the other.

The same coupling is why `ci.yml` has no path filters on its push trigger and
why its concurrency group queues runs instead of cancelling them. A commit on
`main` that produced no image (skipped as docs-only, or cancelled by the next
push) cannot be promoted, and every commit on `main` is meant to be a release
candidate. The buildx cache keeps a no-op rebuild cheap.

Queueing is `queue: max`, which holds up to 100 pending runs per group and
starts them in order. It is not `cancel-in-progress`, which governs running runs
only: under the default `queue: single` a newer run cancels the pending one
regardless, so three commits pushed to `main` inside one run's duration would
leave the middle one with no `sha-<short>` image. `cancel-in-progress` must
therefore stay `false`, since GitHub rejects `queue: max` alongside
`cancel-in-progress: true`, and `queue` is not documented to accept an
expression, so the setting is uniform: runs on PR branches queue too rather than
superseding each other.
