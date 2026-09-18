# `.github/workflows/`

One line per workflow. Read the file itself for details.

| File | Trigger | Purpose |
|---|---|---|
| `ci.yml` | push to `main`, PR | `test` runs the pytest suite. `build` (push to `main`, Magma-Maths only) builds the image once and pushes it to `ghcr.io/magma-maths/calculator` as `sha-<short>` and the moving `main`. |
| `promote.yml` | push of a `v*` tag (Magma-Maths only) | Retags the tagged commit's `sha-<short>` image as the version tag without rebuilding. Fails if the commit has no image; refuses if the version tag is already published. |

## The `sha-<short>` coupling

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
