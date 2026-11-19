# AutoClips Use-case and Comfy Cloud QA Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Each correction must reproduce before implementation and pass thereafter.

**Goal:** audit action→effect for the AutoClips v0.7.0 deployed UI, repair verified bugs, and offer honest Comfy Cloud workflow launching without pretending Google OAuth gives Comfy execution credentials.

**Architecture:** Retain FastAPI/FFmpeg and existing Google OIDC session. Add narrowly scoped backend validations and a read-only creative workflow catalog configured in private Railway environment; its UI opens the authenticated Comfy Cloud canvas in a new tab. Execute no paid Comfy jobs implicitly; direct rendering stays local until server-side Comfy authentication is provisioned.

**Tech Stack:** Python 3.11+, FastAPI, PyJWT, httpx, FFmpeg, Playwright for manual browser QA, Railway, Comfy Cloud MCP (connected to ChatGPT only).

**Spec:** README.md, OPENCHATCUT_ROADMAP.md, user request from 2026-11-19.

## Global Constraints

- No API/Google/Comfy tokens in public repository, logs or generated files.
- Preserve explicit authorization for YouTube publishing and cloud data transfers.
- Any creative workflow card must perform its advertised action; no fake generative execution.
- All functionality confirmed separately by backend test, browser smoke, and deployed readback.

## Review Focus

1. YouTube resumable 308 acknowledges a partial chunk — next PUT starts at the acknowledged byte, without skipping content.
2. VOD downloader only accepts the three services advertised in the UI and refuses untrusted hosts before scheduling work.
3. Upload bigger than available storage fails before accepting/scheduling bytes; leaves no session files.
4. Google-authenticated user sees only curated Comfy workflow links, never secrets; absence of configuration produces explicit non-operational state.
5. AI title generation button is not advertised as available when no corresponding model exists.

---

### Task 1: YouTube resumable correctness
- Create tests/test_usecases_regression.py with partial 308, no-range retry, normal success.
- RED: run targeted tests before fix.
- Modify integrations.py upload loop for 308 acknowledgment and seek behavior; reject invalid/non-progressing ranges.
- GREEN: targeted tests and full suite.

### Task 2: VOD upload safety and user intent
- Add tests for VOD host whitelist and storage headroom.
- RED: run tests before fix.
- Modify server.py import_vod to reuse platform() validation and upload_start to enforce usable free disk.
- GREEN: targeted tests and full suite.

### Task 3: Honest Comfy Cloud integration
- Add tests for catalog empty/valid/invalid IDs; optional readiness indicator for selected title model.
- RED: run tests before fix.
- Modify server.py GET /api/creative/workflows to expose configured, validated workflow action URLs to authenticated users only.
- Modify static/index.html / connections.js: cards launch Comfy Cloud editor via HTTPS deep link, not pretend in-app generation. Add explicit credit/auth explanatory text, title model state and version update.
- Configure private Railway variable with curated saved workflow IDs after tests; do not expose them in GitHub.
- GREEN: targeted tests, Python and JS syntax, full suite, local browser smoke and Railway deployment health.

### Task 4: Final verification and release
- Check repo diff for keys or tokens and test logs. Run full Python suite, JS parsing and browser use-cases.
- Commit/push through GitHub connector into existing public repo; inspect fresh commit and Railway status.
- Export QA matrix report with executed/pending and blockers; avoid unverifiable success claims for real Google channel OAuth, cloud generation and social publication.
