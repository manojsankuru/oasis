---
description: Record a real failure in docs/failures.md
argument-hint: [one-line description of what broke]
---

Append an entry to `docs/failures.md` for: $ARGUMENTS

Use this format, and fill it from what actually happened in this session — do
not generalise, do not tidy, do not invent a cause you did not observe:

```
## <date> — <short title>

**What happened.** <the observable symptom, with the actual error text>
**Where.** <file / function / dataset / endpoint>
**Why.** <root cause if known; write "not determined" if not>
**Did the agent recover?** <yes/no, and how many turns it took>
**Kept as a paper failure case?** <yes/no, and which section>
```

These entries become §3.7 of the paper. Their value is that they are real, so
never rewrite an entry to sound better and never delete one because it was
embarrassing. If the same failure recurs, add a new dated entry rather than
editing the old one.
