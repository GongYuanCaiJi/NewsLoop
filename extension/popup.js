const DEFAULT_API_BASE = "http://localhost:8000";
let apiBase = DEFAULT_API_BASE;
let apiKey = "";

const titleEl = document.getElementById("title");
const originEl = document.getElementById("origin_url");
const tickerEl = document.getElementById("ticker");
const sentimentEl = document.getElementById("sentiment");
const confidenceEl = document.getElementById("confidence");
const confidenceLabelEl = document.getElementById("confidence_label");
const mindsetEl = document.getElementById("mindset");
const mindsetLabelEl = document.getElementById("mindset_label");
const customDateEl = document.getElementById("customDate");
const timeframeEl = document.getElementById("timeframe");
const trackModeEl = document.getElementById("track_mode");
const instrumentEl = document.getElementById("instrument");
const exposureEl = document.getElementById("exposure");
const entryEl = document.getElementById("entry");
const entryModeBadgeEl = document.getElementById("entry_mode_badge");
const tpEl = document.getElementById("tp");
const slEl = document.getElementById("sl");
const rrEl = document.getElementById("rr");
const tagButtonsEl = document.getElementById("tag_buttons");
const tagInputEl = document.getElementById("tag_input");
const tagAddEl = document.getElementById("tag_add");
const tagSelectedEl = document.getElementById("tag_selected");
const noteEl = document.getElementById("note");
const statusEl = document.getElementById("status");
const apiBaseEl = document.getElementById("api_base");
const apiBaseSaveEl = document.getElementById("api_base_save");
const apiKeyEl = document.getElementById("api_key");
const apiKeySaveEl = document.getElementById("api_key_save");
const hasSessionStorage = Boolean(chrome.storage && chrome.storage.session);
const track1TradePlanGroupEl = document.getElementById("track1_trade_plan_group");
const track2PlanGroupEl = document.getElementById("track2_plan_group");
const t2BarsLimitEl = document.getElementById("t2_bars_limit");
const t2ThresholdPctEl = document.getElementById("t2_threshold_pct");
const saveBtnEl = document.getElementById("save");

const selectedTags = new Set();
let hiddenTags = new Set();
let hiddenTagMeta = {};
let isSaving = false;
const HIDDEN_TAG_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const dynamicLabelMap = {
  title: "label_title",
  origin_url: "label_origin_url",
  ticker: "label_ticker",
  sentiment: "label_sentiment",
  date: "label_date",
  timeframe: "label_timeframe",
  track_mode: "label_track_mode",
  instrument: "label_instrument",
  exposure: "label_exposure",
  entry: "label_entry",
  tp: "label_tp",
  sl: "label_sl",
  rr: "label_rr",
  confidence: "label_confidence",
  mindset: "label_mindset",
  tags: "label_tags",
  note: "label_note",
};

const confidenceLabels = {
  1: "猜測",
  2: "小試",
  3: "穩健",
  4: "看好",
  5: "重倉",
};
const mindsetLabels = {
  1: "失衡",
  2: "緊張",
  3: "平穩",
  4: "專注",
  5: "清晰",
};

function setStatus(message, tone = "") {
  statusEl.textContent = message;
  statusEl.style.color = tone === "error" ? "#b20000" : "";
}

function setSavingState(saving) {
  isSaving = Boolean(saving);
  if (saveBtnEl) {
    saveBtnEl.disabled = isSaving;
  }
}

async function applyDynamicLabels() {
  try {
    const resp = await fetch(`${apiBase}/schema/status`, {
      headers: buildHeaders(),
    });
    if (!resp.ok) return;
    const data = await resp.json();
    const displayNames = (data && data.display_names) || {};
    Object.entries(dynamicLabelMap).forEach(([internalKey, domId]) => {
      const el = document.getElementById(domId);
      if (!el) return;
      const name = String(displayNames[internalKey] || "").trim();
      if (name) {
        el.textContent = name;
      }
    });
  } catch (_) {
    // Fallback: keep built-in static labels, do not block popup.
  }
}

function updateConfidenceLabel() {
  const value = Number(confidenceEl.value);
  confidenceLabelEl.textContent = `${value} / ${confidenceLabels[value]}`;
}

function updateMindsetLabel() {
  const value = Number(mindsetEl.value);
  mindsetLabelEl.textContent = `${value} / ${mindsetLabels[value]}`;
}

function syncTagButtonState(tag) {
  const buttons = tagButtonsEl.querySelectorAll(".tag-button");
  buttons.forEach((btn) => {
    if (btn.dataset.tag === tag) {
      btn.classList.toggle("active", selectedTags.has(tag));
    }
  });
}

