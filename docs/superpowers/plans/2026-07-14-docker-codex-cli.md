# Docker Codex CLI Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Docker deployments detect and execute the host-authenticated Codex CLI without putting credentials into the image.

**Architecture:** Install a pinned `@openai/codex` package in a dedicated Node builder stage, extract its platform-native Linux binary, and copy only that binary into the runtime image. Mount `${HOME}/.codex` read-only so the existing backend can copy authentication into its per-request temporary Codex home.

**Tech Stack:** Docker multi-stage builds, Docker Compose, `@openai/codex`, FastAPI, pytest

---

### Task 1: Establish baseline and impact

**Files:** No changes.

- [x] Confirm host `codex --version` reports `0.144.3`.
- [x] Confirm the old container cannot resolve `codex`.
- [x] Run GitNexus impact on `_resolve_command`; avoid changing the HIGH-risk backend symbol chain.
- [x] Run `uv run pytest tests/test_ai_provider.py -q`; expect 15 passing tests.

### Task 2: Package Codex in Docker

**Files:**
- Modify: `Dockerfile`

- [x] Add `CODEX_CLI_VERSION=0.144.3` as a reproducible build argument.
- [x] Install `@openai/codex` in `codex-builder` and locate `*/vendor/*/bin/codex`.
- [x] Copy the native binary to `/opt/codex-native` and verify its version in the builder.
- [x] Copy only `/opt/codex-native` to runtime `/usr/local/bin/codex` and verify it there.
- [x] Keep runtime Node.js conditional on stock-sdk because the extracted Codex binary is self-contained.

### Task 3: Reuse host authentication safely

**Files:**
- Modify: `docker-compose.yml`

- [x] Pass `CODEX_CLI_VERSION` through Compose.
- [x] Mount `${HOME}/.codex:/root/.codex:ro`.
- [x] Run `docker compose config` and confirm the version and read-only mount.

### Task 4: Document behavior

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/plans/2026-07-14-docker-codex-cli.md`

- [x] Document the pinned version, version override, host login requirement, and credential security boundary.
- [ ] Run `git diff --check`.
- [ ] Run `gitnexus detect-changes` before the documentation commit.

### Task 5: End-to-end verification and PR

**Files:** No changes.

- [x] Build the Codex builder stage and verify `codex-cli 0.144.3`.
- [x] Copy the extracted binary into the current TickFlow runtime image and verify it executes without Node.js.
- [ ] Recreate the app with the Codex-enabled image and existing data.
- [ ] Verify `/api/settings` reports Codex configured.
- [ ] POST `/api/strategies/ai/test` and expect `{"ok":true}` with `OK`.
- [ ] Re-run provider tests and inspect final Git/GitNexus scope.
- [ ] Push `codex/docker-codex-cli` and open a Draft PR against `main`.

### Known external build issue

A cold full-image build currently reaches the Codex stages successfully, then fails in the pre-existing backend dependency layer because `backend/uv.lock` contains direct Tsinghua mirror wheel URLs returning HTTP 403. This PR does not rewrite the lockfile or mix that unrelated dependency-source problem into the Codex fix; runtime compatibility is verified separately against the existing TickFlow image.
