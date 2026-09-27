---
description: Baseline — capture the screens a story will change, as they are today.
---

# Baseline — the before picture

Install the current build (`factory android install`), reach each screen the plan names
(`factory android where|tap|open|wait`) and capture it with `factory snap before <label>`, one label per screen,
exactly the labels in the plan. Record how you reached each one in `steps`. Change nothing.
