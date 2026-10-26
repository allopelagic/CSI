# Networked Quiz Tournament — Development Documentation source

## 1. Document control and executive summary

Version: 1.0 development source. Date: 26 October 2026. Module: CSI201 Coursework I.

This is an editable engineering record, **not the final assessed PDF**. Before submission, enter the registered team name, members, student IDs, document owner, reviewers and a real revision history. Convert the completed text to A4 single-column PDF, readable 11pt body text, numbered headings and pages, with consistent captions and IEEE references. The recommended report length is 12–18 pages excluding front matter/references/appendices.

The implementation is a Python 3.12 Standard Library CLI client/server using raw TCP. It supports server-validated replacement banks, unique online identities, multi-player lobbies, authoritative timed quizzes, individualized results and independent concurrent rooms. All state is volatile. Version 1 has no passwords, TLS or reconnection recovery. No GUI, third-party dependencies, database or Internet API is used.

Cloud development validation is recorded separately in TESTING.md. The official examiner image/tag was not provided and, at the user's request, official-image verification is deferred. No cloud result in this source should be described as official Docker evidence. The team must supply actual official-image test results, its own diagrams/screenshots where appropriate, and the participation video.

## 2. Requirements and acceptance traceability

Stakeholders are players using basic terminals, the room host coordinating a match, an operator starting/stopping the server, developers maintaining the shared protocol, and the examiner replacing the bank and reproducing behaviour. The main constraints are Python 3.12 Standard Library, examiner-provided Docker, raw TCP, fixed launch arguments, CLI-only operation and server-only access to the question file.

| ID | Acceptance criterion | Design element | Executable verification |
|---|---|---|---|
| FR01 / Task 1 | Complete bank validated before listening; invalid bank exits nonzero without READY | question_bank.load_bank, server.main | QuestionBankTests; StartupTests |
| FR02 / Task 2 | Online ID and name separately unique; connection-bound session; reuse only after release | Game.sessions/users/names; LOGIN/LOGOUT | GameTests.test_identity_uniqueness_and_connection_binding; IntegrationTests.test_duplicate_identity_and_reuse_after_logout |
| FR03 / Task 3 | Catalogue omits questions/options/answers; joinable-room list filtered | Game._operation public queries | GameTests.test_catalogue_has_no_question_information; integration full workflow |
| FR04 / Task 3 | Create/join/readiness/host/leave work with capacity 16 | Room members in join order | GameTests.test_room_business_rules_and_same_ready_is_silent; test_capacity_room_number_and_room_limit |
| FR05 / Task 3 | Only host starts, at least two players, all ready | START_QUIZ transition | GameTests.test_room_business_rules_and_same_ready_is_silent |
| FR06 / Task 4 | Questions use file order, their own limits/points; all valid active answers or deadline closes | Room.index/deadline; Game._open/_maybe_close/tick | GameTests deadline tests; IntegrationTests full workflow and timeout |
| FR07 / Task 4 | At most one valid answer; malformed option does not consume chance; duplicate/late rejected | Game._answer, answers keyed by original session | GameTests.test_invalid_duplicate_and_last_ack_before_result; integration full workflow |
| FR08 / Task 4 | Open scores and ACKs reveal no correctness; result after closure | Single scoring gate and per-recipient results | GameTests.test_start_order_and_no_open_answer_leak; integration full workflow |
| FR09 / Task 4 | Frozen roster, competition ranks, all tied winners, inactive records retained | Participant, Game.board/_close_question | GameTests finished/all-zero/disconnect tests |
| FR10 / Task 5 | Ten actual clients, two simultaneous rooms, same/different quiz isolation | Independent Room instances; read-only bank | IntegrationTests.test_ten_real_clients_two_independent_rooms_different_and_same_quiz |
| FR11 / Task 6 | Host reassignment, retained answer after disconnect, remaining player continues, empty room removed | Game._leave/disconnect | GameTests departure/reconnect/single-player tests; integration disconnect test |
| FR12 / Task 6 | Ctrl+C/EOF close resources; notify room members; same port restarts | Server.shutdown, Client._quit/run | IntegrationTests Ctrl+C/restart and stdin EOF tests |
| NFR01 | Arbitrary fragmentation/coalescing, exact UTF-8 byte lengths, strict JSON | protocol.Framer/loads/encode | FramingTests; integration fragmented/coalesced requests |
| NFR02 | Sequence and auth errors affect one request/connection, not other rooms | Fixed validation chain, immutable Delivery snapshots | GameTests sequence/token tests; integration malformed peer test |
| NFR03 | Bounded RX/TX/connections/commands and absolute timeout handling | protocol constants; transport budgets | TransportTests; ClientTests input/pending tests |
| NFR04 | Server-only mutable state, no sleep-based deadlines, no shared room progress | Single selectors owner; injected monotonic clock | GameTests timing/isolation tests; source review |
| NFR05 | Safe terminal display and sensitive logging | client.safe/display; no per-request server logs | ClientTests.test_display_escapes_terminal_controls; source review |
| NFR06 | Standard-library-only paired executable with supplied launch interface | server.py/client.py, relative modules and argument paths | README commands, integration processes, official-image rerun pending |

