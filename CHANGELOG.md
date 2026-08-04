# Changelog

This changelog records the notable project revisions before the repository was
prepared for public release with a fresh Git history.

The format follows the practical parts of Keep a Changelog: newest release
first, ISO dates, and grouped change types.

## Unreleased

## 0.2.2 - 2026-08-04

### Added

- Added `deep-research-plan` for stronger-than-huge planning with downloaded
  refs, graphify JSON, and per-reference adoption questions.
- Added `research-and-plan` as a compatibility alias for `search-and-plan`.

### Changed

- Bumped Claude and Codex plugin metadata to `0.2.2`.

## 0.2.1 - 2026-07-31

### Added

- Added the `search-and-plan` skill for controller-backed research before
  optim-plans planning.

### Changed

- Bumped Claude and Codex plugin metadata to `0.2.1`.

## 0.2.0 - 2026-07-28

### Added

- Added the `review-and-plan` skill for multi-angle repository improvement
  review before optim-plans handoff.
- Added Codex agent metadata and README coverage for `review-and-plan`.

### Changed

- Bumped Claude and Codex plugin metadata to `0.2.0`.

## 0.1.2 - 2026-07-27

### Added

- Added a distinct read-only validator worker role and executor -> validator
  retry loop before controller verification, Git audits, and checkpoints.
- Added `0.1.2` execution manifest fields for protocol/schema version,
  validator config and prompt binding, item check IDs, validator retry limits,
  and validator result events.
- Added validator recovery CLI commands, including `fail-validator --reason
  interrupted`.

### Changed

- `prepare-execution` now requires executor and validator worker preferences
  before immutable manifest recording.
- Bumped Claude and Codex plugin metadata to `0.1.2`.

### Fixed

- Validator failures now preserve unsafe worktrees, while valid validator
  rejections can restore safely with bounded feedback for the next executor
  attempt.

### Changed

- First failed execution retry now restores automatically; approval is only
  required if that retry fails again.
- Integrated finish now runs the full local proof before recording terminal
  success, and proof failures stay in integration recovery.
- Successful delegated worker smoke tests now cache exact worker blocks in
  Git-common config for matching later manifests.
- Planning agent and same-platform worker preferences are now reused from the
  repository's Git common directory, with pre-manifest worker configuration.
- Codex read-only worker commands now load normal user configuration instead of
  suppressing it.
- `.agents/plugins/marketplace.json` is no longer hidden from public commits.
- Public GitHub links now point at the live `Optim-Agent/optim-plans` repository.

### Fixed

- Claude executor manifests with `--setting-sources ""` now pass controller
  validation and subprocess launch.
- Claude executor launches now run through an explicit foreground
  `optim-plans-executor` subagent when delegated execution is selected.

## 0.1.1 - 2026-07-26

### Added

- Added the `analyze-and-plan` skill for evidence-driven problem analysis before
  handing off to the appropriate optim-plans planning level.

### Changed

- Bumped Claude and Codex plugin metadata to `0.1.1`.

## 0.1.0 - 2026-07-24

### Added

- Initial optim-plans plugin foundation for Claude and Codex.
- Planning levels: `mini-plan`, `small-plan`, `plan`, `big-plan`, and
  `huge-plan`.
- Codex-compatible hooks, skill presets, and plugin smoke coverage.
- Foreground reviewer/criticizer flow, background model chooser restoration,
  and foreground worker runner.
- Manifest-bound execution lifecycle with controller-owned run worktrees,
  serial checkpoints, final audits, and finish outcomes.
- README workflow pitch, planning level examples, skill badges, and Claude/Codex
  quick-start instructions.

### Changed

- Planning requests now require a first-turn planning question and
  auto-complete choices where appropriate.
- Criticizer revisions are gated on recorded user answers.
- Execution is controller-verified with path audits, protected Git metadata
  audits, retry approval, and evidence-backed terminal outcomes.
- Test repositories now use `main` by default.
- Executor behavior no longer has strict Codex config or worker time limits.

### Fixed

- Corrected Claude local marketplace install documentation.
- Fixed Codex executor strict-config compatibility.

### Removed

- Removed root plan artifacts from tracking and ignored agent-local state.
- Removed brainstorm planning artifact requirements.
- Removed executor time limits while keeping verification timeouts.