function storageGet(key) {
  return new Promise((resolve) => {
    chrome.storage.local.get([key], (result) => resolve(result[key]));
  });
}

function storageSet(obj) {
  return new Promise((resolve) => {
    chrome.storage.local.set(obj, () => resolve());
  });
}

function storageRemove(key) {
  return new Promise((resolve) => {
    chrome.storage.local.remove([key], () => resolve());
  });
}

function storageSessionGet(key) {
  return new Promise((resolve) => {
    if (!hasSessionStorage) {
      resolve(undefined);
      return;
    }
    chrome.storage.session.get([key], (result) => resolve(result[key]));
  });
}

function storageSessionSet(obj) {
  return new Promise((resolve) => {
    if (!hasSessionStorage) {
      resolve();
      return;
    }
    chrome.storage.session.set(obj, () => resolve());
  });
}

function normalizeApiBase(value) {
  if (!value) return "";
  const text = String(value).trim();
  if (!text) return "";
  let parsed;
  try {
    parsed = new URL(text);
  } catch (_) {
    return "";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) return "";
  if (parsed.username || parsed.password) return "";
  const host = String(parsed.hostname || "").trim().toLowerCase();
  const isLoopback = host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  if (parsed.protocol === "http:" && !isLoopback) return "";
  return parsed.toString().replace(/\/+$/, "");
}

async function loadApiBase() {
  const stored = await storageGet("api_base");
  const normalized = normalizeApiBase(stored);
  if (!normalized && stored) {
    await storageRemove("api_base");
  }
  apiBase = normalized || DEFAULT_API_BASE;
  if (apiBaseEl) apiBaseEl.value = apiBase;
}

async function saveApiBase() {
  if (!apiBaseEl) return;
  const normalized = normalizeApiBase(apiBaseEl.value);
  if (!normalized) {
    setStatus("API 位址格式錯誤：本機可用 http，遠端必須用 https，且不可包含帳密", "error");
    apiBaseEl.value = apiBase;
    return;
  }
  apiBase = normalized;
  await storageSet({ api_base: apiBase });
  setStatus(`API 位址已更新為 ${apiBase}`);
}

async function loadApiKey() {
  if (hasSessionStorage) {
    const storedSession = await storageSessionGet("api_key");
    if (storedSession) {
      apiKey = String(storedSession || "").trim();
      if (apiKeyEl) apiKeyEl.value = apiKey;
      return;
    }
  }
  const storedLocal = await storageGet("api_key");
  apiKey = String(storedLocal || "").trim();
  if (storedLocal) {
    await storageRemove("api_key");
  }
  if (apiKey && hasSessionStorage) {
    await storageSessionSet({ api_key: apiKey });
    apiKey = String(apiKey || "").trim();
  }
  if (!hasSessionStorage) {
    apiKey = "";
  }
  if (apiKeyEl) apiKeyEl.value = apiKey;
  const noteEl = document.querySelector(".api-note");
  if (noteEl && !hasSessionStorage) {
    noteEl.textContent = "瀏覽器不支援工作階段儲存，API Key 不會被保存";
  }
}

async function saveApiKey() {
  if (!apiKeyEl) return;
  apiKey = String(apiKeyEl.value || "").trim();
  if (!hasSessionStorage) {
    setStatus("此瀏覽器不支援工作階段儲存，API Key 不會被保存", "error");
    return;
  }
  await storageSessionSet({ api_key: apiKey });
  await storageRemove("api_key");
  setStatus(apiKey ? "API Key 已更新" : "API Key 已清除");
}

function buildHeaders(includeJson = false) {
  const headers = {};
  if (includeJson) {
    headers["Content-Type"] = "application/json";
  }
  if (apiKey) {
    headers["x-api-key"] = apiKey;
  }
  return headers;
}

function renderSelectedTags() {
  tagSelectedEl.innerHTML = "";
  selectedTags.forEach((tag) => {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    const text = document.createElement("span");
    text.className = "tag-chip-text";
    text.textContent = tag;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "tag-chip-remove";
    removeBtn.setAttribute("aria-label", `移除標籤 ${tag}`);
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      selectedTags.delete(tag);
      syncTagButtonState(tag);
      renderSelectedTags();
    });
    chip.appendChild(text);
    chip.appendChild(removeBtn);
    tagSelectedEl.appendChild(chip);
  });
}

function toggleTag(tag) {
  if (selectedTags.has(tag)) {
    selectedTags.delete(tag);
  } else {
    selectedTags.add(tag);
  }
  syncTagButtonState(tag);
  renderSelectedTags();
}