The requirement IDs remain stable when tests change. Supplement the matrix with the final official result file, exact commit and defect IDs; a test name alone is not submitted evidence.

## 3. Architecture and deployment

Each CLI client process holds one long-lived TCP connection. A daemon stdin thread only produces bounded input; the client's main thread owns network I/O, pending requests, room state, correlation and display. The server's single selectors thread owns all identities, membership, tournament records and output queues. Only the server calls load_bank with the operator's file path. The client can run from a directory containing only client.py and protocol.py, without a bank.

The server loads a complete, validated in-memory copy before binding. Multiple rooms reference quiz data without modifying it; progress, answers, timers and participant scores are room-local. The TCP boundary is untrusted. Incoming peer bytes must pass framing, JSON, schema, numbering, identity and state checks before state changes. The server is authoritative; local client state supports presentation only.

All assessed processes run inside the supplied Docker image. Use one container and multiple docker exec terminals; loopback refers to that same container. No ports need publishing to the host for this workflow. The project supplies no Dockerfile. See README for exact process launch and test arguments; the image placeholder must be replaced with the published course tag.

## 4. Module and data design

| Module | Responsibility and ownership |
|---|---|
| protocol.py | Version, limits, six-field envelope, strict JSON, bounded length-prefix decoder, request/server schemas |
| question_bank.py | Bounded binary read, whole-file validation, contextual field diagnostics; never imported by client |
| game.py | Session, Participant, Room, immutable Delivery/Close intents; all business state and scoring |
| server_network.py | Connection wire buffers, partial-send suffixes, event counters, selector interests, fairness, timeout/close queues |
| server.py | Required CLI, validate-before-bind, readiness signal and process exit |
| client.py | Required CLI, stdin queue, command translation, response/event validation, terminal-safe output and bounded exit |

A Session starts unauthenticated with a request high-water mark. Successful login allocates s- plus 32 random lowercase hexadecimal characters using secrets.token_hex(16). User ID and name are stored exactly. Membership points to at most one room. A Room has an ordered member list and current host, one quiz, state, current index, absolute deadline, current answers, closed-question IDs and a frozen Participant roster. A Participant stores the original token, ID, name, accumulated score and active flag. Its record outlives its disconnected session, until the room becomes empty.

Delivery copies the recipient identity, session snapshot and payload with deepcopy. A response or event therefore cannot accidentally reflect a later membership change. Transport assigns event IDs only after successfully enqueueing a complete encoded frame. Tokens never appear in player/room payloads or normal CLI output.

The maximum bank is 1,000,000 bytes, interpreted as decimal 1 MB. Title/text/name require printable, nonblank Unicode; description may be empty/space-only, options may contain control characters but cannot execute terminal controls. IDs use ASCII letters/digits/underscore/hyphen. Additional root/quiz/question members are permitted; options must have exactly A/B/C/D. Integer checks explicitly reject bool and float. Any invalid required field rejects the complete file, including duplicate question IDs across different quizzes.

## 5. State transitions, timing and scoring

Stable room states are LOBBY, QUESTION_OPEN and FINISHED. A new room automatically includes its unready creator as host. Join preserves existing ready flags and appends a new unready member. Ready is idempotent; an unchanged value produces a success response without a status broadcast. Starting freezes the participants and opens the first question only after all start conditions pass. There is no public waiting state or rematch in the same room.

Opening uses a fresh monotonic reading plus that question's time_limit. Both the event-loop scheduler and answer handling check the absolute deadline. Equality rejects an answer. A completed JSON message is the unit of validation; header arrival, a half-body or a client timestamp does not reserve time. A delayed tick closes only the expired current question; the next question receives its full limit starting from its actual opening time.

The first valid answer is recorded under the participant's original session. Scores remain unchanged until closure. _close_question clears the current deadline and records the closed ID before adding awards. The single thread and absence of callback-owned mutable game state prevent two threads scoring one question. Repeated timer ticks at the same time do not score again. Each participant with an accepted correct answer gets exactly points; an accepted answer survives that player's departure. Inactive players receive zero on subsequent unanswered questions.

