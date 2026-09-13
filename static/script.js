const $ = (id) => document.getElementById(id);

let current = null;
let source = null;
let selectedFile = null;
let notificationTimer = null;
const deleteHoldDuration = 2000;
let deleteHoldTimer = null;
let deleteHoldItem = null;
let suppressChannelClick = false;

const textTypes = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/html",
  "text/css",
  "application/json",
  "application/xml",
  "text/xml",
  "application/yaml",
  "text/yaml",
]);
const imageTypes = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/svg+xml",
]);
const approvedExtensions =
  /\.(txt|md|csv|json|log|xml|yaml|yml|html|css|js|png|jpe?g|gif|webp|bmp|svg)$/i;

const nick = localStorage.getItem("chat-nickname") || "guest";
$("nickname").value = nick;
$("nickname").onchange = () =>
  localStorage.setItem("chat-nickname", $("nickname").value.trim() || "guest");

async function api(url, options) {
  // Send an HTTP request and normalize FastAPI errors for the UI.
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail;
    try {
      ({ detail } = await response.json());
    } catch {
      detail = null;
    }
    const message = Array.isArray(detail)
      ? detail
          .map((error) =>
            typeof error === "string"
              ? error
              : error.msg || error.message || JSON.stringify(error),
          )
          .filter(Boolean)
          .join("; ")
      : typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : "";
    throw Error(message || response.statusText);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function showNotification(message, kind = "error") {
  // Display a temporary terminal-style status message.
  const notification = $("notification");
  notification.textContent = `[${kind}] ${message}`;
  notification.className = `notification ${kind}`;
  notification.hidden = false;
  clearTimeout(notificationTimer);
  notificationTimer = setTimeout(() => {
    notification.hidden = true;
  }, 5000);
}

function formatTimestamp(value) {
  // Use a date and time so old messages are not ambiguous.
  const date = new Date(value);
  return date.toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

async function copyText(text) {
  // Copy text through the modern API, with a legacy browser fallback.
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const fallback = document.createElement("textarea");
  fallback.value = text;
  fallback.setAttribute("readonly", "");
  fallback.style.position = "fixed";
  fallback.style.opacity = "0";
  document.body.append(fallback);
  fallback.select();
  if (!document.execCommand("copy")) {
    throw Error("copy unavailable");
  }
  fallback.remove();
}

function render(message) {
  // Convert one server message into safe DOM nodes and append it to the log.
  $("channel-empty").hidden = true;
  const paragraph = document.createElement("p");
  paragraph.className = "message";

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `[${formatTimestamp(message.created_at)}] ${message.nickname}: `;
  const copyButton = document.createElement("button");
  copyButton.className = "copy-message";
  copyButton.type = "button";
  copyButton.textContent = "copy";
  copyButton.onclick = async () => {
    try {
      await copyText(message.content || "");
      showNotification("message copied", "ok");
    } catch {
      showNotification("could not copy message");
    }
  };
  meta.append(copyButton);

  paragraph.append(meta, document.createTextNode(message.content || ""));
  const attachments = Array.isArray(message.attachments)
    ? message.attachments
    : message.attachment
      ? [message.attachment]
      : [];
  if (attachments.length) {
    const attachmentList = document.createElement("div");
    attachmentList.className = "message-attachments";
    attachments.forEach((attachment) =>
      renderAttachment(attachment, attachmentList),
    );
    paragraph.append(attachmentList);
  }
  $("messages").append(paragraph);
  $("messages").scrollTop = $("messages").scrollHeight;
}

function safeEndpoint(attachment) {
  // Accept only absolute HTTP(S) attachment URLs from server metadata.
  const endpoint =
    attachment.download_url ||
    attachment.preview_url ||
    attachment.url ||
    attachment.endpoint;
  if (!endpoint) {
    return null;
  }
  try {
    const url = new URL(endpoint, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function renderAttachment(attachment, container) {
  // Render a download link and an optional safe image or text preview.
  const endpoint = safeEndpoint(attachment);
  if (!endpoint) {
    return;
  }
  const filename = attachment.filename || "attachment";
  const link = document.createElement("a");
  link.href = endpoint;
  link.textContent = `${filename} (${attachment.size || 0} bytes)`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  container.append(link);

  const mediaType = (attachment.media_type || "").toLowerCase();
  if (imageTypes.has(mediaType)) {
    const image = document.createElement("img");
    image.className = "attachment-preview";
    image.src = endpoint;
    image.alt = filename;
    image.loading = "lazy";
    container.append(image);
  } else if (
    textTypes.has(mediaType) ||
    /\.(txt|md|csv|json|log|xml|yaml|yml|html|css|js)$/i.test(filename)
  ) {
    const preview = document.createElement("pre");
    preview.className = "attachment-text-preview";
    preview.textContent = "Loading preview...";
    container.append(preview);
    fetch(endpoint)
      .then((response) => {
        if (!response.ok) {
          throw Error("preview unavailable");
        }
        return response.text();
      })
      .then((text) => {
        preview.textContent = text;
      })
      .catch(() => {
        preview.textContent = "Preview unavailable.";
      });
  }
}

function setSelectedFile(file) {
  // Track the pending upload and update its visible filename/size label.
  selectedFile = file;
  const name = $("attachment-name");
  name.textContent = file ? `${file.name} (${file.size} bytes)` : "";
  name.hidden = !file;
}

async function selectChannel(channel) {
  // Load a channel's history and open its realtime SSE subscription.
  current = channel;
  $("chat-panel").hidden = false;
  $("empty-state").hidden = true;
  $("title").textContent = "# " + channel.name;
  $("messages").replaceChildren();
  $("content").disabled = $("send").disabled = false;
  $("attachment").disabled = false;

  document
    .querySelectorAll(".channels .channel-select")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.id === channel.id),
    );

  if (source) {
    source.close();
  }

  try {
    const messages = (await api(`/api/channels/${channel.id}/messages`))
      .messages;
    messages.forEach(render);
    $("channel-empty").hidden = messages.length > 0;
    source = new EventSource(`/api/channels/${channel.id}/events`);
    source.onopen = () => {
      $("status").textContent = "connected";
    };
    source.onerror = () => {
      $("status").textContent = "reconnecting...";
    };
    source.addEventListener("message", (event) =>
      render(JSON.parse(event.data)),
    );
  } catch (error) {
    showNotification(error.message);
  }
}

function cancelDeleteHold() {
  // Cancel an unfinished destructive hold and restore the channel styling.
  if (deleteHoldTimer) {
    clearTimeout(deleteHoldTimer);
    deleteHoldTimer = null;
  }
  if (deleteHoldItem) {
    deleteHoldItem.classList.remove("hold-delete");
    deleteHoldItem = null;
  }
}

function startDeleteHold(event, channel, listItem) {
  // Start the two-second hold gesture used to request channel deletion.
  if (event.pointerType === "mouse" && event.button !== 0) {
    return;
  }
  cancelDeleteHold();
  deleteHoldItem = listItem;
  listItem.classList.add("hold-delete");
  event.currentTarget.setPointerCapture?.(event.pointerId);
  deleteHoldTimer = setTimeout(async () => {
    deleteHoldTimer = null;
    deleteHoldItem = null;
    listItem.classList.remove("hold-delete");
    suppressChannelClick = true;
    await deleteChannel(channel);
    window.setTimeout(() => {
      suppressChannelClick = false;
    }, 0);
  }, deleteHoldDuration);
}

async function deleteChannel(channel) {
  // Confirm and delete a channel, then reset or refresh the chat view.
  const confirmed = window.confirm(
    `Warning: delete #${channel.name} and all of its messages? This cannot be undone.`,
  );
  if (!confirmed) {
    return;
  }

  try {
    await api(`/api/channels/${channel.id}`, { method: "DELETE" });
    if (current?.id === channel.id) {
      current = null;
      if (source) {
        source.close();
        source = null;
      }
      $("chat-panel").hidden = true;
      $("messages").replaceChildren();
      $("content").disabled = $("send").disabled = true;
      $("attachment").disabled = true;
      setSelectedFile(null);
      $("status").textContent = "disconnected";
    }
    await loadChannels();
  } catch (error) {
    showNotification(error.message);
  }
}

async function loadChannels(preferredChannelId = null) {
  // Rebuild the channel list and optionally select a newly created channel.
  const channels = (await api("/api/channels")).channels;
  $("empty-state").hidden = channels.length > 0;
  $("channels").replaceChildren(
    ...channels.map((channel) => {
      const listItem = document.createElement("li");
      listItem.className = "channel-item";

      const selectButton = document.createElement("button");
      selectButton.className = "channel-select";
      const displayName =
        channel.name.length > 16
          ? `${channel.name.slice(0, 16)}…`
          : channel.name;
      selectButton.textContent = "# " + displayName;
      selectButton.title = `Delete #${channel.name}`;
      selectButton.dataset.id = channel.id;
      selectButton.setAttribute(
        "aria-label",
        `Select #${channel.name}; hold for two seconds to delete`,
      );
      selectButton.onclick = () => {
        if (suppressChannelClick) {
          suppressChannelClick = false;
          return;
        }
        selectChannel(channel);
      };
      selectButton.onpointerdown = (event) =>
        startDeleteHold(event, channel, listItem);
      selectButton.onpointerup = cancelDeleteHold;
      selectButton.onpointercancel = cancelDeleteHold;
      selectButton.onpointerleave = (event) => {
        if (!event.currentTarget.hasPointerCapture?.(event.pointerId)) {
          cancelDeleteHold();
        }
      };

      listItem.append(selectButton);
      return listItem;
    }),
  );

  if (current && !channels.some((channel) => channel.id === current.id)) {
    current = null;
    if (source) {
      source.close();
      source = null;
    }
    $("chat-panel").hidden = true;
    $("messages").replaceChildren();
    $("content").disabled = $("send").disabled = true;
    $("attachment").disabled = true;
    setSelectedFile(null);
  }

  const preferred = channels.find(
    (channel) => channel.id === preferredChannelId,
  );
  if (preferred) {
    await selectChannel(preferred);
  } else if (!current && channels[0]) {
    await selectChannel(channels[0]);
  }
}

$("new-channel").onsubmit = async (event) => {
  // Create a channel, select it, focus the composer, and report success.
  event.preventDefault();

  try {
    const channel = await api("/api/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("channel-name").value }),
    });
    $("channel-name").value = "";
    await loadChannels(channel.id);
    $("content").focus();
    showNotification(`channel #${channel.name} created`, "ok");
  } catch (error) {
    showNotification(error.message);
  }
};

$("compose").onsubmit = async (event) => {
  // Send text and an optional attachment as multipart form data.
  event.preventDefault();
  const input = $("content");

  if (!current || (!input.value.trim() && !selectedFile)) {
    return;
  }

  try {
    const body = new FormData();
    body.append("nickname", $("nickname").value.trim() || "guest");
    body.append("content", input.value);
    if (selectedFile) {
      body.append("attachment", selectedFile, selectedFile.name);
    }
    await api(`/api/channels/${current.id}/messages`, {
      method: "POST",
      body,
    });
    input.value = "";
    $("attachment").value = "";
    setSelectedFile(null);
  } catch (error) {
    showNotification(error.message);
  }
};

$("attachment").onchange = () => {
  // Validate the chosen filename before allowing it to be uploaded.
  const [file] = $("attachment").files;
  if (!file) {
    setSelectedFile(null);
    return;
  }
  if (!approvedExtensions.test(file.name)) {
    $("attachment").value = "";
    setSelectedFile(null);
    showNotification("choose an approved text or image file");
    return;
  }
  setSelectedFile(file);
};

$("content").onkeydown = (event) => {
  // Keep Enter as send and Ctrl/Cmd+Enter as an inserted newline.
  if (event.key !== "Enter") {
    return;
  }

  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    const input = $("content");
    const start = input.selectionStart;
    const end = input.selectionEnd;
    input.setRangeText("\n", start, end, "end");
    return;
  }

  event.preventDefault();
  $("compose").requestSubmit();
};

async function loadConfig() {
  // Load deployment-controlled title and warning text from the server.
  const config = await api("/api/config");
  document.title = config.page_title;
  $("server-warning").textContent =
    `${window.location.host} — ${config.warning_text}`;
  document.querySelector("h1").textContent = `> ${config.page_title}`;
}

loadConfig()
  .then(loadChannels)
  .catch((error) => {
    showNotification(error.message);
  });
