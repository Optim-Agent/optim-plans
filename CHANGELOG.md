# Changelog

This changelog records the notable project revisions before the repository was
prepared for public release with a fresh Git history.

The format follows the practical parts of Keep a Changelog: newest release
first, ISO dates, and grouped change types.

## Unreleased

### Changed

- Codex read-only worker commands now load normal user configuration instead of
  suppressing it.
- `.agents/plugins/marketplace.json` is no longer hidden from public commits.

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