async function loadTags() {
  try {
    const resp = await fetch(`${apiBase}/tags`, {
      headers: buildHeaders(),
    });
    if (!resp.ok) throw new Error("載入標籤失敗");
    const data = await resp.json();
    tagButtonsEl.innerHTML = "";
    (data.tags || []).filter((tag) => !hiddenTags.has(tag)).forEach((tag) => {
      const wrap = document.createElement("div");
      wrap.className = "tag-button-wrap";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = tag;
      btn.dataset.tag = tag;
      btn.className = "tag-button";
      btn.addEventListener("click", () => toggleTag(tag));
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "tag-button-remove";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", `刪除標籤 ${tag}`);
      removeBtn.addEventListener("click", (evt) => {
        evt.preventDefault();
        evt.stopPropagation();
        deleteTagOption(tag);
      });
      wrap.appendChild(btn);
      wrap.appendChild(removeBtn);
      tagButtonsEl.appendChild(wrap);
    });
  } catch (err) {
    setStatus("無法取得標籤，請確認後端已啟動", "error");
  }
}

function addTagFromInput() {
  const value = tagInputEl.value.trim();
  if (!value) return;
  selectedTags.add(value);
  syncTagButtonState(value);
  renderSelectedTags();
  tagInputEl.value = "";
}

async function deleteTagOption(tag) {
  try {
    const resp = await fetch(`${apiBase}/tags/delete`, {
      method: "POST",
      headers: buildHeaders(true),
      body: JSON.stringify({ name: tag }),
    });
    if (resp.ok) {
      selectedTags.delete(tag);
      syncTagButtonState(tag);
      renderSelectedTags();
      setStatus(`已刪除標籤：${tag}`);
      await loadTags();
      return;
    }
  } catch (err) {
    console.error("deleteTagOption failed", err);
    // Fall through to local hide fallback.
  }

  hiddenTags.add(tag);
  hiddenTagMeta[tag] = Date.now();
  await storageSet({
    hidden_tags: Array.from(hiddenTags),
    hidden_tags_meta: hiddenTagMeta,
  });
  selectedTags.delete(tag);
  syncTagButtonState(tag);
  renderSelectedTags();
  await loadTags();
  setStatus(`Notion 無法直接刪除，已在外掛隱藏標籤：${tag}`);
}

function clearForm() {
  titleEl.value = "";
  originEl.value = "";
  tickerEl.value = "";
  sentimentEl.value = "Bullish";
  confidenceEl.value = 3;
  mindsetEl.value = 3;
  customDateEl.value = "";
  instrumentEl.value = "";
  exposureEl.value = "";
  entryEl.value = "";
  tpEl.value = "";
  slEl.value = "";
  rrEl.value = "";
  if (timeframeEl) timeframeEl.value = "1d";
  if (trackModeEl) trackModeEl.value = "Track1";
  if (t2BarsLimitEl) t2BarsLimitEl.value = "20";
  if (t2ThresholdPctEl) t2ThresholdPctEl.value = "";
  noteEl.value = "";
  setDefaultCustomDate();
  selectedTags.clear();
  renderSelectedTags();
  updateConfidenceLabel();
  updateMindsetLabel();
  updateEntryModeBadge();
  applyTrackModeUI();
  setStatus("");
}

function toNumberOrNull(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return null;
  const num = Number(trimmed);
  return Number.isFinite(num) ? num : null;
}

function toIsoOrNullFromDatetimeLocal(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  // Parse datetime-local explicitly to avoid browser-dependent timezone parsing.
  const m = raw.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/
  );
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]) - 1;
  const day = Number(m[3]);
  const hour = Number(m[4]);
  const minute = Number(m[5]);
  const second = Number(m[6] || "0");
  const dt = new Date(year, month, day, hour, minute, second);
  if (Number.isNaN(dt.getTime())) return null;
  // Send timezone-aware timestamp so backend never guesses using server timezone.
  return dt.toISOString();
}

function recalcRR() {
  const entry = toNumberOrNull(entryEl.value);
  const tp = toNumberOrNull(tpEl.value);
  const sl = toNumberOrNull(slEl.value);
  if (entry === null || tp === null || sl === null) {
    rrEl.value = "";
    return;
  }
  const risk = Math.abs(entry - sl);
  const reward = Math.abs(tp - entry);
  if (risk === 0) {
    rrEl.value = "";
    return;
  }
  rrEl.value = (reward / risk).toFixed(2);
}

