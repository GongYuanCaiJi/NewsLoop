const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const test = require("node:test");
const vm = require("node:vm");

process.env.TZ = "UTC";

const POPUP_SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "extension", "popup.js"),
  "utf8",
);

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : Boolean(force);
    if (enabled) this.values.add(value);
    else this.values.delete(value);
    return enabled;
  }
}

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.disabled = false;
    this.style = { color: "" };
    this.dataset = {};
    this.classList = new FakeClassList();
    this.listeners = new Map();
    this.children = [];
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  querySelectorAll() {
    return [];
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  async emit(type, event = {}) {
    const normalizedEvent = {
      preventDefault() {},
      stopPropagation() {},
      ...event,
    };
    const results = (this.listeners.get(type) || []).map((listener) =>
      listener(normalizedEvent),
    );
    await Promise.all(results.map((result) => Promise.resolve(result)));
  }
}

function response(ok, status, body) {
  return {
    ok,
    status,
    async json() {
      return body;
    },
  };
}

async function createPopupHarness({ captureResponse, captureFetch, apiBase }) {
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement(id));
    return elements.get(id);
  };
  const document = {
    readyState: "complete",
    getElementById: getElement,
    createElement: () => new FakeElement(),
    querySelector: () => null,
    addEventListener() {},
  };

  const localStorage = apiBase ? { api_base: apiBase } : {};
  const sessionStorage = {};
  const makeStorage = (store) => ({
    get(keys, callback) {
      const result = {};
      for (const key of keys) result[key] = store[key];
      callback(result);
    },
    set(values, callback) {
      Object.assign(store, values);
      callback();
    },
    remove(keys, callback) {
      for (const key of keys) delete store[key];
      callback();
    },
  });

  let captureRequest = null;
  const fetch = async (url, options = {}) => {
    if (url.endsWith("/schema/status")) {
      return response(true, 200, { display_names: {} });
    }
    if (url.endsWith("/tags")) {
      return response(true, 200, { tags: [] });
    }
    if (url.endsWith("/add_news")) {
      captureRequest = {
        url,
        method: options.method,
        headers: { ...(options.headers || {}) },
        payload: JSON.parse(options.body),
        saveDisabledAtFetch: getElement("save").disabled,
      };
      return captureFetch ? captureFetch(url, options) : captureResponse;
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  const state = { closeCount: 0, timeoutDelays: [] };
  const context = vm.createContext({
    URL,
    Date,
    Number,
    Promise,
    Set,
    String,
    Array,
    Object,
    JSON,
    console: { error() {}, log() {}, warn() {} },
    document,
    fetch,
    chrome: {
      storage: {
        local: makeStorage(localStorage),
        session: makeStorage(sessionStorage),
      },
      tabs: {
        query(_query, callback) {
          callback([{ title: "Current tab", url: "https://example.com/current" }]);
        },
      },
    },
    window: {
      close() {
        state.closeCount += 1;
      },
    },
    setTimeout(callback, delay) {
      state.timeoutDelays.push(delay);
      callback();
      return state.timeoutDelays.length;
    },
  });

  vm.runInContext(POPUP_SOURCE, context, { filename: "extension/popup.js" });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  return {
    element: getElement,
    state,
    captureRequest: () => captureRequest,
  };
}

async function waitForJson(filePath, child, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`capture server exited early with code ${child.exitCode}`);
    }
    try {
      return JSON.parse(fs.readFileSync(filePath, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT" && !(error instanceof SyntaxError)) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timed out waiting for ${filePath}`);
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => {
    const killTimer = setTimeout(() => {
      if (child.exitCode === null) child.kill("SIGKILL");
    }, 2000);
    child.once("exit", () => {
      clearTimeout(killTimer);
      resolve();
    });
  });
}

function fillTrack1Capture(harness) {
  const values = {
    title: " Earnings beat ",
    origin_url: "https://example.com/story",
    ticker: " aapl ",
    sentiment: "Bullish",
    confidence: "4",
    mindset: "3",
    customDate: "2026-07-22T01:30",
    timeframe: "1d",
    track_mode: "Track1",
    instrument: "Stock",
    exposure: "25",
    entry: "200",
    tp: "220",
    sl: "190",
    rr: "2",
    note: "watch guidance",
  };
  for (const [id, value] of Object.entries(values)) {
    harness.element(id).value = value;
  }
}

function fillTrack2Capture(harness, { bars = "20", threshold = "" } = {}) {
  fillTrack1Capture(harness);
  harness.element("track_mode").value = "Track2";
  harness.element("t2_bars_limit").value = bars;
  harness.element("t2_threshold_pct").value = threshold;
}

function expectedTrack1Capture(apiBase = "http://localhost:8000") {
  return {
    url: `${apiBase}/add_news`,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    payload: {
      title: "Earnings beat",
      origin_url: "https://example.com/story",
      ticker: "aapl",
      sentiment: "Bullish",
      confidence: 4,
      mindset: 3,
      tags: ["Earnings"],
      note: "watch guidance",
      custom_date: "2026-07-22T01:30:00.000Z",
      timeframe: "1d",
      order_type: "Limit",
      track_mode: "Track1",
      instrument: "Stock",
      exposure: 25,
      entry: 200,
      tp: 220,
      sl: 190,
      rr: 2,
    },
    saveDisabledAtFetch: true,
  };
}

test("successful save preserves payload, saving state, UI text and close behavior", async () => {
  const harness = await createPopupHarness({
    captureResponse: response(true, 200, { ok: true }),
  });
  fillTrack1Capture(harness);
  harness.element("tag_input").value = "Earnings";
  await harness.element("tag_add").emit("click");

  await harness.element("save").emit("click");

  assert.deepEqual(harness.captureRequest(), expectedTrack1Capture());
  assert.equal(harness.element("status").textContent, "已儲存！視窗即將關閉");
  assert.equal(harness.element("save").disabled, true);
  assert.deepEqual(harness.state.timeoutDelays, [900]);
  assert.equal(harness.state.closeCount, 1);
});

test("failed save preserves backend detail and restores save interaction", async () => {
  const harness = await createPopupHarness({
    captureResponse: response(false, 400, { detail: "origin_url 必須以 http/https 開頭" }),
  });
  fillTrack1Capture(harness);

  await harness.element("save").emit("click");

  assert.equal(
    harness.element("status").textContent,
    "儲存失敗 [400]: origin_url 必須以 http/https 開頭",
  );
  assert.equal(harness.element("status").style.color, "#b20000");
  assert.equal(harness.element("save").disabled, false);
  assert.deepEqual(harness.state.timeoutDelays, []);
  assert.equal(harness.state.closeCount, 0);
});

test("Track2 rejects fractional bar limits before sending a capture", async () => {
  const harness = await createPopupHarness({
    captureResponse: response(true, 200, { ok: true }),
  });
  fillTrack2Capture(harness, { bars: "0.5" });

  await harness.element("save").emit("click");

  assert.equal(harness.captureRequest(), null);
  assert.equal(
    harness.element("status").textContent,
    "請填寫有效的限制 K 棒數量（大於 0）",
  );
  assert.equal(harness.element("save").disabled, false);
});

test("popup capture crosses real localhost HTTP and writes one exact Trading Thesis", async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "newsloop-capture-e2e-"));
  const readyPath = path.join(tempDirectory, "ready.json");
  const outputPath = path.join(tempDirectory, "capture.json");
  const pythonCandidate = process.env.NEWSLOOP_TEST_PYTHON || process.env.PYTHON || "python3";
  const python = path.isAbsolute(pythonCandidate) || !pythonCandidate.includes(path.sep)
    ? pythonCandidate
    : path.resolve(path.join(__dirname, ".."), pythonCandidate);
  const server = spawn(
    python,
    [path.join(__dirname, "support", "capture_e2e_server.py")],
    {
      cwd: tempDirectory,
      env: {
        PATH: process.env.PATH || "",
        HOME: tempDirectory,
        TMPDIR: tempDirectory,
        LANG: "C.UTF-8",
        PYTHON_DOTENV_DISABLED: "1",
        NOTION_TOKEN: "",
        NEWS_ALPHA_DB_ID: "",
        NEWS_ALPHA_API_KEY: "",
        NEWS_ALPHA_MAX_BODY_BYTES: "1048576",
        NEWS_ALPHA_RATE_LIMIT_MAX: "120",
        NEWSLOOP_CAPTURE_E2E_CONFIG: path.join(tempDirectory, "dashboard_config.json"),
        NEWSLOOP_CAPTURE_E2E_READY: readyPath,
        NEWSLOOP_CAPTURE_E2E_OUTPUT: outputPath,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stderr = "";
  server.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  try {
    const { port } = await waitForJson(readyPath, server);
    const apiBase = `http://127.0.0.1:${port}`;
    const harness = await createPopupHarness({
      apiBase,
      captureFetch: globalThis.fetch,
    });
    fillTrack1Capture(harness);
    harness.element("tag_input").value = "Earnings";
    await harness.element("tag_add").emit("click");

    await harness.element("save").emit("click");

    assert.deepEqual(harness.captureRequest(), expectedTrack1Capture(apiBase));
    assert.equal(harness.element("status").textContent, "已儲存！視窗即將關閉");
    const recorded = await waitForJson(outputPath, server);
    assert.deepEqual(recorded, {
      write_count: 1,
      writes: [
        {
          parent: { database_id: "test-database" },
          properties: {
            title: "Earnings beat",
            ticker: "AAPL",
            sentiment: "Bullish",
            order_type: "Limit",
            confidence: 4,
            tags: ["Earnings"],
            note: "watch guidance",
            origin_url: "https://example.com/story",
            date: "2026-07-22T01:30:00+00:00",
            timeframe: "1d",
            track_mode: "Track1",
            instrument: "Stock",
            mindset: 3,
            result_auto: "Pending",
            exposure: 25,
            entry: 200,
            tp: 220,
            sl: 190,
            rr: 2,
          },
        },
      ],
    });
  } catch (error) {
    error.message = `${error.message}\nserver stderr:\n${stderr}`;
    throw error;
  } finally {
    await stopChild(server);
    fs.rmSync(tempDirectory, { recursive: true, force: true });
  }
});
