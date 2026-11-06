import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const fallbackPlaywright = fileURLToPath(
  new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url),
);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fallbackPlaywright);
const { server, url } = await startServer();
const evidenceUrl = new URL("./evidence/", import.meta.url);
await mkdir(evidenceUrl, { recursive: true });

const browser = await chromium.launch({ headless: true });
const productStates = ["disabled", "healthy", "cooling", "probing", "recovering", "needs_attention"];
const expectedState = {
  disabled: ["已关闭", "功能关闭；文字判定失败继续按现有规则静默"],
  healthy: ["正常", "文字判定依赖当前正常"],
  cooling: ["冷却中", "已暂停普通文字判定调用"],
  probing: ["探测中", "冷却结束，正在检查文字判定依赖"],
  recovering: ["恢复中", "依赖已恢复，正在补读受影响会话"],
  needs_attention: ["需处理", "认证、配置或 Provider 需要管理员处理"],
};
const expectedLinks = ["回复方式", "原生分段", "回复节奏", "撤回保护", "称呼私聊", "内容理解", "断线恢复"];
const report = {
  environment: {
    os: `${os.platform()} ${os.release()}`,
    arch: os.arch(),
    node: process.version,
    browser: browser.version(),
  },
  scope: "隔离合成 fixture；无生产请求、无真实模型、无部署",
  source_sha256: {},
  cases: [],
  matrix: [],
  outageStates: [],
  trace: {},
  draft: {},
  consoleErrors: [],
  pageErrors: [],
  externalRequests: [],
  screenshots: [],
};
for (const name of ["pages/guard/index.html", "guard_webui.py", "guard_classifier_outage.py"]) {
  const source = await readFile(new URL(`../../${name}`, import.meta.url));
  report.source_sha256[name] = createHash("sha256").update(source).digest("hex");
}

async function caseRun(name, task) {
  try {
    await task();
    report.cases.push({ name, passed: true });
    console.log(`通过：${name}`);
  } catch (error) {
    report.cases.push({ name, passed: false, error: error.stack || error.message });
    console.error(`失败：${name}：${error.message}`);
  }
}

async function open(options = {}, suffix = "") {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    colorScheme: "light",
    reducedMotion: "reduce",
    ...options,
  });
  const origin = new URL(url).origin;
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin });
  const page = await context.newPage();
  page.on("pageerror", error => report.pageErrors.push({ suffix, message: error.message }));
  page.on("console", message => {
    if (message.type() === "error") report.consoleErrors.push({ suffix, message: message.text() });
  });
  await page.route("**/*", route => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.origin === origin) return route.continue();
    report.externalRequests.push(route.request().url());
    return route.abort();
  });
  await page.goto(url + suffix, { waitUntil: "domcontentloaded" });
  return { context, page };
}

async function ready(page) {
  await page.locator("#ver").filter({ hasText: "fixture-task9" }).waitFor();
  await page.locator("#nativeStatus").filter({ hasText: "已读取AstrBot原生配置" })
    .waitFor({ state: "attached" });
}

async function shot(page, name) {
  const output = fileURLToPath(new URL(name, evidenceUrl));
  await page.screenshot({ path: output, fullPage: false });
  report.screenshots.push({ name, viewport: await page.evaluate(() => ({ width: innerWidth, height: innerHeight })) });
}

function matrixMetrics() {
  const pairOverlaps = selector => {
    const rects = [...document.querySelectorAll(selector)]
      .filter(node => node.checkVisibility({ checkVisibilityCSS: true }))
      .map(node => {
        const rect = node.getBoundingClientRect();
        return { text: node.textContent.trim(), left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      });
    const overlaps = [];
    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        const a = rects[i], b = rects[j];
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5) {
          overlaps.push([a.text, b.text]);
        }
      }
    }
    return overlaps;
  };
  const sectionLinks = [...document.querySelectorAll("#panel-reply .section-links a")]
    .map(node => node.textContent.trim());
  return {
    width: innerWidth,
    height: innerHeight,
    theme: document.documentElement.dataset.theme,
    scrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    sectionLinks,
    tabCount: document.querySelectorAll(".page-tabs [role=tab]").length,
    replyVisible: !document.querySelector("#panel-reply").hidden,
    outageVisible: document.querySelector("#outageRecovery").checkVisibility({ checkVisibilityCSS: true }),
    globalOverflow: document.documentElement.scrollWidth > innerWidth || document.body.scrollWidth > innerWidth,
    sectionLinkOverlaps: pairOverlaps("#panel-reply .section-links a"),
    outageCardOverlaps: pairOverlaps(".outage-status-item"),
    tabOverlaps: pairOverlaps(".page-tabs button"),
    outageColumns: getComputedStyle(document.querySelector(".outage-status-grid")).gridTemplateColumns.split(" ").length,
  };
}

