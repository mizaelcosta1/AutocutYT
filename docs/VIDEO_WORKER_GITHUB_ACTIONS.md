# GitHub Actions video worker + Backblaze B2

## Important storage boundary

GitHub Actions is the **temporary processing worker**, not the media archive. The hosted runner is discarded after the job. Do not upload source videos as Actions artifacts or commit MP4 files to Git. The worker downloads a source object from Backblaze B2, analyzes it, and writes the manifest/transcript back to B2.

## Free-tier constraints

- GitHub Actions artifact storage on GitHub Free is limited; artifacts have a finite retention period. This workflow intentionally creates no video artifacts.
- Standard hosted runners are free for public repositories, subject to GitHub's usage and abuse policies.
- Blitz Cloud Free has 10 GB app storage but only 512 MB reserved memory shared across the account. It is suitable as a lightweight web/API host, not as the FFmpeg execution host.
- Blitz Cloud refuses a single HTTP request larger than 100 MB. The AutoClips chunked upload protocol must be retained; do not replace it with a single multipart request to the app.

## Backblaze B2 bucket and secrets

Create a private B2 bucket and an application key scoped only to that bucket, with read and write access. Add these repository Actions secrets:

- `B2_ENDPOINT`: the S3-compatible endpoint shown by Backblaze (for example, `https://s3.us-west-004.backblazeb2.com`).
- `B2_REGION`: the matching region, e.g. `us-west-004`.
- `B2_BUCKET`: bucket name.
- `B2_KEY_ID`: application key ID.
- `B2_APPLICATION_KEY`: application key secret.

Never commit these values or print them in workflow logs.

## Object key contract

The private object namespace is:

- Original: `users/<64-char-lowercase-owner-sha256>/originals/<video-id>/<filename>`
- Analysis manifest: `users/<owner-hash>/analysis/<video-id>/manifest.json`
- Transcript: `users/<owner-hash>/analysis/<video-id>/transcript.json`

The owner namespace must be a one-way SHA-256 hash of the normalized account identity, never an email address. Bucket access must remain private; the app should authorize every object lookup against the signed-in owner before issuing a short-lived signed URL or proxying the object.

## Trigger

The workflow supports:
- `workflow_dispatch` for an operator-controlled smoke test.
- `repository_dispatch` with event type `autoclips.analyze` for app-triggered analysis.

The backend still needs a dedicated GitHub dispatch credential (fine-grained token limited to this repository, with permission to dispatch workflows) and an integration that sends only the owner hash, video ID, and object key. Do not put B2 credentials in the dispatch payload.

## Processing limits

The worker rejects files larger than 8 GiB to leave room on the hosted runner's ephemeral disk. The B2 free tier has a 10 GB storage allowance; this is an account storage quota, not a guaranteed single-file allowance. Long jobs are bounded by the hosted runner's job timeout and available free Actions usage.

## Validation

Run the unit tests with:

```sh
python -m pytest -q tests/test_actions_video_worker.py
```

The first live B2 test cannot run until the bucket secrets exist. After adding them, dispatch the workflow with an already-uploaded test MP4 and verify that the manifest and transcript appear in the same owner's private B2 prefix. No Actions artifact is used.
