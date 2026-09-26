# Tribal knowledge

Rules the team knows but no linter enforces. Same format as `taste.md`; the guardrail loads a rule only when
its `applies` globs match a changed file (and, if given, its `keywords` regex hits an added line).
IDs use the `K` prefix and never change once published.

### K001 · Network calls go through the repository layer   [major]
applies: **/ui/**/*.kt, **/*ViewModel.kt
keywords: Retrofit|OkHttp|HttpURLConnection|\.execute\(\)
why: screens must stay testable without a network; the repository owns retries and caching.
bad:  val body = api.getProfile().execute()   // inside a ViewModel
good: val profile = profileRepository.profile()

## Accepted exceptions
- Debug-only screens under `**/debug/**` may call the API directly.
