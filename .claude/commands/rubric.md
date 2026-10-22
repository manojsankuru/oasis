---
description: Score the repository 1 to 5 on each OASIS review dimension against docs/reviews/rubric.md
---

Score this repository against the four review dimensions in
`docs/reviews/rubric.md`. That file holds the program committee's rubric
verbatim. Read it first. If it is missing, stop and say so — do not score from
memory or from the criteria summary in `CLAUDE.md`.

This is a critical review, not a status report — assume a program committee is
reading the code, not my description of it. Only working code and produced
artifacts count as evidence. "It is designed to" does not count. The rubric
scores the whole submission: judge the repository and the paper draft in
`paper/main.tex`, and say where a score depends on the demo video, which you
cannot see.

For each dimension (TU, RB, SG, IR in `CLAUDE.md`):

1. **Score.** One integer from 1 to 5, with its level name from `rubric.md`.
   Give the highest level whose descriptor the evidence fully meets.
2. **Evidence.** What a reviewer would find in the repository, concretely.
   Cite files and functions.
3. **Level-5 gap.** Quote the level-5 descriptor from `rubric.md` exactly.
   Then quote each phrase of it the repository falls short of, and say what is
   missing for each. If the score is 5, name the evidence that meets every
   phrase instead.
4. **Claimed but not implemented.** Anything the README, `CLAUDE.md` or the
   paper draft claims that the code does not do. Be blunt about this.
5. **Cheapest next step.** The single cheapest change that would most raise
   this score, with an hour estimate.

Then: a table of the four scores, the **weakest** dimension, and what I should
do next. If two are close, say so rather than picking arbitrarily.

Finish with anything you found that is actively working against a dimension —
a hardcoded county, a hand-cleaned file, a mocked integration presented as
tested, a number in the paper draft that is not in
`outputs/paper/numbers.json`, or a `numbers.json` value with no tool result
behind it.
