# Local Plan Templates

Use these templates for local/offline plans. When drafting in chat, output only the body. When saving, prepend frontmatter with only `name` and `description`.

## Frontmatter

```markdown
---
name: <plan-name>
description: <1-line summary>
---
```

## Implementation Plan Body

```markdown
# Plan

<1-3 sentences: intent, scope, and approach.>

## Requirements
- <Requirement 1>
- <Requirement 2>

## Scope
- In:
- Out:

## Files and entry points
- <File/module/entry point 1>
- <File/module/entry point 2>

## Data model / API changes
- <If applicable, describe schema or contract changes>

## Action items
[ ] <Step 1>
[ ] <Step 2>
[ ] <Step 3>
[ ] <Step 4>
[ ] <Step 5>
[ ] <Step 6>

## Testing and validation
- <Tests, commands, or validation steps>

## Risks and edge cases
- <Risk 1>
- <Risk 2>

## Open questions
- <Question 1>
- <Question 2>
```

## Overview Plan Body

```markdown
# Plan

<1-3 sentences: intent and scope of the overview.>

## Overview
<Describe the system, flow, or architecture at a high level.>

## Diagrams
<Include text or Mermaid diagrams if helpful.>

## Key file references
- <File/module/entry point 1>
- <File/module/entry point 2>

## Auth / routing / behavior notes
- <Capture relevant differences, such as auth modes or routing paths.>

## Current status
- <What is live today vs pending work, if known.>

## Action items
- None (overview only).

## Testing and validation
- None (overview only).

## Risks and edge cases
- None (overview only).

## Open questions
- None.
```