After an answer, all active participants having valid answers triggers early closure. After a departure, the server first updates membership/host and broadcasts status, then rechecks this condition. Empty rooms are deleted without simulating the remaining quiz. A remaining single player continues. Closing builds each member's own answer/award result, then opens the next question or publishes the final board once. Ranking sorts by (-score, user_id); rank equals one plus the number of strictly higher scores. All rank-1 IDs are winners, including inactive players if their score remains highest.

## 6. Application protocol and ordering

PROTOCOL.md is the field-level baseline: twelve request/response operations, six event types and fourteen error codes. Each frame contains four unsigned big-endian bytes giving the UTF-8 JSON body length (1–65536), followed by exactly that body. JSON is compact, Unicode-preserving, and finite-only. Duplicate keys at any nesting level, BOM, invalid UTF-8, lone surrogates and non-object roots are fatal to that connection.

Every message has exactly version/type/request_id/event_id/session_id/payload. Requests carry req-N, no event ID, and the appropriate bound token. Responses carry the original request ID. Events carry null request ID and per-connection evt-N starting at one. Sequence values never wrap; exhaustion closes in bounded time. LOGIN success obtains its token only from the envelope. A token from another socket does not transfer identity.

The validation chain is: complete frame/JSON, common schema/version, strictly newer request number, immediately consume that new number, known request type and exact payload schema, session binding, operation state and arguments, one mutation with ordered output. Invalid common structures do not consume numbers. An invalid business request with a valid new number does consume it. Error responses use a safely parsed request ID or null, and the server's own bound token rather than a forged submitted token.

The per-connection queue preserves prior output. For START, the requester gets START_QUIZ_RESPONSE, then QUIZ_STARTED_EVENT, then QUESTION_EVENT. Other members get the two events. For the last valid answer, that requester gets ANSWER_RESPONSE, then QUESTION_RESULT_EVENT, then next QUESTION_EVENT or QUIZ_FINISHED_EVENT. Other members get result then next/final. If the timer expires before a late answer is processed, its result/next events may precede that answer's QUESTION_CLOSED response. The client does not discard intervening events while waiting for a response.

Client success responses must match both a pending request ID and the expected response type. Correlated errors remove that pending ID; null-ID errors only display. Unknown/duplicate response IDs, wrong tokens, skipped/repeated event IDs and malformed server messages close the client with a protocol diagnostic. Local dependency barriers wait for authoritative responses before issuing room-state-dependent commands. No automatic retransmission risks a second answer.

## 7. Network concurrency and bounded resources

The server uses DefaultSelector and nonblocking raw sockets. Each iteration accepts at most 16 connections; per connection it reads/writes at most 32KiB and processes at most eight complete frames. Buffered complete frames cause an immediate next iteration, so a coalesced batch does not require another read-ready event. select waits at most 0.1 seconds and no later than the closest room/connection deadline.

RX retains at most 128KiB of wire bytes, including headers. recv uses only the remaining capacity. TX retains at most 256KiB of unsent data; enqueue beyond this limit marks the peer closing. Partial sends keep memoryview suffixes, decrement actual unsent bytes and reset send-progress time only after real progress. If RX is full and TX empty, the socket is unregistered until capacity returns rather than using an illegal zero selector mask.

The three ten-second timeouts have different origins: accept time for login, first application-received byte of a trailing incomplete frame for framing, and the most recent actual send (or first enqueue to an empty queue) while TX remains nonempty. Framer retains segment timestamps to distinguish a trailing partial frame from queued complete frames; drip bytes do not renew the age.

Marking closing immediately stops business, including buffered requests. The current transition finishes snapshotting/queueing outputs before an iterative deduplicated cleanup queue releases identities and memberships. Cleanup broadcasts can mark another slow connection; that peer joins the queue rather than recursing into mutable membership lists. LOGOUT and shutdown keep FIFO and allow at most 0.5 seconds to flush. Shutdown stops opening new questions.

Clients bound pending requests to 64, input to 128 lines, line length to 512 characters, and each loop to eight commands/frames. Overlong lines are discarded in their entirety. Pending requests warn once after five seconds without retry. Full pending queues or stalled control requests cannot prevent an explicit bounded exit. The daemon input thread cannot keep the process alive after network shutdown.

## 8. Implementation record and trade-offs

Separating Game from transport enables precise deadline-equality and double-trigger checks without waiting in real time. Real subprocess tests additionally exercise the submitted CLI pair so that passing model tests cannot conceal an unusable client. A single owner avoids locks around rooms/scores, at the cost of bounded CPU work per loop and no parallel game computations. This is suitable for the fixed 64-connection scope.

