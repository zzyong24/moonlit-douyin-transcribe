#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm } from "node:fs/promises";
import { constants } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const videoId = process.argv[2] ?? "";
const explicitChromePath = process.argv[3] ?? "";

if (typeof WebSocket !== "function") {
  console.error("浏览器兜底需要 Node.js 22+（当前运行时缺少 WebSocket）");
  process.exit(2);
}

if (!/^\d+$/.test(videoId)) {
  console.error("video_id 必须是纯数字");
  process.exit(2);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function executableExists(path) {
  if (!path) return false;
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

async function resolveChromePath() {
  const candidates = [
    explicitChromePath,
    process.env.CHROME_PATH,
    process.platform === "darwin"
      ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      : "",
    process.platform === "darwin"
      ? "/Applications/Chromium.app/Contents/MacOS/Chromium"
      : "",
    process.platform === "win32"
      ? `${process.env.PROGRAMFILES ?? "C:\\Program Files"}\\Google\\Chrome\\Application\\chrome.exe`
      : "",
    process.platform === "win32"
      ? `${process.env["PROGRAMFILES(X86)"] ?? "C:\\Program Files (x86)"}\\Google\\Chrome\\Application\\chrome.exe`
      : "",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (await executableExists(candidate)) return candidate;
  }
  throw new Error(
    "未找到 Chrome/Chromium；可用 --chrome-path 或 CHROME_PATH 指定可执行文件",
  );
}

async function waitForDevToolsPort(profileDir, child, timeoutMs = 20_000) {
  const portFile = join(profileDir, "DevToolsActivePort");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Chrome 提前退出(exit=${child.exitCode})`);
    }
    try {
      const lines = (await readFile(portFile, "utf8")).trim().split(/\r?\n/);
      const port = Number(lines[0]);
      if (Number.isInteger(port) && port > 0) return port;
    } catch {
      // Chrome 尚未写好 DevToolsActivePort。
    }
    await sleep(100);
  }
  throw new Error("等待 Chrome DevTools 启动超时");
}

class CdpClient {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.eventWaiters = new Map();
    this.socket = new WebSocket(url);
    this.opened = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener(
        "error",
        () => reject(new Error("无法连接 Chrome DevTools WebSocket")),
        { once: true },
      );
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (typeof message.id === "number") {
        const entry = this.pending.get(message.id);
        if (!entry) return;
        this.pending.delete(message.id);
        clearTimeout(entry.timer);
        if (message.error) {
          entry.reject(new Error(message.error.message ?? "CDP 调用失败"));
        } else {
          entry.resolve(message.result ?? {});
        }
        return;
      }
      const waiters = this.eventWaiters.get(message.method) ?? [];
      this.eventWaiters.delete(message.method);
      for (const waiter of waiters) {
        clearTimeout(waiter.timer);
        waiter.resolve(message.params ?? {});
      }
    });
  }

  async send(method, params = {}, timeoutMs = 30_000) {
    await this.opened;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} 超时`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  waitForEvent(method, timeoutMs = 60_000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const current = this.eventWaiters.get(method) ?? [];
        this.eventWaiters.set(
          method,
          current.filter((entry) => entry.resolve !== resolve),
        );
        reject(new Error(`等待 ${method} 超时`));
      }, timeoutMs);
      const current = this.eventWaiters.get(method) ?? [];
      current.push({ resolve, reject, timer });
      this.eventWaiters.set(method, current);
    });
  }

  close() {
    this.socket.close();
  }
}

