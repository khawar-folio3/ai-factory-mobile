---
description: Accept — prove every acceptance criterion on the device or in a test.
---

# Accept — QA against the criteria

Install the new build, capture `factory snap after <label>` for every baseline label plus any new screen, and
`factory snap diff`. For EACH acceptance criterion from the plan (same wording): `met` true/false and the evidence
(snapshot label, test name, what you observed). A criterion you could not check is `met: false`.