The input thread keeps blocking stdin away from the networking loop. Machine-readable, one-line JSON payloads give complete CLI output without cursor control and support repeatable test drivers. Deep snapshots spend bounded memory to make ordering and disconnect semantics easier to reason about. Uniform shared schemas reduce client/server drift but are not a substitute for testing real transport behaviour.

Development defects found and corrected: a scripted /quit initially could bypass queued login-dependent commands; it now waits for normal control responses and only bypasses a stalled/full pending queue for bounded exit. Two tests initially selected an old queued membership snapshot or assumed two simultaneously due rooms would not both advance; those test expectations were corrected without weakening the room/timer assertions. The final per-run results belong in TESTING.md and official evidence after rerun.

## 9. Verification, validation and evidence record

Run python3 -m unittest discover -s tests -v. The suite contains deterministic state/validation/transport tests and real TCP server/client subprocess tests. Synthetic replacement banks deliberately vary IDs, counts and point values; fake clocks precisely exercise boundaries while a real five-second test checks the wall-time network path. The ten-client test runs both different-quiz and same-quiz simultaneous-room scenarios and checks every member's question, result, final score and winner set.

Record for each official run: requirement ID, test/case, prerequisites and commands, expected result, actual result, pass/fail/skipped/unrun, image tag and digest, Python version, source commit, execution date and evidence filename. Save original terminal output with the real exit status; do not replace failures with screenshots of a different revision. Selective terminal evidence complements the procedure/result table and never replaces it. No evidence images or experimental data are fabricated by this project.

Official-image verification and the final report layout remain the team's actions. The code packaging helper accepts a real final PDF; code-only ZIP mode explicitly omits documentation and is not a complete assessment submission.

## 10. Reliability, security and limitations

Malformed frames close only their socket; schema/auth/state errors return one bounded, printable error without disconnecting unrelated clients. Correct answers are absent from catalogue, open questions, ACKs and error logs. Correctness becomes public only at closure. User answers appear only in that user's result. Names/options are validated or escaped before terminal display.

The process limits and timeouts reduce simple memory/slow-reader/slow-writer abuse within the course scope. They do not provide Internet-grade DoS protection. No TLS/passwords means identity is an online uniqueness label, not proof of a human identity; an off-path client cannot inherit another TCP-bound token but network eavesdropping is outside v1's protection. Wall-clock deadlines are unsuitable because clock corrections can move them; monotonic avoids that defect. There is no persistence, process failover, reconnection recovery or guarantee of a maximum total response latency under CPU starvation.

Possible later work includes TLS/authentication with an explicitly revised protocol, durable audit/recovery with token redaction, independent interoperability tests and measured latency/memory benchmarks. These are not claimed implemented features.

## 11. Project management and individual contribution

Use actual repository commits and reviews to record the milestones: protocol agreement, core model, transport/client, module tests, paired workflow, concurrency/robustness tests, official-image reproduction, report and video. Add the real team work breakdown, planned/actual dates, review responsibilities and reasons for changes. Do not infer human authorship from AI-generated files or substitute proposed percentages for actual work.

| Registered member / student ID | Actual implementation, testing, review and documentation evidence | Verified contribution |
|---|---|---|
| To be completed by the team | Commit IDs, test records, report sections and demonstration responsibility | To be completed |

The final percentages must sum to 100%. Every member must understand and explain the framing, selectors, buffering, timing/scoring, validation, error handling and tests. Disclose AI assistance under the course policy; the team remains responsible for review and verification. A video of no more than ten minutes must involve every member and demonstrate Tasks 1–5 in the official image.

## 12. References

[1] Module team, “CSI201 Introduction to Networking: Coursework I Specification, Networked Quiz Tournament,” supplied task sheet, 2026.

[2] Team protocol baseline, “双人开发协议准则 v2,” supplied PROTOCOL(1).md, 26 Oct. 2026; copied in PROTOCOL.md.

[3] Python Software Foundation, “socket — Low-level networking interface,” Python 3.12 documentation. https://docs.python.org/3.12/library/socket.html

[4] Python Software Foundation, “selectors — High-level I/O multiplexing,” Python 3.12 documentation. https://docs.python.org/3.12/library/selectors.html

[5] Python Software Foundation, “time.monotonic,” Python 3.12 documentation. https://docs.python.org/3.12/library/time.html#time.monotonic

[6] Python Software Foundation, “json” and “unittest,” Python 3.12 documentation. https://docs.python.org/3.12/library/json.html ; https://docs.python.org/3.12/library/unittest.html

[7] OpenAI, ChatGPT/Codex, AI assistance with implementation, test development and documentation drafting, 26 Oct. 2026. The team must supplement this acknowledgement with its actual review/validation record and any course-required disclosure format.