function evaluationExpression(id) {
  return `
    (async () => {
      const videoId = ${JSON.stringify(id)};
      const params = new URLSearchParams({
        device_platform: "webapp",
        aid: "6383",
        channel: "channel_pc_web",
        pc_client_type: "1",
        version_code: "170400",
        version_name: "17.4.0",
        cookie_enabled: "true",
        screen_width: "1920",
        screen_height: "1080",
        browser_language: "zh-CN",
        browser_platform: "Win32",
        browser_name: "Chrome",
        browser_version: "126.0.0.0",
        browser_online: "true",
        platform: "PC",
        aweme_id: videoId,
      });
      const response = await fetch(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + params,
        { credentials: "include" },
      );
      const body = await response.text();
      if (!response.ok) throw new Error("detail HTTP " + response.status);
      if (!body) throw new Error("detail 接口返回空正文");
      const root = JSON.parse(body);
      const item = root && root.aweme_detail;
      if (!item) throw new Error("detail 接口未返回 aweme_detail");
      if (Array.isArray(item.images) && item.images.length > 0) {
        throw new Error("该作品是图集，不包含可转写的视频音轨");
      }
      const video = item.video || {};
      const playUrls = (video.play_addr && video.play_addr.url_list) || [];
      const downloadUrls = (video.download_addr && video.download_addr.url_list) || [];
      const urls = playUrls.length ? playUrls : downloadUrls;
      if (!urls.length) throw new Error("detail 接口未返回播放地址");
      const author = item.author || item.user || {};
      const cover = video.cover || video.origin_cover || {};
      return {
        video_id: String(item.aweme_id || videoId),
        title: String(item.desc || item.title || ("douyin_" + videoId)).trim(),
        author: String(author.nickname || author.name || "").trim(),
        duration_ms: Number(video.duration || item.duration || 0),
        play_addr_src: playUrls.length ? "browser_detail/play_addr" : "browser_detail/download_addr",
        direct_url: String(urls[0]).replaceAll("playwm", "play"),
        cover_url: Array.isArray(cover.url_list) ? (cover.url_list[0] || null) : null,
        aweme_id: String(item.aweme_id || videoId),
        fetch_chain: "douyin.com/browser-detail",
      };
    })()
  `;
}

const profileDir = await mkdtemp(join(tmpdir(), "douyin-browser-detail-"));
let child;
let client;

try {
  const chromePath = await resolveChromePath();
  child = spawn(
    chromePath,
    [
      "--headless=new",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--disable-blink-features=AutomationControlled",
      "--no-first-run",
      "--no-default-browser-check",
      "--no-proxy-server",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDir}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "ignore"] },
  );
  const port = await waitForDevToolsPort(profileDir, child);
  const tabResponse = await fetch(
    `http://127.0.0.1:${port}/json/new?${encodeURIComponent(`https://www.douyin.com/video/${videoId}`)}`,
    { method: "PUT" },
  );
  if (!tabResponse.ok) throw new Error(`创建浏览器标签页失败: HTTP ${tabResponse.status}`);
  const tab = await tabResponse.json();
  if (!tab.webSocketDebuggerUrl) throw new Error("浏览器未返回 DevTools WebSocket 地址");

  client = new CdpClient(tab.webSocketDebuggerUrl);
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  const loaded = client.waitForEvent("Page.loadEventFired").catch(() => null);
  await client.send("Page.navigate", {
    url: `https://www.douyin.com/video/${videoId}`,
  });
  await loaded;
  await sleep(2_500);
  const evaluation = await client.send(
    "Runtime.evaluate",
    {
      expression: evaluationExpression(videoId),
      awaitPromise: true,
      returnByValue: true,
    },
    45_000,
  );
  if (evaluation.exceptionDetails) {
    const description =
      evaluation.result?.description ??
      evaluation.exceptionDetails.exception?.description ??
      evaluation.exceptionDetails.text ??
      "浏览器 detail 解析失败";
    throw new Error(String(description).split("\n")[0]);
  }
  const value = evaluation.result?.value;
  if (!value || typeof value !== "object" || !value.direct_url) {
    throw new Error("浏览器 detail 未返回有效播放地址");
  }
  process.stdout.write(`${JSON.stringify(value)}\n`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  try {
    if (client) await client.send("Browser.close", {}, 3_000);
  } catch {
    // 继续兜底终止临时浏览器。
  }
  client?.close();
  if (child && child.exitCode === null) child.kill("SIGTERM");
  await rm(profileDir, { recursive: true, force: true }).catch(() => {});
}
