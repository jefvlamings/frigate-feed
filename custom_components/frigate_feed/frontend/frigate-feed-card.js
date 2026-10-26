const SUPPORTED_CARD_LABELS = ["person", "pet", "vehicle"];
const LEGACY_CARD_LABEL_ALIASES = { animal: "pet" };

function normalizeCardLabel(label) {
  const lowered = String(label || "").toLowerCase().trim();
  return LEGACY_CARD_LABEL_ALIASES[lowered] || lowered;
}

const LIVE_REFRESH_DEBOUNCE_MS = 500;
const MEDIA_ERROR_REFRESH_MIN_INTERVAL_MS = 60 * 1000;

function slugifyCardValue(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function itemLabels(item) {
  const labels = Array.isArray(item?.labels) && item.labels.length ? item.labels : [item?.label];
  return [...new Set(labels.map((label) => normalizeCardLabel(label)).filter(Boolean))];
}

function formatClipOffset(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  return `${m}:${s}`;
}

// object-position that brings the centre of `box` ([x, y, w, h] fractions of the image)
// as close to the centre of an object-fit: cover crop as the image edges allow.
function focusObjectPosition(box, naturalWidth, naturalHeight, frameWidth, frameHeight) {
  if (!Array.isArray(box) || box.length !== 4) return "";
  if (!(naturalWidth > 0 && naturalHeight > 0 && frameWidth > 0 && frameHeight > 0)) return "";
  const scale = Math.max(frameWidth / naturalWidth, frameHeight / naturalHeight);
  const axis = (start, size, natural, frame) => {
    const scaled = natural * scale;
    if (scaled - frame < 1) return 50;
    const centre = (Number(start) + Number(size) / 2) * scaled;
    return Math.min(100, Math.max(0, ((centre - frame / 2) / (scaled - frame)) * 100));
  };
  const x = axis(box[0], box[2], naturalWidth, frameWidth);
  const y = axis(box[1], box[3], naturalHeight, frameHeight);
  return `${x.toFixed(1)}% ${y.toFixed(1)}%`;
}

function stripUrlQuery(url) {
  return typeof url === "string" ? url.split("?", 1)[0] : "";
}

const CARD_I18N = {
  en: {
    person: "Person",
    pet: "Pet",
    vehicle: "Vehicle",
    motion: "Motion",
    visitor: "Visitor",
    detection_info: "Detection info",
    no_snapshot: "No snapshot",
    camera: "Camera",
    timestamp: "Timestamp",
    duration: "Duration",
    detection: "Detection",
    detections: "Detections",
    seek_to_detection: "Jump to this detection",
    history: "History",
    logbook: "Logbook",
    recording: "Recording",
    photo: "Photo",
    go_to_folder: "Go to folder",
    reset: "Refresh",
    delete: "Delete",
    no_detections: "No detections in range.",
    previous: "Previous",
    next: "Next",
    page: "Page",
    open_recording_preview: "Open recording preview",
    show_detection_info: "Show detection info",
    show_timeline: "Show timeline",
    recording_preview: "Recording preview",
    close: "Close",
    close_info_dialog: "Close info dialog",
    snapshot: "Snapshot",
    page_size: "Items per page",
    thumbnails: "Thumbnails",
    thumbnail_object: "Object (cropped)",
    thumbnail_frame: "Full frame",
    not_found: "Not found",
    download_failed: "Download failed",
    pending: "Pending",
    event: "Event",
    links: "Links",
    download: "Download",
    image: "Image",
    video: "Video",
  },
  nl: {
    person: "Persoon",
    pet: "Huisdier",
    vehicle: "Voertuig",
    motion: "Beweging",
    visitor: "Bezoeker",
    detection_info: "Detectie-info",
    no_snapshot: "Geen snapshot",
    camera: "Camera",
    timestamp: "Tijdstip",
    duration: "Duur",
    detection: "Detectie",
    detections: "Detecties",
    seek_to_detection: "Spring naar deze detectie",
    history: "Geschiedenis",
    logbook: "Logboek",
    recording: "Opname",
    photo: "Foto",
    go_to_folder: "Ga naar map",
    reset: "Vernieuwen",
    delete: "Verwijderen",
    no_detections: "Geen detecties in bereik.",
    previous: "Vorige",
    next: "Volgende",
    page: "Pagina",
    open_recording_preview: "Open opnamevoorbeeld",
    show_detection_info: "Toon detectie-info",
    show_timeline: "Toon tijdlijn",
    recording_preview: "Opnamevoorbeeld",
    close: "Sluiten",
    close_info_dialog: "Sluit detectiedialoog",
    snapshot: "Snapshot",
    page_size: "Items per pagina",
    thumbnails: "Miniaturen",
    thumbnail_object: "Object (bijgesneden)",
    thumbnail_frame: "Volledig beeld",
    not_found: "Niet gevonden",
    download_failed: "Download mislukt",
    pending: "In behandeling",
    event: "Event",
    links: "Links",
    download: "Download",
    image: "Afbeelding",
    video: "Video",
  },
};

class FrigateFeedCard extends HTMLElement {
  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._items = [];
    this._filteredItems = [];
    this._error = null;
    this._loading = false;
    this._resolvingIds = new Set();
    this._page = 1;
    this._availableLabels = [...SUPPORTED_CARD_LABELS];
    this._activeLabels = new Set();
    this._configuredLabels = [];
    this._filtersInitialized = false;
    this._retentionHours = 24;
    this._expandedClusterKey = "";
    this._infoDialog = { open: false, itemId: "" };
    this._videoControlsEnabled = new Set();
    this._ignoreDialogCloseEvents = 0;
    this._hasLoaded = false;
    this._isConnected = false;
    this._wasDisconnected = false;
    this._subscription = null;
    this._liveRefreshTimer = null;
    this._liveRefreshQueued = false;
    this._lastMediaErrorRefreshTs = 0;
    this._lastLoadErrorTs = 0;
    this._handleMediaError = (ev) => {
      const target = ev.target;
      if (!(target instanceof HTMLImageElement) && !(target instanceof HTMLVideoElement)) return;
      if (!this._hass || !this._hasLoaded) return;
      // Signed media URLs expire; reload the list once to get fresh signatures (rate-limited).
      const now = Date.now();
      if (now - this._lastMediaErrorRefreshTs < MEDIA_ERROR_REFRESH_MIN_INTERVAL_MS) return;
      this._lastMediaErrorRefreshTs = now;
      const inDialog = Boolean(target.closest?.("ha-dialog"));
      this._loadItems({ silent: true, refreshDialogMedia: inDialog });
    };
    this._handleDialogKeyDown = (ev) => {
      if (!this._infoDialog.open) return;
      if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        this._openPreviousInfoItem();
      } else if (ev.key === "ArrowRight") {
        ev.preventDefault();
        this._openNextInfoItem();
      }
    };
    this._handleMediaLoad = (ev) => {
      if (ev.target?.matches?.("img[data-focus-box]")) this._focusThumb(ev.target);
    };
    this._thumbResizeObserver =
      typeof ResizeObserver === "function" ? new ResizeObserver(() => this._focusThumbs()) : null;
    this.attachShadow({ mode: "open" });
    // Media error/load events do not bubble; listen in the capture phase for all <img>/<video>.
    this.shadowRoot.addEventListener("error", this._handleMediaError, true);
    this.shadowRoot.addEventListener("load", this._handleMediaLoad, true);
  }

  connectedCallback() {
    this._isConnected = true;
    this._thumbResizeObserver?.observe(this);
    if (this._infoDialog.open) window.addEventListener("keydown", this._handleDialogKeyDown);
    this._ensureLiveSubscription();
    if (this._wasDisconnected) {
      this._wasDisconnected = false;
      // Events may have been missed while detached (e.g. switching dashboard views).
      if (this._hasLoaded) this._scheduleLiveRefresh();
    }
  }

  disconnectedCallback() {
    this._isConnected = false;
    this._thumbResizeObserver?.disconnect();
    this._wasDisconnected = true;
    window.removeEventListener("keydown", this._handleDialogKeyDown);
    if (this._liveRefreshTimer) {
      clearTimeout(this._liveRefreshTimer);
      this._liveRefreshTimer = null;
    }
    this._teardownLiveSubscription();
  }

  _ensureLiveSubscription() {
    const connection = this._hass?.connection;
    if (!this._isConnected || !connection || typeof connection.subscribeMessage !== "function") return;
    if (this._subscription?.connection === connection) return;
    this._teardownLiveSubscription();
    const subscription = { connection, unsubscribe: null, cancelled: false, onReady: null };
    this._subscription = subscription;
    // The websocket library resubscribes after a reconnect; reload to catch missed events.
    subscription.onReady = () => this._scheduleLiveRefresh();
    connection.addEventListener?.("ready", subscription.onReady);
    const onMessage = () => this._scheduleLiveRefresh();
    let pending;
    try {
      pending = Promise.resolve(
        connection.subscribeMessage(onMessage, { type: "frigate_feed/subscribe" }),
      );
    } catch (err) {
      pending = Promise.reject(err);
    }
    pending
      .then((unsubscribe) => {
        if (subscription.cancelled) {
          this._callUnsubscribe(unsubscribe);
          return;
        }
        subscription.unsubscribe = unsubscribe;
      })
      .catch(() => {
        // Live refresh unavailable (older backend or no permission); manual refresh still works.
      });
  }

  _teardownLiveSubscription() {
    const subscription = this._subscription;
    if (!subscription) return;
    this._subscription = null;
    subscription.cancelled = true;
    if (subscription.onReady) {
      subscription.connection.removeEventListener?.("ready", subscription.onReady);
    }
    this._callUnsubscribe(subscription.unsubscribe);
  }

  _callUnsubscribe(unsubscribe) {
    if (typeof unsubscribe !== "function") return;
    try {
      const result = unsubscribe();
      if (result && typeof result.catch === "function") result.catch(() => {});
    } catch (_err) {
      // Connection may already be closed.
    }
  }

  _scheduleLiveRefresh() {
    if (this._liveRefreshTimer) clearTimeout(this._liveRefreshTimer);
    this._liveRefreshTimer = setTimeout(() => {
      this._liveRefreshTimer = null;
      this._loadItems({ silent: true });
    }, LIVE_REFRESH_DEBOUNCE_MS);
  }

  setConfig(config) {
    this._config = {
      labels: [],
      cameras: [],
      page_size: 20,
      show_timeline: true,
      thumbnail: "object",
      ...config,
    };
    const rawLabels = Array.isArray(this._config.labels)
      ? this._config.labels.map((label) => String(label || "").toLowerCase().trim())
      : [];
    const configuredLabels = [
      ...new Set(
        rawLabels
          .map((label) => normalizeCardLabel(label))
          .filter((label) => SUPPORTED_CARD_LABELS.includes(label)),
      ),
    ];
    this._configuredLabels = configuredLabels;
    this._activeLabels = new Set(configuredLabels);
    this._filtersInitialized = false;
    this._render();
    this._loadItems();
  }

  static async getConfigElement() {
    return document.createElement("frigate-feed-card-editor");
  }

  static getStubConfig() {
    return {
      type: "custom:frigate-feed-card",
    };
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureLiveSubscription();
    // Retry a failed initial load on later hass updates, but not on every state change.
    const retryBlocked = Date.now() - this._lastLoadErrorTs < 30 * 1000;
    if (!this._hasLoaded && !this._loading && !retryBlocked) {
      this._loadItems();
    }
  }

  getCardSize() {
    return 6;
  }

  _languageCode() {
    const raw = String(this._hass?.language || document.documentElement.lang || "en").toLowerCase();
    if (CARD_I18N[raw]) return raw;
    const base = raw.split("-")[0];
    if (CARD_I18N[base]) return base;
    return "en";
  }

  _t(key) {
    const lang = this._languageCode();
    return CARD_I18N[lang]?.[key] || CARD_I18N.en[key] || key;
  }

  _labelText(label) {
    const normalized = normalizeCardLabel(label);
    if (SUPPORTED_CARD_LABELS.includes(normalized)) {
      return this._t(normalized);
    }
    return normalized ? normalized.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) : "-";
  }

  _labelIconName(label) {
    const normalized = normalizeCardLabel(label);
    if (normalized === "pet") return "mdi:dog-side";
    if (normalized === "vehicle") return "mdi:car";
    if (normalized === "motion") return "mdi:motion-sensor";
    if (normalized === "visitor") return "mdi:doorbell";
    return "mdi:account";
  }

  _applyFilters() {
    const cameraFilter = new Set((this._config?.cameras || []).map((v) => String(v).toLowerCase()));
    this._filteredItems = this._items.filter((item) => {
      if (!item.labels.some((label) => this._activeLabels.has(label))) return false;
      if (!cameraFilter.size) return true;
      // Accept both the display name and the Frigate camera id in the `cameras:` option.
      return (
        cameraFilter.has(String(item.camera_name || "").toLowerCase()) ||
        cameraFilter.has(String(item.frigate?.camera || "").toLowerCase())
      );
    });
    this._page = Math.min(this._page, this._totalPages());
    if (this._page < 1) this._page = 1;
  }

  _pageSize() {
    const raw = Number(this._config?.page_size ?? 20);
    if (!Number.isFinite(raw)) return 20;
    return Math.max(1, Math.min(100, Math.floor(raw)));
  }

  _totalPages() {
    return Math.max(1, Math.ceil(this._filteredItems.length / this._pageSize()));
  }

  _pagedItems() {
    const pageSize = this._pageSize();
    const page = Math.max(1, Math.min(this._page, this._totalPages()));
    const start = (page - 1) * pageSize;
    return this._filteredItems.slice(start, start + pageSize);
  }

  _toggleLabelFilter(label) {
    if (this._activeLabels.has(label)) {
      this._activeLabels.delete(label);
    } else {
      this._activeLabels.add(label);
    }
    this._applyFilters();
    this._render();
  }

  async _loadItems(options = {}) {
    if (!this._hass || !this._config) {
      return;
    }
    // Silent loads (live refresh, expired URLs) keep page, filters, expanded cluster and dialog.
    const silent = Boolean(options.silent);
    if (silent) {
      if (this._loading) {
        this._liveRefreshQueued = true;
        this._liveRefreshNeedsDialogMedia =
          Boolean(this._liveRefreshNeedsDialogMedia) || Boolean(options.refreshDialogMedia);
        return;
      }
      return this._loadItemsSilently(Boolean(options.refreshDialogMedia));
    }
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "frigate_feed/list",
      });
      this._applyListResult(result);
    } catch (err) {
      this._error = err?.message || String(err);
      this._lastLoadErrorTs = Date.now();
    } finally {
      this._loading = false;
      this._render();
      this._runQueuedLiveRefresh();
    }
  }

  _applyListResult(result) {
    const enabledLabels = Array.isArray(result?.enabled_labels)
      ? result.enabled_labels
          .map((label) => normalizeCardLabel(label))
          .filter((label) => SUPPORTED_CARD_LABELS.includes(label))
      : [...SUPPORTED_CARD_LABELS];
    this._availableLabels = [...new Set(enabledLabels)];
    if (!this._filtersInitialized) {
      const initial =
        this._configuredLabels.length > 0
          ? this._configuredLabels.filter((label) => this._availableLabels.includes(label))
          : [...this._availableLabels];
      this._activeLabels = new Set(initial);
      this._filtersInitialized = true;
    } else {
      const allowed = new Set(this._availableLabels);
      this._activeLabels = new Set([...this._activeLabels].filter((label) => allowed.has(label)));
    }
    this._items = (result?.items || []).map((item) => ({
      ...item,
      label: normalizeCardLabel(item.label),
      labels: itemLabels(item),
      detections: Array.isArray(item.detections) ? item.detections : [],
    }));
    const retentionHours = Number.parseInt(String(result?.retention_hours ?? ""), 10);
    if (Number.isFinite(retentionHours) && retentionHours > 0) {
      this._retentionHours = retentionHours;
    }
    this._applyFilters();
    this._hasLoaded = true;
  }

  _runQueuedLiveRefresh() {
    if (!this._liveRefreshQueued) return;
    const refreshDialogMedia = Boolean(this._liveRefreshNeedsDialogMedia);
    this._liveRefreshQueued = false;
    this._liveRefreshNeedsDialogMedia = false;
    this._loadItems({ silent: true, refreshDialogMedia });
  }

  _infoMediaKey(item) {
    if (!item) return "";
    // Ignore query strings: signed URLs change on every list call for the same media.
    return [
      item.recording?.status || "",
      stripUrlQuery(item.recording?.local_url),
      stripUrlQuery(item.snapshot_url),
    ].join("|");
  }

  async _loadItemsSilently(refreshDialogMedia) {
    this._loading = true;
    const previousDialogItem = this._currentInfoItem();
    const previousMediaKey = this._infoMediaKey(previousDialogItem);
    let result;
    try {
      result = await this._hass.callWS({
        type: "frigate_feed/list",
      });
    } catch (_err) {
      // Keep the current list on transient errors; the next update or manual refresh retries.
      this._loading = false;
      this._runQueuedLiveRefresh();
      return;
    }
    this._loading = false;
    this._error = null;
    this._applyListResult(result);
    if (!this._infoDialog.open) {
      this._render();
    } else {
      const dialogItem = this._currentInfoItem();
      if (!dialogItem) {
        // The open detection disappeared (hidden or expired by Frigate retention).
        this._closeInfoDialog();
      } else if (refreshDialogMedia || this._infoMediaKey(dialogItem) !== previousMediaKey) {
        this._updateInfoDialogInPlace();
      } else {
        // Same media: keep the playing video, refresh only the surrounding details.
        this._updateInfoDialogInPlace({ keepMedia: true });
      }
      // The list behind the modal dialog is re-rendered when the dialog closes.
    }
    this._runQueuedLiveRefresh();
  }

  async _refreshRecording(item, showToast = true, showSpinner = true, finalAttempt = false) {
    if (!this._hass || !item?.id) return item?.recording || null;
    const keepDialogOpenForItem =
      this._infoDialog.open && this._infoDialog.itemId === item.id;
    if (keepDialogOpenForItem && showSpinner) {
      this._ignoreDialogCloseEvents = 2;
    }
    if (showSpinner) {
      this._resolvingIds.add(item.id);
      this._render();
    }
    try {
      const recording = await this._hass.callWS({
        type: "frigate_feed/resolve_recording",
        item_id: item.id,
        final_attempt: finalAttempt,
      });
      item.recording = recording;
      if (showToast) {
        if (recording.status === "linked") this._showToast("Recording linked");
        else if (recording.status === "not_found") this._showToast("Recording not found");
        else if (recording.status === "download_failed") this._showToast("Recording download failed");
        else this._showToast("Recording still pending");
      }
      return recording;
    } catch (err) {
      this._showToast(`Resolve failed: ${err?.message || err}`);
      return null;
    } finally {
      if (showSpinner) {
        this._resolvingIds.delete(item.id);
        this._render();
      }
    }
  }

  async _openFromThumbnail(item) {
    if (!item?.id) return;
    this._expandedClusterKey = "";
    this._openInfoDialog(item);
  }

  _openInfoDialog(item) {
    if (!item?.id) return;
    if (this._infoDialog.open) {
      this._infoDialog.itemId = item.id;
      this._videoControlsEnabled.delete(item.id);
      this._updateInfoDialogInPlace();
      return;
    }
    this._infoDialog = { open: true, itemId: item.id };
    this._videoControlsEnabled.delete(item.id);
    window.addEventListener("keydown", this._handleDialogKeyDown);
    this._render();
  }

  _closeInfoDialog() {
    this._infoDialog = { open: false, itemId: "" };
    this._videoControlsEnabled.clear();
    window.removeEventListener("keydown", this._handleDialogKeyDown);
    this._render();
  }

  _currentInfoItemIndex() {
    if (!this._infoDialog.open) return -1;
    return this._filteredItems.findIndex((item) => item.id === this._infoDialog.itemId);
  }

  _currentInfoItem() {
    if (!this._infoDialog.open) return null;
    return this._items.find((item) => item.id === this._infoDialog.itemId) || null;
  }

  _openPreviousInfoItem() {
    const idx = this._currentInfoItemIndex();
    if (idx <= 0) return;
    const prevItem = this._filteredItems[idx - 1];
    if (!prevItem) return;
    this._infoDialog.itemId = prevItem.id;
    this._videoControlsEnabled.delete(prevItem.id);
    this._updateInfoDialogInPlace();
  }

  _openNextInfoItem() {
    const idx = this._currentInfoItemIndex();
    if (idx < 0 || idx >= this._filteredItems.length - 1) return;
    const nextItem = this._filteredItems[idx + 1];
    if (!nextItem) return;
    this._infoDialog.itemId = nextItem.id;
    this._videoControlsEnabled.delete(nextItem.id);
    this._updateInfoDialogInPlace();
  }

  async _deleteItem(item) {
    if (!this._hass || !item?.id) return;
    try {
      await this._hass.callWS({
        type: "frigate_feed/delete_item",
        item_id: item.id,
      });
      this._items = this._items.filter((existing) => existing.id !== item.id);
      this._applyFilters();
      this._closeInfoDialog();
      this._showToast("Detection deleted");
    } catch (err) {
      this._showToast(`Delete failed: ${err?.message || err}`);
    }
  }

  _showToast(message) {
    const event = new Event("hass-notification", {
      bubbles: true,
      composed: true,
    });
    event.detail = { message };
    this.dispatchEvent(event);
  }

  _formatTime(ts) {
    if (!ts) return "";
    return new Date(ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  _formatDuration(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined) return "-";
    const seconds = Math.max(0, Number(totalSeconds) || 0);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  _formatDateTime(ts) {
    if (!ts) return "";
    return new Date(ts).toLocaleString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  _formatDate(ts) {
    if (!ts) return "";
    return new Date(ts).toLocaleDateString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  }

  _formatWeekdayAndTime(ts) {
    if (!ts) return "";
    return new Date(ts).toLocaleString([], {
      weekday: "long",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  _formatWeekday(ts) {
    if (!ts) return "";
    return new Date(ts).toLocaleString([], { weekday: "long" });
  }

  _formatStartEndLine(item) {
    if (!item?.start_ts) return "-";
    const start = new Date(item.start_ts);
    const end = item?.end_ts
      ? new Date(item.end_ts)
      : new Date(start.getTime() + Math.max(0, Number(item?.duration_s) || 0) * 1000);
    return `${this._formatDate(start.toISOString())} ${this._formatTime(start.toISOString())} - ${this._formatTime(end.toISOString())}`;
  }

  _videoControlsActive(item) {
    return Boolean(item?.id) && this._videoControlsEnabled.has(item.id);
  }

  _infoHeaderTitle(item) {
    if (!item) return this._t("detection_info");
    return `${this._formatTime(item.start_ts) || "-"} (${this._formatDuration(item.duration_s)}) - ${this._formatWeekday(item.start_ts) || "-"}`;
  }

  _infoRecordingFallbackText(status) {
    if (status === "not_found") return this._t("not_found");
    if (status === "download_failed") return this._t("download_failed");
    return this._t("pending");
  }

  _buildInfoDownloadsHtml(item) {
    const recordingHref = this._recordingLinkHref(item);
    const recordingName = this._recordingFilename(item) || "video.mp4";
    const recordingStatus = item?.recording?.status || "pending";
    const recordingAvailable = Boolean(recordingHref && recordingHref !== "#");
    const recordingTitle = recordingAvailable
      ? this._mediaFileDisplayPath(item)
      : this._infoRecordingFallbackText(recordingStatus);

    const photoHref = this._snapshotLinkHref(item);
    const photoName = this._snapshotFilename(item) || "snapshot.jpg";
    const photoAvailable = Boolean(photoHref && photoHref !== "#");
    const photoTitle = photoAvailable ? photoHref : this._t("no_snapshot");

    return `
      <div class="info-downloads-inline">
        <a
          class="download-inline${photoAvailable ? "" : " disabled"}"
          ${photoAvailable ? `href="${photoHref}" download="${photoName}"` : 'href="#" aria-disabled="true" tabindex="-1"'}
          ${photoAvailable ? `data-download-url="${photoHref}" data-download-name="${photoName}" data-download-label="${this._t("image")}" data-download-kind="image"` : ""}
          title="${photoTitle}"
        >
          <ha-icon icon="mdi:image"></ha-icon>
          <span>${this._t("image")}</span>
        </a>
        <span class="download-sep">|</span>
        <a
          class="download-inline${recordingAvailable ? "" : " disabled"}"
          ${recordingAvailable ? `href="${recordingHref}" download="${recordingName}"` : 'href="#" aria-disabled="true" tabindex="-1"'}
          ${recordingAvailable ? `data-download-url="${recordingHref}" data-download-name="${recordingName}" data-download-label="${this._t("video")}" data-download-kind="video"` : ""}
          title="${recordingTitle}"
        >
          <ha-icon icon="mdi:video"></ha-icon>
          <span>${this._t("video")}</span>
        </a>
      </div>
    `;
  }

  _buildInfoLinksHtml(item) {
    const entityId = encodeURIComponent(item?.source_entity_id || "");
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
    const startIso = start.toISOString();
    const endIso = end.toISOString();
    const historyPath = `/history?entity_id=${entityId}&start_date=${encodeURIComponent(startIso)}&end_date=${encodeURIComponent(endIso)}`;
    const logbookPath = `/logbook?entity_id=${entityId}&start_date=${encodeURIComponent(startIso)}&end_date=${encodeURIComponent(endIso)}`;
    return `
      <a class="internal-nav-link" href="${historyPath}" data-nav-path="${historyPath}">${this._t("history")}</a>
      <span class="download-sep">|</span>
      <a class="internal-nav-link" href="${logbookPath}" data-nav-path="${logbookPath}">${this._t("logbook")}</a>
    `;
  }

  _bindInfoActionLinks() {
    this.shadowRoot.querySelectorAll(".download-inline[data-download-url]").forEach((el) => {
      if (el.dataset.boundClick === "1") return;
      el.dataset.boundClick = "1";
      el.addEventListener("click", async (ev) => {
        ev.preventDefault();
        const url = el.getAttribute("data-download-url");
        if (!url) return;
        const fileName = el.getAttribute("data-download-name") || "";
        const label = el.getAttribute("data-download-label") || "";
        const kind = el.getAttribute("data-download-kind") || "";
        await this._shareOrDownloadAsset(url, fileName, label, kind);
      });
    });
    this.shadowRoot.querySelectorAll(".detection-chip[data-seek]").forEach((el) => {
      if (el.dataset.boundClick === "1") return;
      el.dataset.boundClick = "1";
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        this._seekInfoVideo(el.getAttribute("data-seek"));
      });
    });
    this.shadowRoot.querySelectorAll(".internal-nav-link[data-nav-path]").forEach((el) => {
      if (el.dataset.boundClick === "1") return;
      el.dataset.boundClick = "1";
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        const path = el.getAttribute("data-nav-path");
        if (!path) return;
        if (typeof this._hass?.navigate === "function") {
          this._hass.navigate(path);
          return;
        }
        window.history.pushState({}, "", path);
        window.dispatchEvent(new Event("location-changed"));
      });
    });
  }

  async _shareOrDownloadAsset(url, fileName, label, kind) {
    const downloadFallback = () => {
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName || "";
      link.rel = "noopener";
      link.target = "_blank";
      document.body.appendChild(link);
      link.click();
      link.remove();
    };
    const isLikelyMobile = () => {
      const ua = navigator?.userAgent || "";
      const touch = typeof window !== "undefined" && "ontouchstart" in window;
      const coarse = typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)")?.matches;
      return /iPhone|iPad|iPod|Android/i.test(ua) || (touch && coarse);
    };

    // Desktop UX: always download directly.
    if (!isLikelyMobile()) {
      downloadFallback();
      return;
    }

    const shareApi = navigator?.share;
    if (!shareApi) {
      downloadFallback();
      return;
    }

    const absoluteUrl = (() => {
      try {
        return new URL(url, window.location.origin).toString();
      } catch (_err) {
        return url;
      }
    })();

    const canShare = navigator?.canShare?.bind(navigator);
    const title = `${label}`;
    const text = fileName || label;
    const mimeType = kind === "image" ? "image/jpeg" : "video/mp4";
    // iOS Safari is strict about user activation timing.
    // Share URL first (no async fetch) to keep it inside the tap gesture.
    try {
      await navigator.share({ title, text, url: absoluteUrl });
      return;
    } catch (err) {
      if (err?.name === "AbortError") {
        return;
      }
    }

    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`http_${response.status}`);
      const blob = await response.blob();
      const file = new File([blob], fileName || `${kind}.bin`, { type: blob.type || mimeType });
      if (!canShare || canShare({ files: [file] })) {
        await navigator.share({ title, text, files: [file] });
        return;
      }
      downloadFallback();
    } catch (err) {
      if (err?.name === "AbortError") return;
      downloadFallback();
    }
  }

  _buildInfoMediaHtml(item) {
    const controlsActive = this._videoControlsActive(item);
    return `
      <div class="info-media-frame">
        ${
          item && item.recording?.local_url
            ? `
              <video class="info-video" ${controlsActive ? "controls" : ""} autoplay muted playsinline preload="auto" src="${item.recording.local_url}"></video>
            `
            : item && item.snapshot_url
              ? `<img class="info-snapshot" src="${item.snapshot_url}" alt="${item.camera_name || this._t("snapshot")}" loading="lazy" />`
              : `<div class="placeholder">${this._t("no_snapshot")}</div>`
        }
      </div>
    `;
  }

  _setupInfoVideoAutoplay() {
    if (!this._infoDialog.open) return;
    const infoVideoEl = this.shadowRoot?.querySelector("video.info-video");
    if (!infoVideoEl) return;
    const currentItem = this._currentInfoItem();
    if (!currentItem?.id) return;
    const requestedOffset = Number(currentItem?.recording?.start_offset_s);
    const initialOffsetSeconds =
      Number.isFinite(requestedOffset) && requestedOffset >= 0
        ? Math.max(0, requestedOffset - 2)
        : 0;
    let initialSeekApplied = false;
    const applyInitialSeek = () => {
      if (initialSeekApplied) return;
      const duration = Number(infoVideoEl.duration);
      if (!Number.isFinite(duration) || duration <= 0) return;
      if (initialOffsetSeconds <= 0) {
        initialSeekApplied = true;
        return;
      }
      const safeOffset = Math.min(initialOffsetSeconds, Math.max(0, duration - 0.25));
      try {
        infoVideoEl.currentTime = safeOffset;
        initialSeekApplied = true;
      } catch (_err) {
        // Some browsers may reject early seek before enough media is buffered.
      }
    };
    infoVideoEl.muted = true;
    infoVideoEl.defaultMuted = true;
    infoVideoEl.volume = 0;
    infoVideoEl.playsInline = true;
    infoVideoEl.preload = "auto";
    infoVideoEl.setAttribute("muted", "");
    infoVideoEl.setAttribute("playsinline", "");
    infoVideoEl.setAttribute("autoplay", "");
    if (infoVideoEl.readyState >= 1) {
      applyInitialSeek();
    } else {
      infoVideoEl.addEventListener("loadedmetadata", applyInitialSeek, { once: true });
    }
    const attemptPlay = () => {
      const playPromise = infoVideoEl.play();
      if (playPromise && typeof playPromise.catch === "function") {
        playPromise.catch(() => {
          // Autoplay may still be blocked by browser policy or power-saving mode.
        });
      }
    };
    infoVideoEl.load();
    attemptPlay();
    infoVideoEl.addEventListener("loadeddata", attemptPlay, { once: true });
    infoVideoEl.addEventListener("canplay", attemptPlay, { once: true });
    if (!this._videoControlsActive(currentItem)) {
      const enableControls = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        this._videoControlsEnabled.add(currentItem.id);
        this._updateInfoDialogInPlace();
      };
      infoVideoEl.addEventListener("click", enableControls, { once: true });
      infoVideoEl.addEventListener("touchstart", enableControls, { once: true, passive: false });
    }
  }

  _updateInfoDialogInPlace(options = {}) {
    if (!this._infoDialog.open || !this.shadowRoot) return;
    const keepMedia = Boolean(options.keepMedia);
    const dialogEl = this.shadowRoot.querySelector("ha-dialog");
    const infoItem = this._currentInfoItem();
    if (!dialogEl || !infoItem) {
      this._render();
      return;
    }

    const titleIconEl = this.shadowRoot.querySelector(".info-title ha-icon");
    const titleTextEl = this.shadowRoot.querySelector(".info-title-text");
    const mediaSlotEl = this.shadowRoot.querySelector(".info-media-slot");
    const detectionValueEl = this.shadowRoot.querySelector(".info-detection-value");
    const detectionsRowEl = this.shadowRoot.querySelector(".info-detections-row");
    const startEndEl = this.shadowRoot.querySelector(".info-start-end");
    const downloadsSlotEl = this.shadowRoot.querySelector(".info-downloads-slot");
    const linksValueEl = this.shadowRoot.querySelector(".info-links");
    const prevInfoButton = this.shadowRoot.querySelector("button.prev-info-body");
    const nextInfoButton = this.shadowRoot.querySelector("button.next-info-body");
    const resetInfoButton = this.shadowRoot.querySelector("button.reset-info");
    const resetIcon = resetInfoButton?.querySelector("ha-icon");

    titleIconEl?.setAttribute("icon", this._labelIconName(infoItem.label));
    if (titleTextEl) titleTextEl.textContent = this._infoHeaderTitle(infoItem);
    if (mediaSlotEl && !keepMedia) mediaSlotEl.innerHTML = this._buildInfoMediaHtml(infoItem);
    if (detectionValueEl) detectionValueEl.textContent = this._labelsText(infoItem);
    if (detectionsRowEl) detectionsRowEl.outerHTML = this._buildInfoDetectionsRowHtml(infoItem);
    if (startEndEl) startEndEl.textContent = this._formatStartEndLine(infoItem);
    if (linksValueEl) linksValueEl.innerHTML = this._buildInfoLinksHtml(infoItem);
    if (downloadsSlotEl) downloadsSlotEl.innerHTML = this._buildInfoDownloadsHtml(infoItem);
    this._bindInfoActionLinks();

    const idx = this._currentInfoItemIndex();
    if (prevInfoButton) prevInfoButton.disabled = idx <= 0;
    if (nextInfoButton) nextInfoButton.disabled = idx >= this._filteredItems.length - 1;
    if (resetInfoButton && resetIcon) {
      const isResolving = this._resolvingIds.has(infoItem.id);
      resetInfoButton.classList.toggle("resolving", isResolving);
      resetInfoButton.disabled = isResolving;
      resetIcon.setAttribute("icon", isResolving ? "mdi:loading" : "mdi:arrow-u-left-top");
      resetIcon.classList.toggle("spin", isResolving);
    }

    if (!keepMedia) this._setupInfoVideoAutoplay();
  }

  _mediaFolderDisplayPath(item) {
    const dt = new Date(item?.start_ts || Date.now());
    const year = dt.getFullYear();
    const month = String(dt.getMonth() + 1).padStart(2, "0");
    const day = String(dt.getDate()).padStart(2, "0");
    const camera = item?.camera_name || item?.frigate?.camera || "Camera";
    return `/Frigate/${camera}/${year}-${month}-${day}`;
  }

  _mediaFilenameBase(item) {
    const dt = new Date(item?.start_ts || Date.now());
    const hh = String(dt.getHours()).padStart(2, "0");
    const mi = String(dt.getMinutes()).padStart(2, "0");
    const ss = String(dt.getSeconds()).padStart(2, "0");
    const camera =
      slugifyCardValue(item?.frigate?.camera) || slugifyCardValue(item?.camera_name) || "camera";
    const label = slugifyCardValue(normalizeCardLabel(item?.label)) || "detection";
    return `${camera}_${label}_${hh}${mi}${ss}`;
  }

  _recordingFilename(item) {
    return `${this._mediaFilenameBase(item)}.mp4`;
  }

  _mediaFileDisplayPath(item) {
    const frigateId = item?.frigate?.review_id || item?.frigate?.event_id;
    const fileName = frigateId ? String(frigateId) : this._recordingFilename(item);
    return `${this._mediaFolderDisplayPath(item)}/${fileName}`;
  }

  _recordingLinkHref(item) {
    const localUrl = item?.recording?.local_url;
    if (typeof localUrl === "string" && localUrl) {
      return localUrl;
    }
    return "#";
  }

  _snapshotFilename(item) {
    const snapshotUrl = item?.snapshot_url;
    if (typeof snapshotUrl === "string" && snapshotUrl) {
      return `${this._mediaFilenameBase(item)}.jpg`;
    }
    return "";
  }

  _snapshotLinkHref(item) {
    const snapshotUrl = item?.snapshot_url;
    if (typeof snapshotUrl === "string" && snapshotUrl) {
      return snapshotUrl;
    }
    return "#";
  }

  _labelsText(item) {
    return itemLabels(item).map((label) => this._labelText(label)).join(", ");
  }

  _labelIcons(item) {
    return itemLabels(item).map((label) => this._labelIcon(label)).join("");
  }

  _buildInfoDetectionsRowHtml(item) {
    const detections = Array.isArray(item?.detections) ? item.detections : [];
    // A single detection is the item itself; only grouped items get seek chips.
    if (detections.length < 2) return `<div class="info-row info-detections-row" hidden></div>`;
    const seekable = item.recording?.status === "linked" && Boolean(item.recording?.local_url);
    const chips = detections
      .map((detection) => {
        const offset = Number(detection.start_offset_s);
        const label = normalizeCardLabel(detection.label);
        const title = `${this._labelText(label)}${detection.sub_label ? ` (${detection.sub_label})` : ""}`;
        return `
          <button class="detection-chip" type="button" data-seek="${Number.isFinite(offset) ? offset : 0}"
            title="${title} - ${this._t("seek_to_detection")}" aria-label="${title} ${formatClipOffset(offset)}"
            ${seekable ? "" : "disabled"}>
            <ha-icon icon="${this._labelIconName(label)}"></ha-icon><span>${formatClipOffset(offset)}</span>
          </button>
        `;
      })
      .join("");
    return `<div class="info-row info-detections-row"><span class="info-label">${this._t("detections")}:</span><span class="info-value detection-chips">${chips}</span></div>`;
  }

  _seekInfoVideo(seconds) {
    const videoEl = this.shadowRoot?.querySelector("video.info-video");
    if (!videoEl) return;
    const duration = Number(videoEl.duration);
    const target = Math.max(0, Number(seconds) - 1);
    try {
      videoEl.currentTime = Number.isFinite(duration) && duration > 0 ? Math.min(target, Math.max(0, duration - 0.25)) : target;
    } catch (_err) {
      return;
    }
    const playPromise = videoEl.play();
    if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
  }

  // Thumbnails are cropped to the tile; keep the detected object in view.
  _focusThumb(img) {
    if (!img.complete || !img.naturalWidth) return;
    let box;
    try {
      box = JSON.parse(img.dataset.focusBox || "null");
    } catch (_err) {
      return;
    }
    img.style.objectPosition = focusObjectPosition(
      box, img.naturalWidth, img.naturalHeight, img.clientWidth, img.clientHeight
    );
  }

  _focusThumbs() {
    this.shadowRoot?.querySelectorAll("li.item img[data-focus-box]").forEach((img) => this._focusThumb(img));
  }

  _labelIcon(label) {
    const icon = this._labelIconName(label);
    const labelText = this._labelText(label);
    return `
      <span class="label-icon" title="${labelText}" aria-label="${labelText}">
        <ha-icon icon="${icon}"></ha-icon>
      </span>
    `;
  }

  _render() {
    if (!this.shadowRoot || !this._config) {
      return;
    }

    const showTimeline = this._config.show_timeline !== false;
    const pagedItems = this._pagedItems();
    const totalPages = this._totalPages();
    const filterPills = this._availableLabels
      .map((label) => {
        const active = this._activeLabels.has(label);
        const icon = this._labelIconName(label);
        return `
            <button class="filter-pill${active ? " active" : ""}" data-filter-label="${label}" aria-pressed="${active ? "true" : "false"}">
              <ha-icon icon="${icon}"></ha-icon>
              <span>${this._labelText(label)}</span>
            </button>
        `;
      })
      .join("");
    const nowTs = Date.now();
    const timelineHours = Number.isFinite(this._retentionHours) && this._retentionHours > 0
      ? this._retentionHours
      : 24;
    const timelineStartTs = nowTs - timelineHours * 60 * 60 * 1000;
    const timelineEntries = this._filteredItems
      .map((item) => ({ item, ts: new Date(item.start_ts).getTime() }))
      .filter((entry) => Number.isFinite(entry.ts))
      .sort((a, b) => a.ts - b.ts);
    const timelineHtml = (() => {
      const startTs = timelineStartTs;
      const endTs = nowTs;
      const range = endTs - startTs;
      const clusterGapPercent = 2.4;
      const hourMarks = (() => {
        if (range <= 0) return "";
        const firstHour = new Date(startTs);
        firstHour.setMinutes(0, 0, 0);
        if (firstHour.getTime() < startTs) {
          firstHour.setHours(firstHour.getHours() + 1);
        }
        const marks = [];
        for (let ts = firstHour.getTime(); ts < endTs; ts += 60 * 60 * 1000) {
          const ratio = (ts - startTs) / range;
          const left = Math.min(100, Math.max(0, ratio * 100));
          const hour24 = new Date(ts).getHours();
          const hour12 = hour24 % 12 || 12;
          const hourLabel = hour12 % 2 === 0 ? String(hour12) : "";
          marks.push(
            `
              <span class="timeline-hour-mark" style="left:${left}%" title="${hourLabel}:00" aria-hidden="true">
                <span class="timeline-hour-label">${hourLabel}</span>
              </span>
            `,
          );
        }
        return marks.join("");
      })();
      const clusteredEntries = (() => {
        const buckets = [];
        for (const { item, ts } of timelineEntries) {
          const ratio = range <= 0 ? 0.5 : (ts - startTs) / range;
          const left = Math.min(100, Math.max(0, ratio * 100));
          const current = buckets[buckets.length - 1];
          if (!current || left - current.left > clusterGapPercent) {
            buckets.push({
              left,
              items: [item],
              minTs: ts,
              maxTs: ts,
            });
            continue;
          }
          current.items.push(item);
          current.minTs = Math.min(current.minTs, ts);
          current.maxTs = Math.max(current.maxTs, ts);
          const total = current.items.length;
          current.left = (current.left * (total - 1) + left) / total;
        }
        return buckets;
      })();
      if (this._expandedClusterKey) {
        // Keep an expanded cluster open when a live refresh adds/removes items in it.
        const clusterKeyOf = (cluster) =>
          [...cluster.items]
            .sort((a, b) => new Date(b.start_ts).getTime() - new Date(a.start_ts).getTime())
            .map((item) => item.id)
            .join(",");
        const previousIds = new Set(this._expandedClusterKey.split(","));
        const keys = clusteredEntries.map((cluster) => clusterKeyOf(cluster));
        if (!keys.includes(this._expandedClusterKey)) {
          const matchIndex = clusteredEntries.findIndex(
            (cluster) =>
              cluster.items.length > 1 && cluster.items.some((item) => previousIds.has(item.id)),
          );
          this._expandedClusterKey = matchIndex >= 0 ? keys[matchIndex] : "";
        }
      }
      const markers = clusteredEntries
        .map((cluster) => {
          const items = [...cluster.items].sort(
            (a, b) => new Date(b.start_ts).getTime() - new Date(a.start_ts).getTime(),
          );
          const ids = items.map((item) => item.id).join(",");
          const isCluster = items.length > 1;
          const markerLabel = isCluster ? String(items.length) : "";
          const icon = this._labelIconName(items[0].label);
          const title = isCluster
            ? `${items.length} detections (${this._formatDateTime(new Date(cluster.minTs).toISOString())} - ${this._formatDateTime(new Date(cluster.maxTs).toISOString())})`
            : this._formatDateTime(items[0].start_ts);
          const expandedHtml =
            isCluster && this._expandedClusterKey === ids
              ? `
                <div class="timeline-cluster-expanded" style="left:${cluster.left}%">
                  ${items
                    .map(
                      (entry) => `
                        <button
                          class="timeline-expanded-item"
                          data-timeline-id="${entry.id}"
                          title="${this._formatDateTime(entry.start_ts)}"
                          aria-label="${this._formatDateTime(entry.start_ts)}"
                        >
                          <ha-icon icon="${this._labelIconName(entry.label)}"></ha-icon>
                        </button>
                      `,
                    )
                    .join("")}
                  <button class="timeline-expanded-close" type="button" aria-label="${this._t("close_info_dialog")}">✕</button>
                </div>
              `
              : "";
          return `
            ${expandedHtml}
            <button
              class="timeline-marker${isCluster ? " cluster" : ""}"
              data-timeline-ids="${ids}"
              style="left:${cluster.left}%"
              title="${title}"
              aria-label="${title}"
            >
              ${isCluster ? `<span class="cluster-count">${markerLabel}</span>` : `<ha-icon icon="${icon}"></ha-icon>`}
            </button>
          `;
        })
        .join("");
      return `
        <div class="timeline" role="list" aria-label="${this._t("event")}">
          <div class="timeline-track"></div>
          ${hourMarks}
          ${markers}
        </div>
      `;
    })();
    // `thumbnail: object` (default) shows the snapshot cropped to the object, like Frigate's
    // Explore page; `frame` (or items without a crop) shows the whole frame, focused on the box.
    const objectThumbnails = this._config.thumbnail !== "frame";
    const listHtml = pagedItems
      .map((item) => {
        const image =
          objectThumbnails && item.thumbnail_url
            ? `<img src="${item.thumbnail_url}" alt="${item.camera_name}" loading="lazy" />`
            : item.snapshot_url
              ? `<img src="${item.snapshot_url}" alt="${item.camera_name}" loading="lazy"${
                  Array.isArray(item.frigate?.box) ? ` data-focus-box="${JSON.stringify(item.frigate.box)}"` : ""
                } />`
              : `<div class="placeholder">${this._t("no_snapshot")}</div>`;

        return `
          <li class="item" data-id="${item.id}">
            <button class="thumb" aria-label="${this._t("open_recording_preview")}">
              ${image}
              <span class="overlay top-left">
                ${this._labelIcons(item)}
              </span>
              <span class="overlay bottom-left line2">${this._formatTime(item.start_ts)} (${this._formatDuration(item.duration_s)})</span>
            </button>
          </li>
        `;
      })
      .join("");
    const paginationHtml =
      this._filteredItems.length > this._pageSize()
        ? `
      <div class="pagination">
        <button class="page-nav" data-page-nav="prev" ${this._page <= 1 ? "disabled" : ""}>${this._t("previous")}</button>
        <span class="page-info">${this._t("page")} ${this._page} / ${totalPages}</span>
        <button class="page-nav" data-page-nav="next" ${this._page >= totalPages ? "disabled" : ""}>${this._t("next")}</button>
      </div>
      `
        : "";

    const infoItem = this._infoDialog.open
      ? this._items.find((item) => item.id === this._infoDialog.itemId) || null
      : null;
    const infoLabelIcon = infoItem ? this._labelIconName(infoItem.label) : "mdi:account";
    const infoHeaderTitle = this._infoHeaderTitle(infoItem);
    const infoIsResolving = infoItem ? this._resolvingIds.has(infoItem.id) : false;
    const infoDownloadsHtml = infoItem ? this._buildInfoDownloadsHtml(infoItem) : "";
    const infoMediaHtml = infoItem ? this._buildInfoMediaHtml(infoItem) : "";
    const infoDialogHtml =
      this._infoDialog.open && infoItem
        ? `
      <ha-dialog open width="medium">
        <span slot="headerTitle" class="info-title">
          <ha-icon icon="${infoLabelIcon}"></ha-icon>
          <span class="info-title-text">${infoHeaderTitle}</span>
        </span>
        <div class="info-body">
          <div class="info-media-slot">${infoMediaHtml}</div>
          <div class="info-nav-row">
            <button
              class="nav-info-body prev-info-body"
              type="button"
              aria-label="${this._t("previous")}"
              ${this._currentInfoItemIndex() <= 0 ? "disabled" : ""}
            ><ha-icon icon="mdi:chevron-left"></ha-icon><span>${this._t("previous")}</span></button>
            <button
              class="nav-info-body next-info-body"
              type="button"
              aria-label="${this._t("next")}"
              ${this._currentInfoItemIndex() >= this._filteredItems.length - 1 ? "disabled" : ""}
            ><span>${this._t("next")}</span><ha-icon icon="mdi:chevron-right"></ha-icon></button>
          </div>
          <div class="info-row"><span class="info-label">${this._t("detection")}:</span><span class="info-value info-detection-value">${this._labelsText(infoItem)}</span></div>
          ${this._buildInfoDetectionsRowHtml(infoItem)}
          <div class="info-row"><span class="info-label">${this._t("event")}:</span><span class="info-value info-start-end">${this._formatStartEndLine(infoItem)}</span></div>
          <div class="info-row"><span class="info-label">${this._t("links")}:</span><span class="info-value info-links">${this._buildInfoLinksHtml(infoItem)}</span></div>
          <div class="info-row"><span class="info-label">${this._t("download")}:</span><span class="info-value info-downloads-slot">${infoDownloadsHtml}</span></div>
        </div>
        <div slot="footer" class="info-footer">
          <button class="reset-info${infoIsResolving ? " resolving" : ""}" type="button" ${infoIsResolving ? "disabled" : ""}>
            <ha-icon class="${infoIsResolving ? "spin" : ""}" icon="${infoIsResolving ? "mdi:loading" : "mdi:arrow-u-left-top"}"></ha-icon>
            <span>${this._t("reset")}</span>
          </button>
          <button class="delete-info" type="button">
            <ha-icon icon="mdi:trash-can-outline"></ha-icon>
            <span>${this._t("delete")}</span>
          </button>
        </div>
      </ha-dialog>
      `
        : "";

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 10px; }
        .topbar { display: flex; justify-content: flex-start; align-items: center; gap: 10px; margin-bottom: 18px; }
        .filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .filter-pill { border: 1px solid rgba(255,255,255,0.22); background: transparent; color: var(--primary-text-color); border-radius: 999px; height: 30px; padding: 0 10px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; font-size: 12px; opacity: 0.55; }
        .filter-pill ha-icon { --mdc-icon-size: 14px; }
        .filter-pill.active { border-color: #fff; color: #fff; opacity: 1; }
        .filter-pill:hover { opacity: 0.9; background: rgba(255,255,255,0.08); }
        .timeline { position: relative; height: 40px; margin: 12px 6px 12px; }
        .timeline-track { position: absolute; left: 4px; right: 4px; top: 50%; transform: translateY(-50%); height: 2px; border-radius: 999px; background: rgba(255,255,255,0.3); }
        .timeline-hour-mark { position: absolute; top: 50%; transform: translateX(-50%); width: 1px; height: 8px; background: rgba(255,255,255,0.32); pointer-events: none; }
        .timeline-hour-label { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); font-size: 9px; line-height: 1; color: rgba(255,255,255,0.55); font-variant-numeric: tabular-nums; }
        .timeline-marker { position: absolute; top: calc(50% - 18px); transform: translate(-50%, -50%); width: 18px; height: 18px; border: 0; border-radius: 999px; background: rgba(210,210,210,0.88); color: rgba(28,28,28,0.95); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
        .timeline-marker::after { content: ""; position: absolute; left: 50%; top: 100%; transform: translateX(-50%); width: 1px; height: 9px; background: rgba(255,255,255,0.35); pointer-events: none; }
        .timeline-marker:hover { background: rgba(235,235,235,0.96); color: #111; }
        .timeline-marker.link-hover { background: #fff; color: #111; }
        .timeline-marker ha-icon { --mdc-icon-size: 12px; pointer-events: none; }
        .timeline-marker.cluster { font-size: 9px; font-weight: 700; font-variant-numeric: tabular-nums; }
        .cluster-count { pointer-events: none; }
        .timeline-cluster-expanded { position: absolute; top: calc(50% - 18px); transform: translate(-50%, -50%); display: inline-flex; align-items: center; gap: 4px; padding: 4px 6px; border-radius: 999px; background: rgba(20,20,20,0.85); border: 1px solid rgba(255,255,255,0.28); z-index: 5; }
        .timeline-expanded-item { width: 18px; height: 18px; border: 0; border-radius: 999px; background: rgba(210,210,210,0.96); color: rgba(28,28,28,0.95); display: inline-flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
        .timeline-expanded-item:hover { background: #fff; color: #111; }
        .timeline-expanded-item.link-hover { background: #fff; color: #111; }
        .timeline-expanded-item ha-icon { --mdc-icon-size: 12px; pointer-events: none; }
        .timeline-expanded-close { width: 18px; height: 18px; border: 0; border-radius: 999px; background: rgba(255,255,255,0.16); color: #fff; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; font-size: 12px; line-height: 1; }
        .timeline-expanded-close:hover { background: rgba(255,255,255,0.28); }
        .pagination { display: flex; justify-content: center; align-items: center; gap: 10px; margin-top: 10px; }
        .page-info { color: var(--secondary-text-color); font-size: 12px; min-width: 84px; text-align: center; }
        button.page-nav { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); border-radius: 8px; height: 28px; padding: 0 10px; cursor: pointer; font-size: 12px; }
        button.page-nav:hover { background: var(--secondary-background-color); }
        button.page-nav:disabled { opacity: 0.6; cursor: default; }
        ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
        .item { position: relative; padding: 0; border-radius: 10px; overflow: hidden; background: rgba(255, 255, 255, 0.04); }
        .thumb { position: relative; display: block; width: 100%; height: clamp(140px, 22vw, 190px); overflow: hidden; border-radius: 10px; background: #111; border: 1px solid var(--divider-color); padding: 0; cursor: pointer; appearance: none; -webkit-appearance: none; }
        .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .thumb::before { content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 1; }
        .thumb::after { content: ""; position: absolute; inset: 0; border-radius: inherit; box-sizing: border-box; border: 2px solid transparent; pointer-events: none; z-index: 4; transition: border-color 120ms ease; }
        .thumb:hover::after { border-color: rgba(255,255,255,0.9); }
        .thumb.link-hover::after { border-color: rgba(255,255,255,0.9); }
        .overlay { position: absolute; z-index: 3; display: inline-flex; align-items: center; }
        .overlay.top-left { top: 8px; left: 8px; }
        .overlay.bottom-left { left: 8px; bottom: 8px; max-width: calc(100% - 16px); }
        .placeholder { color: #ddd; font-size: 11px; padding: 8px; }
        .label-icon { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; }
        .label-icon ha-icon { --mdc-icon-size: 18px; color: #fff; filter: drop-shadow(0 1px 2px rgba(0,0,0,0.95)) drop-shadow(0 0 7px rgba(0,0,0,0.55)); }
        .line2 { color: rgba(255,255,255,0.95); font-size: 12px; font-weight: 500; text-shadow: 0 1px 2px rgba(0,0,0,0.9), 0 0 8px rgba(0,0,0,0.55); display: inline-block; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; box-sizing: border-box; }
        .empty { color: var(--secondary-text-color); font-size: 13px; padding: 8px 2px; }
        .error { color: var(--error-color); font-size: 12px; white-space: pre-wrap; }
        ha-dialog {
          --dialog-content-padding: 0;
          --ha-dialog-width-md: 760px;
        }
        .info-body {
          width: 100%;
          max-width: 100%;
          min-width: 0;
          padding: 12px 16px;
          display: grid;
          gap: 10px;
          color: var(--primary-text-color);
          box-sizing: border-box;
          max-height: min(68vh, 560px);
          overflow-y: auto;
          overflow-x: hidden;
          border-bottom: 1px solid var(--divider-color);
          user-select: text;
          -webkit-user-select: text;
        }
        .info-body div, .info-body span, .info-body strong, .info-body a {
          user-select: text;
          -webkit-user-select: text;
        }
        .detection-chips { display: inline-flex; flex-wrap: wrap; gap: 6px; }
        .detection-chip { border: 1px solid rgba(255,255,255,0.22); background: transparent; color: var(--primary-text-color); border-radius: 999px; height: 26px; padding: 0 9px; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-variant-numeric: tabular-nums; }
        .detection-chip ha-icon { --mdc-icon-size: 14px; }
        .detection-chip:hover:not([disabled]) { background: rgba(255,255,255,0.08); border-color: #fff; }
        .detection-chip[disabled] { opacity: 0.5; cursor: default; }
        .info-row[hidden] { display: none; }
        .info-row { display: flex; align-items: baseline; gap: 6px; font-size: 13px; color: var(--primary-text-color); line-height: 1.35; }
        .info-label { font-weight: 500; color: inherit; }
        .info-value { color: inherit; }
        .info-links { display: inline-flex; align-items: center; gap: 6px; }
        .info-body a { color: inherit; text-decoration: underline; }
        .info-body a:hover { opacity: 0.9; }
        .info-nav-row { display: flex; gap: 10px; width: 100%; }
        .nav-info-body {
          flex: 1 1 50%;
          min-width: 0;
          box-sizing: border-box;
          border: 1px solid var(--divider-color);
          border-radius: 10px;
          height: 40px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          padding: 0 10px;
          color: var(--primary-text-color);
          background: transparent;
          cursor: pointer;
        }
        .nav-info-body:hover { background: var(--secondary-background-color); }
        .nav-info-body:disabled { opacity: 0.45; cursor: default; }
        .nav-info-body ha-icon { --mdc-icon-size: 18px; }
        .info-downloads-inline {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: inherit;
        }
        .download-inline {
          height: 24px;
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 0 2px;
          text-decoration: none;
          color: inherit;
          font-size: 13px;
        }
        .download-inline:hover { text-decoration: underline; }
        .download-inline.disabled {
          opacity: 0.55;
          pointer-events: none;
        }
        .download-inline ha-icon { --mdc-icon-size: 14px; }
        .download-sep { color: inherit; opacity: 0.7; font-size: 12px; }
        .info-media-slot { min-width: 0; }
        .info-media-frame {
          width: 100%;
          max-width: 100%;
          box-sizing: border-box;
          border-radius: 8px;
          border: 1px solid var(--divider-color);
          background: #000;
          overflow: hidden;
        }
        /* Full width at the media's own aspect ratio; tall media is letterboxed. */
        .info-video, .info-snapshot, .info-media-frame .placeholder {
          width: 100%;
          max-width: 100%;
          height: auto;
          max-height: 70vh;
          box-sizing: border-box;
          display: block;
        }
        /* Until the metadata is loaded, reserve a 16:9 box instead of the 2:1 default. */
        .info-video { background: #000; object-fit: contain; aspect-ratio: auto 16 / 9; }
        .info-snapshot { object-fit: contain; }
        .info-media-frame .placeholder { aspect-ratio: 16 / 9; }
        .info-body .placeholder {
          display: grid;
          place-items: center;
          color: var(--secondary-text-color);
          background: rgba(255,255,255,0.05);
          font-size: 13px;
          box-sizing: border-box;
          overflow-wrap: anywhere;
          text-align: center;
          padding: 8px;
        }
        .info-title {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          min-width: 0;
        }
        .info-title ha-icon { --mdc-icon-size: 20px; flex: 0 0 auto; }
        .info-title span {
          display: block;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .info-footer { box-sizing: border-box; }
        .reset-info, .delete-info { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); border-radius: 8px; height: 34px; padding: 0 12px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
        .reset-info:hover { background: var(--secondary-background-color); }
        .reset-info.resolving { opacity: 0.7; }
        .reset-info:disabled { cursor: default; }
        .spin { animation: rf-spin 1s linear infinite; }
        @keyframes rf-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        .delete-info { border-color: #c03b3b; color: #d64545; }
        .delete-info:hover { background: rgba(214, 69, 69, 0.1); }
        .reset-info ha-icon, .delete-info ha-icon { --mdc-icon-size: 16px; }
      </style>
      <ha-card>
        <div class="topbar">
          <div class="filters">
            ${filterPills}
          </div>
        </div>
        ${showTimeline ? timelineHtml : ""}
        ${this._error ? `<div class="error">${this._error}</div>` : ""}
        ${this._filteredItems.length ? `<ul>${listHtml}</ul>${paginationHtml}` : `<div class="empty">${this._t("no_detections")}</div>`}
      </ha-card>
      ${infoDialogHtml}
    `;
    const setTimelineHoverState = (itemId, hovered) => {
      if (!itemId) return;
      this.shadowRoot.querySelectorAll(".timeline-marker[data-timeline-ids]").forEach((marker) => {
        const idsRaw = marker.getAttribute("data-timeline-ids") || "";
        const ids = idsRaw
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean);
        marker.classList.toggle("link-hover", hovered && ids.includes(itemId));
      });
      this.shadowRoot.querySelectorAll(".timeline-expanded-item[data-timeline-id]").forEach((marker) => {
        marker.classList.toggle(
          "link-hover",
          hovered && marker.getAttribute("data-timeline-id") === itemId,
        );
      });
    };
    const setThumbHoverState = (itemIds, hovered) => {
      if (!itemIds?.length) return;
      const idSet = new Set(itemIds);
      this.shadowRoot.querySelectorAll("li.item[data-id]").forEach((itemEl) => {
        const itemId = itemEl.getAttribute("data-id");
        const thumb = itemEl.querySelector("button.thumb");
        if (!itemId || !thumb) return;
        thumb.classList.toggle("link-hover", hovered && idSet.has(itemId));
      });
    };

    this._focusThumbs();
    this.shadowRoot.querySelectorAll("li.item").forEach((el) => {
      const id = el.getAttribute("data-id");
      const item = this._items.find((x) => x.id === id);
      if (!item) return;

      const thumb = el.querySelector("button.thumb");

      if (thumb) {
        thumb.addEventListener("mouseenter", () => setTimelineHoverState(id, true));
        thumb.addEventListener("mouseleave", () => setTimelineHoverState(id, false));
        thumb.addEventListener("click", (ev) => {
          ev.preventDefault();
          this._openFromThumbnail(item);
        });
      }
    });

    this.shadowRoot.querySelectorAll(".filter-pill").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        const label = normalizeCardLabel(el.getAttribute("data-filter-label"));
        if (!SUPPORTED_CARD_LABELS.includes(label)) return;
        this._toggleLabelFilter(label);
      });
    });
    this.shadowRoot.querySelectorAll(".timeline-marker").forEach((el) => {
      const ids = (el.getAttribute("data-timeline-ids") || "")
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean);
      el.addEventListener("mouseenter", () => setThumbHoverState(ids, true));
      el.addEventListener("mouseleave", () => setThumbHoverState(ids, false));
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        const idsRaw = el.getAttribute("data-timeline-ids") || "";
        const markerIds = idsRaw
          .split(",")
          .map((id) => id.trim())
          .filter(Boolean);
        const id = markerIds[0];
        if (!id) return;
        if (markerIds.length > 1) {
          const clusterKey = markerIds.join(",");
          this._expandedClusterKey = this._expandedClusterKey === clusterKey ? "" : clusterKey;
          this._render();
          return;
        }
        const item = this._items.find((x) => x.id === id);
        if (!item) return;
        this._openFromThumbnail(item);
      });
    });
    this.shadowRoot.querySelectorAll(".timeline-expanded-item").forEach((el) => {
      const id = el.getAttribute("data-timeline-id");
      if (id) {
        el.addEventListener("mouseenter", () => setThumbHoverState([id], true));
        el.addEventListener("mouseleave", () => setThumbHoverState([id], false));
      }
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        const itemId = el.getAttribute("data-timeline-id");
        if (!itemId) return;
        const item = this._items.find((x) => x.id === itemId);
        if (!item) return;
        this._openFromThumbnail(item);
      });
    });
    this.shadowRoot.querySelectorAll(".timeline-expanded-close").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        this._expandedClusterKey = "";
        this._render();
      });
    });
    const cardEl = this.shadowRoot.querySelector("ha-card");
    cardEl?.addEventListener("click", (ev) => {
      if (!this._expandedClusterKey) return;
      const path = ev.composedPath();
      const clickedTimelineOverlay = path.some(
        (node) =>
          node instanceof Element &&
          (node.classList.contains("timeline-marker") ||
            node.classList.contains("timeline-cluster-expanded") ||
            node.classList.contains("timeline-expanded-item")),
      );
      if (clickedTimelineOverlay) return;
      this._expandedClusterKey = "";
      this._render();
    });

    this.shadowRoot.querySelectorAll("button.page-nav").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        const action = el.getAttribute("data-page-nav");
        if (action === "prev" && this._page > 1) {
          this._page -= 1;
          this._render();
          return;
        }
        if (action === "next" && this._page < this._totalPages()) {
          this._page += 1;
          this._render();
        }
      });
    });

    const prevInfoButton = this.shadowRoot.querySelector("button.prev-info-body");
    prevInfoButton?.addEventListener("click", (ev) => {
      ev.preventDefault();
      this._openPreviousInfoItem();
    });
    const nextInfoButton = this.shadowRoot.querySelector("button.next-info-body");
    nextInfoButton?.addEventListener("click", (ev) => {
      ev.preventDefault();
      this._openNextInfoItem();
    });
    const resetInfoButton = this.shadowRoot.querySelector("button.reset-info");
    resetInfoButton?.addEventListener("click", (ev) => {
      ev.preventDefault();
      const currentInfoItem = this._currentInfoItem();
      if (!currentInfoItem) return;
      this._refreshRecording(currentInfoItem, true, true, true);
    });
    const deleteInfoButton = this.shadowRoot.querySelector("button.delete-info");
    deleteInfoButton?.addEventListener("click", (ev) => {
      ev.preventDefault();
      const currentInfoItem = this._currentInfoItem();
      if (!currentInfoItem) return;
      this._deleteItem(currentInfoItem);
    });
    const infoDialogEl = this.shadowRoot.querySelector("ha-dialog");
    this._setupInfoVideoAutoplay();
    this._bindInfoActionLinks();
    infoDialogEl?.addEventListener("closed", () => {
      if (this._ignoreDialogCloseEvents > 0) {
        this._ignoreDialogCloseEvents -= 1;
        return;
      }
      if (this._infoDialog.open) {
        this._closeInfoDialog();
      }
    });
    infoDialogEl?.addEventListener("close", () => {
      if (this._ignoreDialogCloseEvents > 0) {
        this._ignoreDialogCloseEvents -= 1;
        return;
      }
      if (this._infoDialog.open) {
        this._closeInfoDialog();
      }
    });
  }
}

if (!customElements.get("frigate-feed-card")) {
  customElements.define("frigate-feed-card", FrigateFeedCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "frigate-feed-card",
  name: "Frigate Feed Card",
  description: "Timeline of Frigate detections",
});

class FrigateFeedCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _languageCode() {
    const raw = String(this._hass?.language || document.documentElement.lang || "en").toLowerCase();
    if (CARD_I18N[raw]) return raw;
    const base = raw.split("-")[0];
    if (CARD_I18N[base]) return base;
    return "en";
  }

  _t(key) {
    const lang = this._languageCode();
    return CARD_I18N[lang]?.[key] || CARD_I18N.en[key] || key;
  }

  _emitConfig(next) {
    this._config = next;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: next },
        bubbles: true,
        composed: true,
      })
    );
  }

  _onNumberChange(key, value, fallback) {
    const parsed = Number.parseInt(value, 10);
    const next = { ...this._config, [key]: Number.isFinite(parsed) ? parsed : fallback };
    this._emitConfig(next);
  }

  _onBooleanChange(key, checked) {
    const next = { ...this._config, [key]: Boolean(checked) };
    this._emitConfig(next);
  }

  _render() {
    if (!this.shadowRoot) return;
    const pageSize = Number(this._config?.page_size ?? 20);
    const showTimeline = this._config?.show_timeline !== false;
    const thumbnail = this._config?.thumbnail === "frame" ? "frame" : "object";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .grid { display: grid; gap: 10px; }
        .field { display: grid; gap: 4px; }
        .field.toggle { display: flex; align-items: center; justify-content: space-between; }
        label { color: var(--primary-text-color); font-size: 13px; }
        input[type="text"], input[type="number"], select {
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          color: var(--primary-text-color);
          border-radius: 8px;
          padding: 8px;
          font-size: 13px;
        }
      </style>
      <div class="grid">
        <div class="field">
          <label for="page_size">${this._t("page_size")}</label>
          <input id="page_size" type="number" min="1" max="100" value="${pageSize}" />
        </div>
        <div class="field toggle">
          <label for="show_timeline">${this._t("show_timeline")}</label>
          <ha-switch id="show_timeline" ${showTimeline ? "checked" : ""}></ha-switch>
        </div>
        <div class="field">
          <label for="thumbnail">${this._t("thumbnails")}</label>
          <select id="thumbnail">
            <option value="object" ${thumbnail === "object" ? "selected" : ""}>${this._t("thumbnail_object")}</option>
            <option value="frame" ${thumbnail === "frame" ? "selected" : ""}>${this._t("thumbnail_frame")}</option>
          </select>
        </div>
      </div>
    `;

    this.shadowRoot.querySelector("#page_size")?.addEventListener("change", (ev) => {
      this._onNumberChange("page_size", ev.target.value, 20);
    });
    this.shadowRoot.querySelector("#show_timeline")?.addEventListener("change", (ev) => {
      this._onBooleanChange("show_timeline", ev.target.checked);
    });
    this.shadowRoot.querySelector("#thumbnail")?.addEventListener("change", (ev) => {
      this._emitConfig({ ...this._config, thumbnail: ev.target.value === "frame" ? "frame" : "object" });
    });
  }
}

if (!customElements.get("frigate-feed-card-editor")) {
  customElements.define("frigate-feed-card-editor", FrigateFeedCardEditor);
}
