# Baloney PR (test of auto PR reviewer)

This PR exists only to verify the DND AI automated PR reviewer fires.
It should NOT be merged. Feel free to close it after the review appears.

## Contents

- One (1) extremely serious function that adds two numbers.
- One (1) deeply philosophical comment about sandwiches.

```python
def add(a, b):
    # A sandwich, like clean code, is mostly about what you leave out.
    return a + b
```

Expected reviewer behavior: a `COMMENT` review containing
`<!-- dnd-ai-automated-review -->` posted by the workflow.
