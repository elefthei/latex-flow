# latex-flow

Checks for latex theory flow issues.

## flow-tracker

`skills/flow-tracker` is an Agent Skill that audits the definition-to-use flow of a LaTeX
theory paper: it scans the root file (following `\input`/`\include`), records every
definition, result, label, reference, and notation macro in document order, and then
reports issues such as references to undefined labels, results that are never used, and
notation used before it is introduced. Output is JSONL sorted by document position, so a
diff between two runs shows exactly what moved.

```sh
python3 skills/flow-tracker/scripts/flow.py scan ROOT.tex -o flow.jsonl [--notation NOTATION.tex]
python3 skills/flow-tracker/scripts/flow.py issues flow.jsonl -o flow-issues.jsonl
```

Install: copy `skills/flow-tracker` into a project's `.github/skills/` or
`.claude/skills/` directory.