function contrastRatio(foreground, background) {
  const parse = value => value.match(/[\d.]+/g).slice(0, 3).map(Number);
  const luminance = value => parse(value).map(channel => channel / 255)
    .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4)
    .reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

try {
  await caseRun("320/390/1280 × 浅深主题：七按钮、同页跳转、全恢复状态、无全局溢出", async () => {
    for (const width of [320, 390, 1280]) {
      for (const theme of ["light", "dark"]) {
        const height = width === 320 ? 720 : width === 390 ? 844 : 900;
        const { context, page } = await open({ viewport: { width, height }, colorScheme: theme }, "?outage=healthy");
        await ready(page);
        assert.equal(await page.locator("html").getAttribute("data-theme"), theme);
        for (const state of productStates) {
          await page.goto(`${url}?outage=${state}`, { waitUntil: "domcontentloaded" });
          await ready(page);
          const observed = {
            width,
            height,
            theme,
            state,
            badge: (await page.locator("#outageStatusBadge").textContent()).trim(),
            description: (await page.locator("#outageState").textContent()).trim(),
            className: await page.locator("#outageStatusBadge").getAttribute("class"),
            cooldown: (await page.locator("#outageCooldown").textContent()).trim(),
          };
          report.outageStates.push(observed);
          assert.deepEqual([observed.badge, observed.description], expectedState[state]);
          if (state === "cooling") assert.match(observed.cooldown, /剩余 10 分钟 · 固定 10 分钟/);
        }
        await page.goto(`${url}?outage=healthy`, { waitUntil: "domcontentloaded" });
        await ready(page);
        await page.getByRole("tab", { name: "回复与私聊" }).click();
        const beforePath = new URL(page.url()).pathname;
        const links = page.locator("#panel-reply .section-links a");
        assert.deepEqual(await links.allTextContents(), expectedLinks);
        await links.filter({ hasText: "断线恢复" }).click();
        assert.equal(new URL(page.url()).pathname, beforePath);
        assert.equal(new URL(page.url()).hash, "#outageRecovery");
        const metrics = await page.evaluate(matrixMetrics);
        report.matrix.push(metrics);
        assert.equal(metrics.tabCount, 4);
        assert.deepEqual(metrics.sectionLinks, expectedLinks);
        assert.equal(metrics.replyVisible, true);
        assert.equal(metrics.outageVisible, true);
        assert.equal(metrics.globalOverflow, false);
        assert.deepEqual(metrics.sectionLinkOverlaps, []);
        assert.deepEqual(metrics.outageCardOverlaps, []);
        assert.deepEqual(metrics.tabOverlaps, []);
        await shot(page, `${width}-${theme}-outage.png`);
        await context.close();
      }
    }
    assert.equal(report.outageStates.length, 36);
  });

  await caseRun("加载与读取错误状态有明确降级", async () => {
    for (const theme of ["light", "dark"]) {
      const loading = await open({ viewport: { width: 390, height: 844 }, colorScheme: theme }, "?outage=loading");
      assert.equal((await loading.page.locator("#outageStatusBadge").textContent()).trim(), "加载中");
      await ready(loading.page);
      assert.equal((await loading.page.locator("#outageStatusBadge").textContent()).trim(), "正常");
      await loading.context.close();
      const error = await open({ viewport: { width: 390, height: 844 }, colorScheme: theme }, "?outage=error");
      await ready(error.page);
      assert.equal((await error.page.locator("#outageStatusBadge").textContent()).trim(), "读取失败");
      assert.match(await error.page.locator("#outageState").textContent(), /暂时无法读取/);
      await error.context.close();
    }
  });

  await caseRun("详情：监听/执行紧凑四色、主要节点健康色与有效静默", async () => {
    const { context, page } = await open({ viewport: { width: 390, height: 844 }, colorScheme: "light" }, "?outage=healthy");
    await ready(page);
    await page.locator(".decision-action").first().click();
    await page.locator("#rawModal.show").waitFor();
    await page.locator(".request-flow li").nth(4).waitFor();
    const nodes = await page.locator(".request-flow li").evaluateAll(items => items.map(item => ({
      name: item.querySelector("b").textContent,
      status: item.querySelector(".flow-status").textContent,
      tone: item.className,
      color: getComputedStyle(item.querySelector(".flow-status")).color,
      noteLines: Math.round(item.querySelector(".request-flow-note").getBoundingClientRect().height
        / parseFloat(getComputedStyle(item.querySelector(".request-flow-note")).lineHeight)),
    })));
    assert.equal(nodes.length, 5);
    assert.deepEqual(nodes.map(item => item.name), ["接收消息", "判断是否参与", "生成回复", "执行工具", "发送结果"]);
    assert.equal(nodes[1].status, "完成 · 静默");
    assert.match(nodes[1].tone, /trace-tone-normal/);
    assert.match(nodes[3].tone, /trace-tone-abnormal/);
    assert.ok(nodes.every(item => item.noteLines <= 1));
    await page.getByText("展开请求标识、监听与功能明细", { exact: true }).click();
    const compact = await page.evaluate(() => {
      const read = selector => [...document.querySelectorAll(selector)].map(node => ({
        name: node.querySelector("b").textContent,
        text: node.querySelector("small").textContent,
        tone: node.className,
        color: getComputedStyle(node.querySelector("small")).color,
        background: getComputedStyle(node).backgroundColor,
      }));
      return {
        coverage: read(".trace-grid .trace-compact-cell"),
        features: read(".feature-grid .trace-compact-cell"),
        coverageDisplay: getComputedStyle(document.querySelector(".trace-grid")).display,
        featureDisplay: getComputedStyle(document.querySelector(".feature-grid")).display,
        coverageColumns: getComputedStyle(document.querySelector(".trace-grid")).gridTemplateColumns.split(" ").length,
        featureColumns: getComputedStyle(document.querySelector(".feature-grid")).gridTemplateColumns.split(" ").length,
      };
    });
    report.trace = { nodes, ...compact };
    assert.equal(compact.coverageDisplay, "grid");
    assert.equal(compact.featureDisplay, "grid");
    assert.ok(compact.coverageColumns >= 2);
    assert.ok(compact.featureColumns >= 2);
    assert.deepEqual(compact.coverage.map(item => item.text), ["正常", "正常", "未注册", "宿主不支持", "无法核实"]);
    assert.ok(compact.features.every(item => item.text.split(" · ").length <= 4));
    assert.ok(compact.features.some(item => item.text.endsWith("…")));
    const tones = new Set([...compact.coverage, ...compact.features].map(item => item.tone.match(/trace-tone-\w+/)?.[0]));
    assert.deepEqual([...tones].sort(), [
      "trace-tone-abnormal", "trace-tone-neutral", "trace-tone-normal", "trace-tone-observe",
    ]);
    for (const item of [...compact.coverage, ...compact.features]) {
      const ratio = contrastRatio(item.color, item.background);
      assert.ok(ratio >= 4.5, `${item.name} 状态文字对比度仅 ${ratio.toFixed(2)}`);
    }
    await shot(page, "390-light-trace-details.png");
    await context.close();
  });

  await caseRun("三段式原始输入、复制仅中间内容并可粘贴到文字试跑", async () => {
    const { context, page } = await open({ viewport: { width: 1280, height: 900 }, colorScheme: "dark" }, "?outage=healthy");
    await ready(page);
    await page.locator(".decision-action").first().click();
    await page.locator("#rawModal.show").waitFor();
    await page.locator(".record-disclosure > summary").click();
    const headings = await page.locator(".classifier-input-part h5").allTextContents();
    assert.deepEqual(headings, ["开头规则", "中间输入", "结尾要求"]);
    assert.equal(await page.locator(".model-raw-output").evaluate((raw, input) => input.contains(raw), await page.locator(".classifier-input").elementHandle()), false);
    const parts = await page.locator(".classifier-input-part pre").allTextContents();
    assert.match(parts[0], /OPENING-ONLY/);
    assert.match(parts[1], /MIDDLE-ONLY/);
    assert.match(parts[2], /ENDING-ONLY/);
    await page.getByRole("button", { name: "复制复验内容" }).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const expected = await page.evaluate(() => window.__fixture.middleInput);
    assert.equal(copied, expected);
    assert.doesNotMatch(copied, /OPENING-ONLY|ENDING-ONLY/);
    await shot(page, "1280-dark-structured-input.png");
    await page.keyboard.press("Escape");
    await page.getByRole("tab", { name: "流程与试跑" }).click();
    await page.locator("#probeText").focus();
    await page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
    assert.equal(await page.locator("#probeText").inputValue(), expected);
    assert.equal(await page.evaluate(() => window.__fixture.modelCalls), 0);
    assert.equal(await page.evaluate(() => window.__fixture.calls.filter(call => call.path === "console/probe").length), 0);
    await context.close();
  });

  await caseRun("旧记录无法分段时保留完整原文、可复制且与模型输出分离", async () => {
    const { context, page } = await open({ viewport: { width: 320, height: 720 }, colorScheme: "light" }, "?outage=healthy");
    await ready(page);
    await page.locator(".decision-action").nth(1).click();
    await page.locator("#rawModal.show").waitFor();
    await page.locator(".record-disclosure > summary").click();
    assert.equal((await page.locator(".classifier-input-notice").textContent()).trim(), "旧记录未能自动分段");
    assert.deepEqual(await page.locator(".classifier-input-part h5").allTextContents(), ["完整原文"]);
    const expected = await page.evaluate(() => window.__fixture.legacyFull);
    assert.equal(await page.locator(".classifier-input-part pre").textContent(), expected);
    await page.getByRole("button", { name: "复制复验内容" }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expected);
    assert.equal(await page.locator(".model-raw-output").evaluate((raw, input) => input.contains(raw), await page.locator(".classifier-input").elementHandle()), false);
    await shot(page, "320-light-legacy-fallback.png");
    await context.close();
  });

  await caseRun("键盘导航、详情焦点与保存草稿在轮询后保留", async () => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      colorScheme: "dark",
      reducedMotion: "reduce",
    });
    const origin = new URL(url).origin;
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin });
    await context.addInitScript(() => {
      const nativeSetInterval = window.setInterval.bind(window);
      window.setInterval = (callback, timeout, ...args) =>
        nativeSetInterval(callback, timeout === 15000 ? 120 : timeout, ...args);
    });
    const page = await context.newPage();
    page.on("pageerror", error => report.pageErrors.push({ suffix: "draft", message: error.message }));
    page.on("console", message => {
      if (message.type() === "error") report.consoleErrors.push({ suffix: "draft", message: message.text() });
    });
    await page.goto(`${url}?outage=disabled`, { waitUntil: "domcontentloaded" });
    await ready(page);
    const overview = page.getByRole("tab", { name: "运行概览" });
    await overview.focus();
    await page.keyboard.press("End");
    assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "流程与试跑");
    await page.keyboard.press("ArrowLeft");
    assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "回复与私聊");
    assert.equal(await page.locator("#panel-reply").getAttribute("hidden"), null);
    await page.getByRole("tab", { name: "运行概览" }).click();
    const detail = page.locator(".decision-action").first();
    await detail.focus();
    await page.keyboard.press("Enter");
    await page.locator("#rawModal.show").waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.id), "rawModalClose");
    await page.keyboard.press("Shift+Tab");
    assert.equal(await page.evaluate(() => document.querySelector("#rawModal .modal").contains(document.activeElement)), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains("decision-action")), true);
    await page.getByRole("tab", { name: "回复与私聊" }).click();
    const outageSwitch = page.locator("#field-classifier_outage_recovery_enabled");
    await outageSwitch.check();
    assert.equal(await page.locator("#saveBar").getAttribute("class"), "save-bar show");
    await page.getByRole("tab", { name: "判定设置" }).click();
    await page.getByRole("tab", { name: "回复与私聊" }).click();
    assert.equal(await outageSwitch.isChecked(), true);
    await page.evaluate(() => window.__fixture.setOutageState("cooling"));
    await page.locator("#outageStatusBadge").filter({ hasText: "冷却中" }).waitFor({ timeout: 3000 });
    assert.equal(await outageSwitch.isChecked(), true);
    await page.locator("#saveBarSave").click();
    await page.locator("#saveMsg").filter({ hasText: "回复参数已保存" }).waitFor();
    const writes = await page.evaluate(() => window.__fixture.calls.filter(call => call.path === "console/config"));
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0].payload, { classifier_outage_recovery_enabled: true });
    assert.equal(await outageSwitch.isChecked(), true);
    assert.equal(await page.evaluate(() => window.__fixture.modelCalls), 0);
    report.draft = {
      keyboardTabs: true,
      dialogFocusRestored: true,
      survivedPolling: true,
      savedPayload: writes[0].payload,
      modelCalls: 0,
    };
    await shot(page, "390-dark-saved-draft.png");
    await context.close();
  });

  await caseRun("生产页面未包含 fixture，测试全程无外联、控制台与页面错误", async () => {
    const source = await readFile(new URL("../../pages/guard/index.html", import.meta.url), "utf8");
    assert.doesNotMatch(source, /fixture-bridge|__fixture/);
    assert.deepEqual(report.externalRequests, []);
    assert.deepEqual(report.consoleErrors, []);
    assert.deepEqual(report.pageErrors, []);
  });
} finally {
  report.passed = report.cases.every(item => item.passed)
    && report.consoleErrors.length === 0
    && report.pageErrors.length === 0
    && report.externalRequests.length === 0;
  await writeFile(
    new URL("browser-results.json", evidenceUrl),
    `${JSON.stringify(report, null, 2)}\n`,
  );
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  console.log(
    `Task 9 UI：${report.cases.filter(item => item.passed).length}/${report.cases.length}`
    + `；控制台错误 ${report.consoleErrors.length}；页面错误 ${report.pageErrors.length}`
    + `；外联 ${report.externalRequests.length}`,
  );
  if (!report.passed) process.exitCode = 1;
}
