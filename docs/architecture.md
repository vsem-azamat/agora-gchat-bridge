# Architecture

[Docs](README.md) / **Architecture**

The bridge is one process per bridged space. It reaches Google Chat through the Chat REST API with your user credentials (`chat_api.ChatApi`), and the People API for names.

The Agora hub starts the bridge, writes `hello` and `out` lines to its standard input and reads `hello`, `in`, `sent` and `failed` lines from its standard output; its standard error goes to the hub's log.

| Module | Holds |
| --- | --- |
| `cli.py` | Entry point: reads stdin, writes stdout, the reconnect loop with backoff, signals |
| `config.py` | Command line and the config file |
| `auth.py` | The token file and access tokens (refreshed from the refresh token) |
| `account.py` | `gchat-bridge login` and `gchat-bridge spaces` |
| `chat_api.py` | The HTTP client: retries, errors, the calls the bridge makes |
| `errors.py` | Errors, sorted by what the bridge does about them |
| `bridge.py` | The bridge itself: cursor, reading, threads, send jobs |
| `render.py` | A Chat message to the text and fields of an `in` line |
| `text.py` | UTF-16 lengths, splitting, the agent's name in front of a message |

## Start

The bridge waits for `hello` and answers it with `{"type":"hello","version":3,"caps":{"personas":"prefix"}}` (`version` is the protocol version the bridge speaks, `PROTOCOL_VERSION`). It then reads the space (`spaces.get`). In a space with threads (`spaceThreadingState` `THREADED_MESSAGES` or `GROUPED_MESSAGES`) it writes a second `hello` with `"threads":"thread"`; the hub keeps the latest caps and holds replies in threads for a few seconds after `hello` until they are declared. A space the account may not read (403 or 404) is a configuration error; other errors on this first read count as a connection failure, and the sixth in a row is a configuration error (`STARTUP_ATTEMPTS`).

The cursor is the creation time of the last message reported and its id: `2026-10-10T18:00:01.000000Z|AbCdEf`.

- With an empty cursor it starts after the newest message (no history import) and reports that position at once as a cursor-only `in` line, so a restart before the first message keeps it. An empty space starts at the current time.
- With a cursor it reads every message created after it.

## Reading

Chat sends no events to user credentials, so the bridge reads the space every `--poll` seconds (default 5): `spaces.messages.list` filtered on `createTime > …`, oldest first, in pages of 100. Each read starts 10 s before the cursor (`POLL_LOOKBACK`), and one every `--reconcile` seconds (default 60) starts 2 minutes before it (`RECONCILE_LOOKBACK`), so a message that becomes visible after a newer one is still reported. Message ids reported in this process are remembered (the last 5000) and skipped; the hub skips repeats by `ext_id` anyway. A message older than the cursor is reported with the current cursor, so the cursor never moves back. The first read of a process skips what lies before the hub's cursor, which an earlier process reported. A message is marked as reported, and the cursor moved past it, only once its line is built: when building it fails for want of an answer (a name, a thread's root), the read ends there and the next one starts from that message again. A thread root Google refuses to read leaves the reply as a message of the room rather than holding up the messages after it.

Each message becomes an `in` line:

- `id`: the message's id in the space (the last part of `spaces/S/messages/ID`), and `cursor`;
- `author`: `id` (the numeric user id), `name` (the People API's display name, cached for 24 h; `user <id>` when the directory does not know them), `self` for the account's own messages written in Chat. An app's message carries its display name, else `app`;
- `text`: the text, with `<users/ID>` markup as `@Name` and `<url|text>` links as `text (url)`; attachments, GIFs and cards as placeholders in front (`[image a.png]`, `[file doc.pdf]`, `[Drive file …]`, `[GIF]`, `[card] <fallback text>`);
- `at`: the creation time;
- `reply_to`: the quoted message, when the message quotes one of this space;
- `addressed`: in a direct message every message but the account's own; in a space a mention of the account or `@all`, or a reply in a thread the account started (see Threads).

An own message that starts with the prefix is not the operator's: it comes in from the author `assistant (<prefix>)` with `self: false` and is not addressed. The bridge's own messages (see Sending) only move the cursor.

## Threads

In a threaded space every top-level message starts a thread (`thread.name`), and replies carry `threadReply`. A thread is keyed in Agora by its root message's id: a reply's `in` line carries `thread: {"key": "<root id>", "title": …}` and `reply_to` the root, so the hub joins it to the root's thread. A reply that quotes another message of the thread replies to that one instead, when the hub has it (it was reported or sent by this process, or lies before the hub's cursor): the hub joins a new key to the thread of the message the line replies to, so a quoted message it does not have would start a new thread. The root's own line has no `thread`. The title is the first line of the root's text, cut to 80 characters. Roots are remembered as they are read or sent (the last 2000); a reply to an older root reads the thread's first message (`thread.name` filter, one message).

