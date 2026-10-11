"""A starter set of prompts, one or two of each kind, installed by `pil examples`."""

# ruff: noqa: E501

from __future__ import annotations

EXAMPLES = {
    "start/ticket": """\
---
description: Pick up a ticket and agree a plan before touching code
kind: session
mode: plan
tags: [work]
vars:
  ticket: The ticket number, like ABC-123
  bug:
    help: Is this ticket a bug?
    options:
      yes: resolve
      no: implement
  context: Anything else worth knowing
  tests:
    help: Plan to write tests first?
    default: true
    options:
      yes: Plan to write failing tests first.
      no: Leave tests out of the plan; I'll handle them separately.
---

I'm about to {{bug}} {{ticket}}, on the branch {{branch}}.
{{#context}}

Some context: {{context}}
{{/context}}

Read the code this touches before proposing anything, then give me a short plan: what you'd change, in what order, and what could go wrong.
{{tests}}
""",
    "start/review": """\
---
description: Review the current branch before it goes up for review
kind: session
tags: [work, review]
vars:
  base:
    default: main
    help: The branch to compare against
  focus: Something to look at especially closely
---

Review the changes on {{branch}} against {{base}} as a careful colleague would. Look for bugs, missed edge cases and anything that will be hard to maintain; skip style nitpicks.
{{#focus}}
Pay particular attention to {{focus}}.
{{/focus}}

List what you find, most serious first, with file and line. Don't change anything yet.
""",
    "text/fix": """\
---
description: Fix spelling, grammar and punctuation, and nothing else
kind: transform
tags: [writing]
---

Fix the spelling, grammar and punctuation in this text. Don't reword anything that's already correct, and keep the formatting exactly as it is.
""",
    "text/rewrite": """\
---
description: Rewrite text in a chosen tone
kind: transform
tags: [writing]
vars:
  tone:
    options: [plain, formal, friendly, terse]
    default: plain
  shorter:
    type: toggle
    help: Also cut it down
---

Rewrite this text in a {{tone}} tone. Keep its meaning and its formatting.
{{#shorter}}
Make it noticeably shorter: cut filler and repetition.
{{/shorter}}
""",
    "ask/explain": """\
---
description: Explain whatever is on the clipboard
kind: ask
tags: [reading]
vars:
  question: A specific question about it
---

{{#question}}
{{question}}
{{/question}}
{{^question}}
Explain what this is and what it does, briefly. Point out anything surprising.
{{/question}}
""",
    "ask/error": """\
---
description: Diagnose an error message or stack trace
kind: ask
tags: [debugging]
---

This is an error message or stack trace. Say what most likely caused it, then what to check or try first. If it could be several things, rank them.
""",
    "copy/bug-report": """\
---
description: A bug report skeleton, ready to paste into a tracker
kind: copy
tags: [work]
vars:
  summary: One line on what's wrong
  severity:
    options: [low, medium, high]
    default: medium
---

**Summary:** {{summary}}
**Severity:** {{severity}}
**Reported:** {{date}}

**Steps to reproduce**
1.

**Expected**

**Actual**
""",
}
