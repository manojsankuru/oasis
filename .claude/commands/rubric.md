---
description: Audit the current repository state against the four OASIS criteria
---

Audit this repository against the four OASIS criteria in `CLAUDE.md`. This is a
critical review, not a status report — assume a program committee is reading
the code, not my description of it.

For each of TU, RB, SG, IR:
- What in the repository, concretely, a reviewer would find as evidence. Cite
  files and functions. "It is designed to" does not count; only working code
  and produced artifacts count.
- What is claimed anywhere (README, CLAUDE.md, paper draft) but not actually
  implemented. Be blunt about this.
- The single cheapest thing that would most improve this criterion right now,
  with an hour estimate.

Then: name the **weakest** of the four and say what I should do next. If two
are close, say so rather than picking arbitrarily.

Finish with anything you found that is actively working against a criterion —
a hardcoded county, a hand-cleaned file, a mocked integration presented as
tested, a number in the paper draft with no tool result behind it.
