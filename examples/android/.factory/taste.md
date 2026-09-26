# Owner taste · example-org/demo-android · harvested 2026-09-01 · PRs merged 2025-09..2026-08 · 412 comments

### R001 · Prefer sealed UI state over boolean/nullable flags   [major]
applies: **/*ViewModel.kt, **/ui/**/*.kt
keywords: isLoading|isError|: String\?
why: owner wants one exhaustive `when` per screen state.
bad:  var isLoading = false; var error: String? = null
good: sealed interface UiState { data object Loading : UiState; data class Error(val msg: String) : UiState }
evidence: 9 comments · PRs #812 #790 #744 #702 #655

### R002 · Dimensions come from the design system   [nit]
applies: **/ui/**/*.kt
keywords: [0-9]+\.dp
why: spacing drifts from Figma when literals are used.
bad:  Modifier.padding(12.dp)
good: Modifier.padding(Spacing.M)
evidence: 4 comments · PRs #801 #777

### R003 · One ticket, one concern   [blocker]
applies: **
why: unrelated refactors hide the fix and block cherry-picks to release branches.
evidence: 6 comments · PRs #815 #760 #731

## Accepted exceptions
- R002 does not apply to preview-only composables (`@Preview`).

## How this reviewer works
- Reads the test first; a fix without a test for testable logic gets "changes requested".