function updateEntryModeBadge() {
  if (!entryModeBadgeEl) return;
  const entry = toNumberOrNull(entryEl.value);
  const isMarket = entry === null;
  if (isMarket) {
    entryModeBadgeEl.textContent = "市價";
    entryModeBadgeEl.classList.remove("limit");
    return;
  }
  entryModeBadgeEl.textContent = "限價";
  entryModeBadgeEl.classList.add("limit");
}

function applyTrackModeUI() {
  const mode = (trackModeEl && trackModeEl.value) || "Track1";
  const isTrack2 = mode === "Track2";
  if (track1TradePlanGroupEl) {
    track1TradePlanGroupEl.classList.toggle("hidden", isTrack2);
  }
  if (track2PlanGroupEl) {
    track2PlanGroupEl.classList.toggle("hidden", !isTrack2);
  }
}

async function saveRecord() {
  if (isSaving) return;
  let closeAfterSuccess = false;
  try {
    const trackMode = (trackModeEl && trackModeEl.value) || "Track1";
    const isTrack2 = trackMode === "Track2";
    const entry = toNumberOrNull(entryEl.value);
    const orderType = entry === null ? "Market" : "Limit";
    const payload = {
      title: titleEl.value.trim(),
      origin_url: originEl.value.trim(),
      ticker: tickerEl.value.trim(),
      sentiment: sentimentEl.value,
      confidence: Number(confidenceEl.value),
      mindset: Number(mindsetEl.value),
      tags: Array.from(selectedTags),
      note: noteEl.value.trim(),
      custom_date: toIsoOrNullFromDatetimeLocal(customDateEl.value),
      timeframe: timeframeEl.value,
      order_type: orderType,
      track_mode: trackMode,
    };

    const tp = toNumberOrNull(tpEl.value);
    const sl = toNumberOrNull(slEl.value);

    if (isTrack2) {
      const t2BarsLimit = Number(String((t2BarsLimitEl && t2BarsLimitEl.value) || "").trim());
      if (!Number.isInteger(t2BarsLimit) || t2BarsLimit <= 0) {
        setStatus("請填寫有效的限制 K 棒數量（大於 0）", "error");
        return;
      }
      payload.t2_bars_limit = Math.floor(t2BarsLimit);
      const t2ThresholdPct = toNumberOrNull((t2ThresholdPctEl && t2ThresholdPctEl.value) || "");
      if (t2ThresholdPct !== null) payload.t2_threshold_pct = t2ThresholdPct;
      const snapTs = toIsoOrNullFromDatetimeLocal(customDateEl.value);
      if (!snapTs) {
        setStatus("Track2 模式需要填寫進場日期時間", "error");
        return;
      }
      payload.t2_entry_time = snapTs;
      if (entry !== null) payload.t2_entry_price = entry;
    } else {
      const instrument = instrumentEl.value.trim();
      if (instrument) payload.instrument = instrument;
      const exposure = toNumberOrNull(exposureEl.value);
      if (exposure !== null) payload.exposure = exposure;
      if (entry !== null) payload.entry = entry;
      if (tp !== null) payload.tp = tp;
      if (sl !== null) payload.sl = sl;
      const rr = toNumberOrNull(rrEl.value);
      if (rr !== null) payload.rr = rr;
    }

    if (!payload.title || !payload.origin_url || !payload.ticker) {
      setStatus("請填寫標題、網址與代碼", "error");
      return;
    }

    // Strong front-end validation to prevent invalid TP/SL direction.
    const sentiment = sentimentEl.value;
    const isDirectional = sentiment === "Bullish" || sentiment === "Bearish";
    // Neutral/other sentiment records are allowed, but we do not enforce TP/SL ordering on them.
    if (!isTrack2 && tp !== null && sl !== null) {
      if (isDirectional && sentiment === "Bullish" && tp <= sl) {
        setStatus("⚠️ [錯誤] 看漲單：止盈必須大於止損", "error");
        return;
      }
      if (isDirectional && sentiment === "Bearish" && tp >= sl) {
        setStatus("⚠️ [錯誤] 看跌單：止盈必須小於止損", "error");
        return;
      }
    }
    if (!isTrack2 && entry !== null && tp !== null) {
      if (isDirectional && sentiment === "Bullish" && !(tp > entry)) {
        setStatus("⚠️ [錯誤] 看漲單：止盈必須大於進場", "error");
        return;
      }
      if (isDirectional && sentiment === "Bearish" && !(tp < entry)) {
        setStatus("⚠️ [錯誤] 看跌單：止盈必須小於進場", "error");
        return;
      }
    }
    if (!isTrack2 && entry !== null && sl !== null) {
      if (isDirectional && sentiment === "Bullish" && !(entry > sl)) {
        setStatus("⚠️ [錯誤] 看漲單：止損必須小於進場", "error");
        return;
      }
      if (isDirectional && sentiment === "Bearish" && !(entry < sl)) {
        setStatus("⚠️ [錯誤] 看跌單：止損必須大於進場", "error");
        return;
      }
    }

    setSavingState(true);
    setStatus("儲存中...");
    const resp = await fetch(`${apiBase}/add_news`, {
      method: "POST",
      headers: buildHeaders(true),
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      let detail = "";
      try {
        const errJson = await resp.json();
        detail = String((errJson && errJson.detail) || "").trim();
      } catch (_) {
        detail = "";
      }
      throw new Error(`儲存失敗 [${resp.status}]${detail ? `: ${detail}` : ""}`);
    }
    setStatus("已儲存！視窗即將關閉");
    closeAfterSuccess = true;
    setTimeout(() => window.close(), 900);
  } catch (err) {
    const msg = (err && err.message) ? err.message : "儲存失敗，請確認後端與 Notion 設定";
    setStatus(msg, "error");
  } finally {
    if (!closeAfterSuccess) {
      setSavingState(false);
    }
  }
}

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs[0];
  if (tab) {
    if (titleEl) titleEl.value = tab.title || "";
    if (originEl) originEl.value = tab.url || "";
  }
});

