---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# CI, typing and readiness (PRD epic E6)

## What / why

Nothing checks this repo but the person at the keyboard. The suite only
runs when someone remembers to start a database container and run it; a
missing file or a broken import reaches `main` unseen; type mistakes are
found at run time; and an orchestrator cannot tell a process that is up
from one that can reach its database. This run closes PRD epic E6: every
push is checked on a clean checkout, the code is type-checked, the tests
start with one command, and the API says whether it is ready.

In scope (existing repo):

- **Every push is checked.** A GitHub Actions workflow runs on every
  push to any branch, and on demand: lint, type check, then the full
  suite against a Postgres service, from a clean checkout with the
  locked dependencies.
- **The code is type-checked.** mypy runs over `app/`: loose on most of
  it, strict on `app/security`, `app/queue` and `app/conversations`. The
  errors it reports today (about 44 loose, 16 strict) are fixed without
  changing behaviour.
- **Tests start with one command.** `make test` brings up the database
  when none is listening and runs the suite. `make lint`, `make
  typecheck` and `make check` (all three) sit beside it.
- **The API reports readiness.** `GET /ready` answers 200 when the
  database and Redis both respond, 503 when either does not, and says
  which. `/health` stays as it is: cheap, no dependencies.

Decided in the grill:

- mypy, not pyright (user).
- The workflow runs on `push` only (all branches) plus manual dispatch:
  one developer, no forks, so a pull-request trigger would only run
  everything twice.
- No Redis service in CI: the suite uses an in-memory fake. `/ready` is
  tested with fakes that fail on purpose.
- Tests are not type-checked, only `app/`.
- A type error is fixed with a type, not silenced; an ignore needs a
  reason on the same line.
- E6.4 needs no work: the earlier epics' tests exist and CI now runs
  them.
- The README and `docs/FOLLOW_UPS.md` describe the commands, the
  workflow and the new endpoint.

## Primary user story

As the person maintaining parley, I push a branch and GitHub tells me
within minutes whether it still installs, type-checks and passes, and on
my machine `make check` tells me the same before I push.

## Out of scope

- Coverage measurement and thresholds
- Type-checking `tests/`, strict mode on the rest of `app/`
- Building or publishing the Docker image in CI; deploy steps
- A test run against a real Redis, model or channel
- Branch protection rules on GitHub (a setting, not code)
- `/ready` checking the worker, the model endpoint or the media disk
- PRD epic E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when I can run `make check` on my machine and see lint, type check
and the suite pass, call `/ready` and get 200 with both checks ok and
503 naming the one that is down, and push the branch and see the
workflow pass on GitHub — and the suite proves the readiness answers,
and a change that deletes `app/media/` or breaks a type in
`app/security` fails `make check`.
