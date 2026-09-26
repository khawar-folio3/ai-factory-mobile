# Adding a platform

A platform is a class implementing `mobile_factory.platforms.base.Platform`:

| Method | Contract |
|---|---|
| `doctor()` | list of `Check`s for tools, device, config |
| `ensure_device()` | return a ready device id, booting one if configured |
| `build_install(log_dir, launch)` | build + install the debug variant; full log to `log_dir`, failing tail in the summary |
| `checks(files, log_dir)` | lint + unit tests for the modules the change touched |
| `modules_for(files)` | modules a set of paths belongs to (used for risk) |
| `screen_state()` | **stable, diffable text** of what is on screen: screen/controller, stack, visible labels; no clock/battery |
| `screenshot(dest)` | PNG, after the screen settled |
| `tap`, `wait_for`, `open_link`, `back`, `launch` | navigation primitives |
| `run_flow(flow, log_dir)` | run a Maestro flow |

`snapshot()` and `snapshot_diff()` come for free from the base class.

## iOS (planned)

- build/install: `xcodebuild -scheme … -destination 'platform=iOS Simulator,…'` + `xcrun simctl install/launch`
- screen state: `maestro hierarchy` (or `idb ui describe-all`), reduced to view controller + labels
- screenshots: `xcrun simctl io booted screenshot`
- deeplinks: `xcrun simctl openurl booted <url>`
- checks: `xcodebuild test` for touched targets, `swiftlint` on changed files

Cross-platform parity (run the same Maestro flow on both, compare screen states) builds on having both platforms
behind this interface. Shared accessibility identifiers across the two apps make that comparison reliable.
