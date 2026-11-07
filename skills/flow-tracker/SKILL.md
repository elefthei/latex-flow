---
name: flow-tracker
description: Track the definition-to-use flow of a LaTeX theory paper. Scans the document in reading order, records every definition, lemma, theorem, proposition, labelled object, inference-rule name, and notation macro together with each place it is used later or earlier, and writes flow.jsonl. A second deterministic pass over flow.jsonl writes flow-issues.jsonl listing results that are never used again and objects used before they are defined. Use when asked to check the paper's logical flow, find dead lemmas or definitions, find forward references, or audit notation.
---

# Flow tracker

## Run

From the repository root:

```sh
python3 skills/flow-tracker/scripts/flow.py scan ROOT.tex -o flow.jsonl [--notation NOTATION.tex]
python3 skills/flow-tracker/scripts/flow.py issues flow.jsonl -o flow-issues.jsonl
```

`scan` follows `\input`/`\include` from the root in reading order, drops comments and
`comment`/`verbatim` bodies, and writes one JSON object per item. `issues` reads only
`flow.jsonl`; it never looks at the sources, so its output is a pure function of the scan.
Both outputs are sorted by document position (`order`), so a diff between two runs shows
exactly what moved.

| Exit (`issues`) | Meaning |
| --- | --- |
| 0 | No issues |
| 1 | Warnings or info only |
| 2 | At least one error (reference to a label that does not exist) |

`--notation FILE` (repeatable) names the files whose macro definitions count as notation.
Without it, every file `\input` before `\begin{document}` is used; pass `--notation` to
narrow this to the files that actually define notation.
`--ignore-macro REGEX` drops macros by name.

## What is tracked

| Kind | Item | Uses counted |
| --- | --- | --- |
| theorem-like (`definition`, `lemma`, `theorem`, `proposition`, `corollary`, `remark`, …, plus any `\newtheorem`) | each environment instance with its labels and `[title]` | `\ref`-family and `\cref`/`\Cref`/`\eqref`/`\hyperref` to any of its labels; case-insensitive mention of a multi-word title outside the environment |
| `equation`, `figure`, `table`, `section`, `other` | every other `\label` | `\ref`-family |
| `rule` | `\inferrule*[right=Name]` | the name elsewhere in the text; names that look like English words (`Frame`) count only inside `\textsc{}`/`\mkw{}` |
| `macro` | `\newcommand`, `\DeclareMathOperator`, `\def`, `\NewDocumentCommand`, … in the notation files | every occurrence in the body outside macro definitions |
| `undefined-label` | a referenced label with no `\label` | the references |

Each use records `file`, `line`, `order`, `via` (`ref`, `cref`, `mention`, `macro`, …),
`relation` (`before`, `after`, `inside` the item's own environment), and the source line.

For a macro, `introduced` is the first use that looks like a definition: inside a
`definition`/`notation` environment, on a line with a definitional operator
(`:=`, `\coloneqq`, `\triangleq`, `\iff`, `&=`) to its right, or within two lines of
defining prose (`define`, `denote`, `write`, `notation`). This is a heuristic; the issue
record names the detected point so you can check it.

## Issue codes

| Code | Severity | Meaning |
| --- | --- | --- |
| `undefined-label` | error | referenced, never labelled |
| `unused-result` | warning | theorem-like statement never referenced or mentioned again |
| `forward-reference-only` | warning | every reference precedes the statement |
| `used-before-defined` | warning | some references precede the statement |
| `unused-notation` | warning | macro defined in a notation file, never used in the body |
| `notation-used-before-introduction` | warning | macro used before its detected introduction |
| `notation-never-introduced` | info | macro used, but no use looks like a definition |
| `unlabeled-result` | info | theorem-like statement with no `\label` |
| `unused-rule`, `rule-used-before-defined` | info | inference-rule name never or only prematurely mentioned |
| `unreferenced-equation` | info | labelled display never referenced |

## Report

1. Triage every `unused-result` into exactly one of: (a) a top-level or final theorem,
   the end of a line of argument; (b) a standalone result that is interesting on its own
   and whose role the surrounding text states; (c) dead weight. Only (c) is a defect.
   Report (a) and (b) as a list so the author can confirm the classification; do not
   silently drop them.
2. For `forward-reference-only` and `used-before-defined`, distinguish roadmap text
   (introduction, abstract, section openers) from a proof or definition that depends on
   something stated later. Only the latter is a defect.
3. `notation-*` findings depend on the introduction heuristic. Open the cited line before
   reporting; if the detected introduction is actually a use, say so and point to where the
   notation is really introduced, or that it is introduced nowhere.
4. Do not edit the manuscript to resolve a finding unless asked. The deliverable is the
   two JSONL files plus a short list of confirmed defects with `file:line`.
