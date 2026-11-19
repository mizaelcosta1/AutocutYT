# AutoClips Timeline Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a non-destructive segment timeline, searchable transcript and FFmpeg timeline export to AutoClips v0.5.0 without copying AGPL source.

**Architecture:** A standalone pure-Python timeline state machine, FastAPI endpoints persisted under data-root/projects, an independent responsive HTML/JS editor, and an FFmpeg renderer reusing the project's render_clip. Original API workflows remain unchanged.

**Tech Stack:** Python 3.11+, FastAPI, JS, CSS, FFmpeg.

**Spec:** `docs/superpowers/specs/2026-11-19-editor-timeline-design.md`

## Global Constraints
- Preserve original source media and all v0.5 API routes.
- No remote deployments, social uploads, external API charges, or secrets in the package.
- No reuse of AGPL source code from OpenChatCut.
- Constrain projects to 80 segments and total edited duration at most 600 seconds for this first milestone.

## Review Focus
- Splitting exactly at a segment boundary must fail cleanly.
- Trimming outside the original duration must fail cleanly.
- Undo/redo should restore prior timeline states without corrupting history.
- SRT timestamps should be relative to the edited timeline, not original source.
- Final export must reject a missing or stale preview after any timeline change.

---
### Task 1: Pure timeline state machine
**Files:** Create `timeline.py`, `tests/test_timeline.py`.
**Interfaces:** `new_timeline(video_id,duration,digest)`, `apply_command(document,command,words=None)`, `rebase_words(document,words)`, `total_duration(document)`.
- [ ] Write unit tests for split/move/trim/add/remove/undo/redo and invalid boundaries.
- [ ] Run failing tests and record missing module.
- [ ] Implement minimal state machine with bounded operations.
- [ ] Run tests, verify pass.

### Task 2: FastAPI persistence/transcript endpoints
**Files:** Modify `server.py`; test `tests/test_timeline_api.py`.
**Interfaces:** GET/POST `/api/videos/{video_id}/timeline`, GET `/api/videos/{video_id}/transcript`, GET `/api/videos/{video_id}/timeline.srt`; `GET /studio/{video_id}`.
- [ ] Write tests against TestClient with temp video and monkeypatched catalog.
- [ ] Run failing endpoint tests.
- [ ] Implement timeline storage and endpoints with validation and optimistic version match.
- [ ] Run tests, verify pass.

### Task 3: Render and export timeline
**Files:** Create `timeline_export.py`, change `server.py`, test `tests/test_timeline_export.py`.
**Interfaces:** POST `/api/videos/{video_id}/timeline/render/{quality}`, GET `/api/videos/{video_id}/timeline/rendered/{quality}`.
- [ ] Write tests for preview-gating and a short synthetic concat rendered via FFmpeg.
- [ ] Run failing tests.
- [ ] Implement async render and safe access to completed files.
- [ ] Run tests, verify pass.

### Task 4: Responsive editor interface and regression suite
**Files:** Create `static/timeline.html`, `static/timeline.js`, `static/timeline.css`; add link to `static/index.html` and `static/app.js`, update `static/app.css`, README.
- [ ] Test route static delivery plus open-editor link smoke.
- [ ] Create accessible controls for segment operations, transcript search, captions, export with explicit preview approval.
- [ ] Run JS syntax checks and complete Python test suite; write QA summary with tested and untested features.

### Task 5: Package
- [ ] Check source and manifest for secrets/private media.
- [ ] Zip the standalone v0.6.0 directory excluding caches and private test footage.
- [ ] Verify the output ZIP contains expected test files and no originals.