An `out` line goes into a thread when it names one: its `thread` key, else its `reply_to`. The bridge finds that message's `thread.name` (remembered, or `spaces.messages.get`) and sends every part with it and `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`. A message that names no thread starts one with its first part, and every further part goes into that thread, also when the job is resumed after a reconnect. A reply to a message inside a thread goes into that thread. A thread started in Agora reaches the bridge root first (the hub holds replies until the root has an `ext_id`), so it becomes a thread on the root's message. In a space without threads, replies go out as plain messages.

## Sending

Each `out` line becomes a send job: one message per part, as plain text starting with the prefix. When the line's author is an agent (any `kind` but `human`), every part names it after the prefix: `🤖 secretary: …`, from the author's `display` (else `name`), on one line and cut to 32 UTF-16 units (`MAX_PERSONA_UTF16`). The operator's messages carry the prefix alone. Text over 3998 UTF-16 units (`MAX_MESSAGE_UTF16`: Chat's 4096 characters less the longest prefix, name and `: `) is split at line breaks or spaces into several messages, each with the prefix and the name.

Every part carries a client-assigned message id, `client-ag-<h>-<out id>-<part>`, where `<h>` is 8 hex digits of a hash of the bridge's name and the space. The bridge answers `sent` with the first part's message id before any read can see it: reads and sends share one lock. An `out` id handed twice in one process is sent once. A refused send answers `failed` with Google's reason; when a later part fails, the reason says `sent in part: N of M`.

## Delivery guarantees

Chat refuses a second message with the same client-assigned id in a space (409 `ALREADY_EXISTS`), so a part sent again is never a duplicate: the bridge reads the existing message and counts the part as sent.

- **Lost answers.** A send that gets no answer (a timeout, a broken connection) leaves the job in place; the bridge reconnects and sends the rest of the job again, and a part that had gone out is found by its id.
- **Earlier processes.** A message the hub hands again after a restart (the earlier process died before answering) gets the same ids and is claimed the same way. No time window applies.
- **Stopping.** On `SIGTERM` a send in flight is finished (for up to 4 s; the hub kills the bridge 5 s after `SIGTERM`) before the process exits.

Limit: the ids rest on the hub's message ids. If the hub's database starts over, an id can name a message of the earlier hub; the bridge compares the text of the message it finds with the part, and when they differ sends under the id with 8 hex digits of a hash of the part's text appended, which is again the same on every attempt. A part whose text matches an earlier hub's message with the same id is not sent.

## Failures

HTTP 429 and 5xx answers are retried three times with exponential backoff (honouring `Retry-After`, at most 30 s each). A 401 refreshes the access token once. Connection errors and errors left after the retries reconnect with backoff from 1 s to 60 s, reset after a minute of steady work. These exit with status 2, so the Agora hub's own restart backoff applies:

- no token file, or one without the fields `gchat-bridge login` writes;
- a refresh token Google refused (revoked access, a deleted OAuth client);
- a space the account may not read (403 or 404), at the start or on any later read;
- a space that cannot be read in 6 attempts in a row at the start (`STARTUP_ATTEMPTS`).

Logs go to standard error and carry ids and counts, never message text or tokens.
