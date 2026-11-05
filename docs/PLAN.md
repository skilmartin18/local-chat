# Minimal terminal chat app plan

## Problem and approach

Create a small, dependency-light chat application from the current placeholder workspace. The browser will use only local HTML, CSS, and JavaScript served by a FastAPI application. FastAPI will expose JSON/HTTP endpoints for channels and messages, plus Server-Sent Events (SSE) for near-real-time updates. Messages and channel metadata will be persisted in a local JSON file, avoiding an external database. Nicknames will be temporary and stored in `localStorage`; accounts are explicitly deferred.

The UI will use a terminal-inspired monochrome layout with keyboard-first interactions, while still providing basic responsive behavior and accessible labels. All message text will be rendered as text rather than HTML, and the API will enforce length, channel-name, and rate limits suitable for a minimal public MVP.

## Scope

### In scope

- List existing public channels and create new channels.
- Join/select a channel and read its complete retained message history.
- Send messages visible to every user in that channel.
- Receive new messages through SSE, with reconnect behavior.
- Temporary nickname stored in the browser, with an editable nickname command/control.
- Local JSON persistence with atomic writes and startup validation.
- Terminal-like visual design with no external assets, CDNs, or browser dependencies.
- Basic input validation, output escaping, message IDs, timestamps, and health/error responses.

### Out of scope for MVP

- User registration, passwords, sessions, authorization, or private channels.
- Direct messages, file uploads, reactions, editing, deletion, search, or moderation UI.
- Multi-process/distributed deployment, external databases, and horizontal scaling.
- Rich text/Markdown, third-party UI frameworks, or client-side package managers.

## Suggested product safeguards

- Add a bounded message history per channel and document the retention behavior.
- Enforce maximum lengths for nicknames, channel names, and messages.
- Add simple per-client send throttling to protect a public instance from spam.
- Generate server-side IDs and UTC timestamps so clients cannot spoof message metadata.
- Treat all user content as untrusted text and avoid `innerHTML`.
- Include SSE reconnect support and a clear disconnected/reconnecting indicator.
- Provide a small health endpoint and structured server logs for operational visibility.
- Use atomic file replacement when saving JSON so an interrupted write does not corrupt the store.

## Ticket set

1. **Scaffold the FastAPI application and local development entrypoint**
   - Create the Python package, dependency manifest, app factory/entrypoint, static-file serving, and documented local run command.
   - Add a health endpoint and consistent JSON error handling.

2. **Define the local JSON data model and safe persistence layer**
   - Store channels, message histories, IDs, and UTC timestamps in a versioned JSON document.
   - Implement startup creation/validation, atomic replacement writes, bounded history, and clear persistence errors.

3. **Implement public channel APIs**
   - Add endpoints to list channels and create a channel with normalized, validated names.
   - Return stable channel identifiers and metadata needed by the client.
   - Prevent duplicate names and reject invalid/oversized input.

4. **Implement message history and send-message APIs**
   - Add channel history retrieval and message creation endpoints.
   - Validate nickname and message content, assign server-side IDs/timestamps, apply per-client throttling, persist, and publish events.

5. **Implement SSE channel subscriptions**
   - Add an SSE endpoint per channel with keep-alive behavior, subscriber cleanup, reconnect-friendly event payloads, and explicit errors for missing channels.
   - Ensure a message is delivered to all active subscribers for the selected channel.

6. **Build the terminal-style browser client**
   - Replace the placeholder page with semantic HTML, inline CSS, and vanilla JavaScript only.
   - Add channel navigation/creation, nickname input, message history rendering, send control, connection status, responsive layout, and keyboard-first behavior.
   - Render untrusted content as text and persist only the temporary nickname in `localStorage`.

7. **Add automated coverage for core behavior**
   - Test persistence round trips and recovery/error paths.
   - Test channel validation/duplicates, message validation/rate limiting, SSE publication, and health responses.
   - Add a lightweight browser-facing smoke check if the repository’s available test setup supports it.

8. **Document MVP operation and future account extension**
   - Document setup, local data-file location/backup expectations, API behavior, retention/rate limits, and the no-auth trust model.
   - Record a follow-up design for replacing browser-local nicknames with accounts without changing public-channel semantics.

## Notes and decisions

- FastAPI is selected over Flask because SSE and typed request/response validation are straightforward while keeping the dependency set small.
- SSE is selected over WebSockets because the MVP primarily needs server-to-client updates and normal HTTP remains sufficient for sending messages.
- JSON-file persistence is appropriate for a single-process MVP, but the documentation should state that concurrent multi-worker deployment is not supported.
- The existing `test.html` is a placeholder and should be replaced or retired as part of the client ticket rather than preserved as a second entrypoint.