function setDefaultCustomDate() {
  const now = new Date();
  const offsetMs = now.getTimezoneOffset() * 60000;
  const local = new Date(now.getTime() - offsetMs);
  customDateEl.value = local.toISOString().slice(0, 16);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setDefaultCustomDate);
} else {
  setDefaultCustomDate();
}

confidenceEl.addEventListener("input", updateConfidenceLabel);
mindsetEl.addEventListener("input", updateMindsetLabel);
entryEl.addEventListener("input", recalcRR);
entryEl.addEventListener("input", updateEntryModeBadge);
tpEl.addEventListener("input", recalcRR);
slEl.addEventListener("input", recalcRR);
if (trackModeEl) trackModeEl.addEventListener("change", applyTrackModeUI);
updateConfidenceLabel();
updateMindsetLabel();
updateEntryModeBadge();
applyTrackModeUI();
(async () => {
  await loadApiBase();
  await loadApiKey();
  await applyDynamicLabels();
  const [stored, storedMeta] = await Promise.all([
    storageGet("hidden_tags"),
    storageGet("hidden_tags_meta"),
  ]);
  const now = Date.now();
  hiddenTagMeta = (storedMeta && typeof storedMeta === "object") ? { ...storedMeta } : {};
  if (Array.isArray(stored)) {
    const nextHidden = [];
    for (const tag of stored) {
      const ts = Number(hiddenTagMeta[tag] || 0);
      if (!ts) {
        // Legacy entries (before meta timestamp existed): drop immediately to avoid "phantom hidden" tags.
        delete hiddenTagMeta[tag];
        continue;
      }
      if ((now - ts) < HIDDEN_TAG_TTL_MS) {
        nextHidden.push(tag);
      } else {
        delete hiddenTagMeta[tag];
      }
    }
    hiddenTags = new Set(nextHidden);
    const nextMeta = {};
    hiddenTags.forEach((tag) => {
      nextMeta[tag] = Number(hiddenTagMeta[tag] || now);
    });
    hiddenTagMeta = nextMeta;
    await storageSet({
      hidden_tags: Array.from(hiddenTags),
      hidden_tags_meta: hiddenTagMeta,
    });
  }
  await loadTags();
})();

noteEl.addEventListener("keydown", (evt) => {
  if (evt.key === "Enter" && (evt.metaKey || evt.ctrlKey)) {
    saveRecord();
  }
});

tagAddEl.addEventListener("click", addTagFromInput);
tagInputEl.addEventListener("keydown", (evt) => {
  if (evt.key === "Enter") {
    evt.preventDefault();
    addTagFromInput();
  }
});

if (saveBtnEl) saveBtnEl.addEventListener("click", saveRecord);
document.getElementById("clear").addEventListener("click", clearForm);

if (apiBaseSaveEl) apiBaseSaveEl.addEventListener("click", saveApiBase);
if (apiBaseEl) {
  apiBaseEl.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter") {
      evt.preventDefault();
      saveApiBase();
    }
  });
}
if (apiKeySaveEl) apiKeySaveEl.addEventListener("click", saveApiKey);
if (apiKeyEl) {
  apiKeyEl.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter") {
      evt.preventDefault();
      saveApiKey();
    }
  });
}
