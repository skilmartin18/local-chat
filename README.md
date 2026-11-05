# Local Chat

Local Chat is a small, single-process public chat server. It provides public
channels, multiline messages, browser-local nicknames, realtime updates, and
text/image attachments. It has no accounts, private channels, external
database, or browser-side dependencies.

![example screenshot](screenshot.png)
## Running

Create the environment and install the server dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Start the server using the deployment settings:

```bash
.venv/bin/python run.py
```

Open the configured host and port in a browser. The default is
<http://127.0.0.1:8000>.

### Data and configuration

The server loads [config.json](./config.json) once at startup. It is a
deployment file and cannot be changed from the chat application. It controls:

- `host` and `port`: the Uvicorn bind address.
- `page_title`: the browser title and displayed site title.
- `warning_text`: the warning shown beside the host and port.
- `max_history`: retained messages per channel.
- `max_channels`: maximum number of channels.
- `max_messages_per_client`: messages allowed per client IP in the rate window.
- `rate_window_seconds`: rolling rate-limit duration.
- `max_attachment_size`: maximum attachment size in bytes.

Messages and channel metadata are stored in the formatted
`chat-data.json` file. Attachments are stored as generated-ID files in
`chat-data-attachments/`. Original attachment bytes are preserved. Deleting a
channel deletes its messages and referenced attachment files. Back up the data
file and attachment directory while the server is stopped.

### Features

- Public channel creation, switching, and two-second hold-to-delete.
- Multiline messages: Enter sends; Ctrl+Enter or Cmd+Enter inserts a newline.
- Browser-local nickname storage.
- SSE-based realtime message delivery and reconnect status.
- Text and small image attachments with previews and downloads.
- Inline error/success notifications and message copy buttons.
- Date-and-time message timestamps.
- No external CDN or frontend package dependency.

This MVP is unauthenticated: everyone who can reach the server can see public
channels, messages, and attachments. It is designed for a single process;
multi-worker or distributed deployment is not supported.

## Developing

See the [development guide](docs/software.md).
