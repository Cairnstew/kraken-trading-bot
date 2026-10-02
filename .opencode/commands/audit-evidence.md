---
description: Flag evidence in .data-audit/ that a fresh clone cannot re-derive, and check a falsification record exists
---

```bash
just audit-evidence
```

Run this before citing evidence in a gate record or a final summary. It reports three things:

1. **Scripts cited as evidence** that are not in the repo — a citation nobody can re-run.
2. **`/tmp` paths cited** in `.data-audit/*.md` — by construction not re-derivable from a clone. The
   2026-10-02 pass's width-hash assertion depended on exactly this and was therefore unreproducible;
   that script is now `tools/width_check.py`.
3. **Whether a falsification record exists** anywhere in the corpus.

Item 3 is the one that matters most. A green check proves nothing until someone has tried to break
it. The 2026-10-02 pass cited a clause-checking script as covering its final state; a reviewer proved
it **inert by mutation** — restoring both defects in the docstring still gave 11/11 PASS — because
every assertion measured *pandas' behaviour* and none was bound to any *docstring text*. The lesson
for any check written here: each assertion must name the exact artifact substring it defends and fail
when that substring is absent or altered.

Resolving a finding from this command means **either** moving the script into the repo **or**
withdrawing the citation. Do not leave a `/tmp` path standing as evidence.
