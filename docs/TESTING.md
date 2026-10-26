# Development validation record

Date: 26 October 2026. Runtime: Python 3.12.14, Linux x86_64, current Codex cloud machine. This is development validation, **not examiner-image assessment evidence**. The user explicitly deferred official Docker reproduction; no official image/tag or formal image result is claimed.

Command, working directory `/workspace/CSI`:

```sh
python3 -m unittest discover -s tests -v
```

Actual result at the completion of implementation: **71 tests executed, 71 passed, 0 failures, 0 errors, 0 skipped, 0 expected failures; 29.177 seconds.** Table-driven subtests add boundary cases without counting as separate unittest methods. The generated log contains the test names and runner summary. No zero-test or skipped run is treated as success.

| Layer | Actual checks |
|---|---|
| JSON/wire | Every split point of a Unicode frame; coalesced frames with trailing half-message; absolute partial age; zero/oversized headers; UTF-8/BOM/duplicates/nonfinite/surrogate rejection; buffer bound |
| Bank | Submitted two-quiz file; valid single-quiz replacement; 1–20 quizzes and 5–50 questions; exact decimal 1MB; extra fields; all required members; cross-quiz duplicate IDs; strict integers |
| Model | Session binding and uniqueness; request-number consumption order; host/readiness/capacity; ACK/result ordering; exact deadline equality; delayed ticks; accepted answer retained after departure; all-zero/tied winners; no rematch |
| Client | Strict response/event correlation; wrong token; repeated response; null error; input queue and whole-line limits; control escaping; bounded full-pending/stalled-input exit; sequence exhaustion |
| Transport | Partial sends preserve exact FIFO bytes; actual read/write budgets; absolute login/frame/send deadlines; RX pause/re-register; TX overflow cleanup; 64 connection/accept limits; event exhaustion |
| Paired CLI | Actual server.py and client.py subprocesses; full correct/wrong/duplicate/late-answer game; real five-second missing-answer timeout; identity release/reuse; no client bank dependency; malformed peer isolation; host disconnect; Ctrl+C and port restart |
| Concurrency | Ten real client processes; simultaneous two rooms with different quizzes, then two rooms with the same quiz; every question/result stays in its room; exact final scores and all tied winners verified |
| Packaging | Expected Codes structure, cache exclusion, refusal to overwrite an existing archive, missing/non-PDF report rejection |

Additional source inspection parsed runtime/test/tool imports with `ast` and found no third-party imports. `git diff --check` passed. Temporary bank files and child processes are owned and cleaned by the test harness; the server does not install dependencies or download anything.

The generated code-only ZIP was extracted into a fresh temporary directory; the same **71 tests passed there in 30.061 seconds**, with no missing relative files/modules. The submitted questions.json was also used by a separately started server on port 5000 with two actual CLI clients: both completed all five network-basics questions and each received the expected 800 points. The saved startup smoke sequence (LOGIN, LIST_QUIZZES, LOGOUT) exited 0 against that server. These checks remain cloud development results.

Resolved development issues include scripted client exit ordering and two incorrect test assumptions about stale queued snapshots/coincident room deadlines. No required failing check remains in this cloud run. This does not establish performance beyond the stated limits or behaviour in the still-unverified official image.

For the final assessed evidence, rerun the README commands in the official image at the submitted commit, record the image tag/digest and Python version, preserve the complete terminal output and exit status, and update the final PDF from those real results. Do not relabel these cloud results as official-image results. The team must also complete its own report layout, accurate contribution declaration and <=10-minute participation video.
