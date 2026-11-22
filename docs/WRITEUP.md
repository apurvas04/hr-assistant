# Building an HR assistant, step by step: method, results and lessons

## 1\. Goal

Build an internal HR assistant that answers policy questions with citations, retrieves personal
data through tools, and **never shows anyone something they shouldn't see**. The build order was
chosen so that every later change could be measured against an earlier baseline: plain retrieval
first, then evaluation, and only then the refinements.

## 2\. System in brief

A signed-in user asks a question. A gateway checks the session and rate limit; an input guard
screens the text; a cache answers repeats; otherwise an orchestrator retrieves policy chunks
(filtered by the user's role), a router picks a small or large model, and the model may call
tools (leave balance, profile, team list) served over MCP as that user. An output guard checks the
answer for ungrounded personal data and missing citations before it is shown and cached. Every
stage emits a trace span. Prompts live in versioned files.

The one rule that ties it together: **the same user identity flows into the retrieval filter, the
tool calls, the cache key and the traces.** Each of those was a place where a leak could have
happened, and each has its own test.

## 3\. Method: evaluate before improving

* **Question set.** 50 questions written against the actual policies: 21 direct, 5 paraphrased,
2 needing several sections, 4 "version traps" (the archived 2024 policy gives a different
answer), 10 deliberately harder ones that avoid the policy's own wording, 5 about restricted
documents and 3 out-of-scope. Each has an expected document and section, and (for answers) a
key fact that must appear.
* **Metrics.** Hit rate at ranks 1, 3 and 5 (document and exact chunk), mean reciprocal rank,
how often the archived policy appears, and a leak count (restricted chunks returned to an
ordinary employee).
* **Caveat found early:** on a corpus of about 40 chunks, hit@5 covers an eighth of everything, so
it was 100% from the start and told us nothing. hit@1 and MRR are the numbers that move.

## 4\. Results by step

**Retrieval (Steps 1–3).** The first prototype already found the right *document* 97% of the time
at rank 1, but only 89% (40-question set) of the right *chunk*, and the archived policy leaked into
37.5% of top-5 results. Four configurations, compared on the same 50 questions:

|Configuration|chunk hit@1|MRR|archived in top 5|version traps|
|-|-|-|-|-|
|Vector only|0.830|0.901|34%|3/4|
|+ exclude archived|0.872|0.923|0%|4/4|
|+ hybrid keyword search|0.766|0.838|0%|4/4|
|+ reranker (no hybrid)|0.979|0.989|0%|4/4|

Hiding archived policies fixed the version problem. **Hybrid search made things worse**: the
keyword side matched common words ("leave", "days") that appear in many chunks of a small corpus,
and fusing the two lists pushed plausible-but-wrong chunks up. A cross-encoder reranker fixed
ranking; a follow-up run showed it worked just as well on plain vector candidates, so the keyword
path was removed entirely. That also removed a second query path where an access filter could have been forgotten.

**Access control (Step 4).** The role filter runs inside the vector query, before ranking. Result:
0 of 50 questions returned a restricted chunk to an ordinary employee, and chunk@1 did not drop.
Access tests passed 30 of 31; the one failure was an API quota error in an end-to-end test, not
an access bug. The functions fail closed: no user is a `TypeError`, an unknown role a `PermissionError`.

**Tools and MCP (Step 5).** Three tools with one authorisation rule (yourself; a manager's direct
reports; HR everyone), a 21-case access matrix, and a design choice worth recording: the identity
is set in the MCP server's environment when the orchestrator starts it, so it is not in the tool schema
and the model cannot change it. "Not allowed" and "doesn't exist" return the same error, so the
assistant can't be used to learn which names are real. Tool tests: 31 passed. Tool eval: 14 of 14.

**Guardrails (Step 6).** Input rules block known injection phrasing after normalising Unicode,
zero-width characters and letter-spacing; they are checked against every real eval question so they
don't block legitimate ones (including tricky ones like "Can I override the approval limit?").
The output guard redacts personal data that did not come from the retrieved text or a tool result
and requires valid `\[n]` citations.

**Cache and router (Step 7).** The cache key is the access group plus the corpus and prompt
version. The semantic tier also requires that every *differing word* be filler, because embeddings
treat "casual" and "sick" leave as near-identical but the answers differ. Router: 20 of 41 answer-check
questions went to the small model, 21 to the large one, none needed escalation, and the answer check
still scored 41 of 41. **TODO: paste the `python -m eval.run\_cache` summary here** (false hits, how many
paraphrases were served from cache, and how many false hits embeddings alone would have caused).

**Prompts and tracing (Step 8).** Prompts are versioned files with a manifest; a test fails if any
version drops a safety rule. Traces hold ids, counts, timings and tool names, not text, and redact
personal data; the sink enforces this so careless instrumentation can't leak. **TODO: paste the
prompt v1 vs v2 comparison** (answer pass rate and "needed a citation repair" count for each).

**Answer quality over time.** Stage by stage, with the `--answers` check on the same 41 key-fact
questions: 35/35 answered (6 errored on API quota) → **27/41** after the output guard first went in →
39/41 after fixes → **41/41** with the router.

## 5\. What went wrong (and what it taught)

1. **The first LLM provider was unreliable.** Daily free-tier quota and repeated 503/504 errors made
evaluation runs flaky. Switching provider fixed it, but the real lesson was to keep model calls behind
one small function with retries and fallbacks, and to design evals that don't need the model where possible.
2. **A library major version broke the tool server.** `mcp` 2.x renamed the server class, and the failure
surfaced only as "Connection closed" in the client. Running the server on its own showed the real error.
Lesson: pin versions, and run each process by itself when debugging.
3. **The output guard silently discarded correct answers.** After it was added, 14 of 41 answers became
"couldn't verify". The model used a citation style the guard didn't recognise, and because the
original answer was thrown away there was nothing to inspect. Fixes: recognise more formats, keep
the raw answer, allow one repair attempt, and tell the model more firmly to cite. Lesson: a guard
that fails closed still needs to be *observable*.
4. **Small string issues defeated exact-match checks and a regex.** The model wrote "three days" where the
eval wanted "3", and used narrow no-break spaces that a phone-number pattern would have missed.
Output is now normalised before checking, and the eval accepts spelled-out numbers.
5. **Overlapping patterns.** A 16-digit card number was only partly redacted because the shorter
Aadhaar pattern matched its first 12 digits. Found by a test, fixed with stricter boundaries.
6. **Hybrid search, a standard recommendation, hurt.** Measuring it was the only way to know.

## 6\. Design decisions worth defending

* **Enforce access where data is read, not in the prompt.** Telling a model "don't reveal salaries"
is a request; filtering the query means it cannot.
* **One query path.** Removing the rejected hybrid code removed a place a filter could be forgotten.
* **Identity from the session only.** A test sends `{"user\_id": "E003"}` in a chat body and checks it is ignored.
* **Cache conservatively.** Never cache tool-using answers, personal questions, refusals or anything a
guard touched; the cost of a wrong cached answer is higher than the cost of a model call.
* **Make guards measurable.** False positives are tested against all real questions; false negatives
against an attack list.

## 7\. Limitations

* Each question is answered independently: **no conversation memory**, so follow-ups don't work.
* Small synthetic corpus; real documents would need better chunking and more careful evaluation.
* The answer check tests for the key fact, not overall answer quality, and the eval sets were written
by the same person who built the system, which biases results upward.
* The sign-in is a mock; rate limits and sessions are in memory; traces are local files.
* Injection defence is layered but incomplete; the dependable layer is access control.
* Several end-to-end paths (the live model loop, MCP over Windows, the web page) were checked by hand and by
tests with fakes, not by automated browser tests.

## 8\. Needed improvements

1. Conversation memory (rewriting follow-up questions into standalone ones before retrieval).
2. Grow the corpus and question set, and have someone else write held-out questions.
3. Replace the mock sign-in with real SSO and move rate limits, sessions and traces out of process.
4. Build evals directly on traces so production behaviour feeds back into the test set.
5. Judge answer quality with a rubric (and a second model) rather than a single key-fact check.

