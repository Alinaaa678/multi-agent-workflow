/**
 * 课堂作业三真实接口浏览器回归：暂停 → 继续 → 单步 → 取消。
 * 运行前需启动 B02 与前端，并通过 B02_BASE 指定后端地址。
 * 运行：node tests/ui_controls.mjs http://127.0.0.1:4321 shots/real-controls.png
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request as httpRequest } from 'node:http';

const BASE = process.argv[2] || 'http://127.0.0.1:4321';
const OUT = process.argv[3] || 'shots/real-controls.png';
const API = process.env.B02_BASE || 'http://127.0.0.1:7100';
const RUN_ID = 'TASK-RUN-UI-CONTROL';
const PORT = 9341;
const profile = mkdtempSync(join(tmpdir(), 'b05-controls-'));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const consoleErrors = [];
const httpErrors = [];
let requestCount = 0;
let responseBytes = 0;
let failures = 0;

function check(condition, message) {
  console.log(`  ${condition ? 'PASS' : 'FAIL'} ${message}`);
  if (!condition) failures += 1;
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') consoleErrors.push(message.params.exceptionDetails.text);
      if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
        consoleErrors.push(message.params.args.map((arg) => arg.value ?? arg.description ?? '').join(' '));
      }
      if (message.method === 'Network.responseReceived') {
        requestCount += 1;
        const { status, url } = message.params.response;
        if (status >= 400) httpErrors.push(`${status} ${url}`);
      }
      if (message.method === 'Network.loadingFinished') responseBytes += message.params.encodedDataLength || 0;
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message));
      else entry.resolve(message.result);
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify(message));
    });
  }

  async evaluate(expression, sessionId) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || '页面脚本异常');
    return result.result.value;
  }
}

async function waitFor(url) {
  for (let index = 0; index < 80; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch { /* not ready */ }
    await sleep(250);
  }
  throw new Error(`等待 ${url} 超时`);
}

async function clickButton(page, label) {
  return page.evaluate(`
    (() => {
      const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent.trim().includes(${JSON.stringify(label)}));
      if (!button || button.disabled) return false;
      button.click();
      return true;
    })()
  `);
}

async function waitForText(page, text, attempts = 30) {
  for (let index = 0; index < attempts; index += 1) {
    const found = await page.evaluate(`document.body.innerText.includes(${JSON.stringify(text)})`);
    if (found) return true;
    await sleep(250);
  }
  return false;
}

async function createDemoRun() {
  const body = JSON.stringify({
    spec_path: 'b01-spec-planner/docs/fixtures/campus-lost-found.spec.json',
    run_id: RUN_ID,
  });
  await new Promise((resolve, reject) => {
    const request = httpRequest(new URL(`${API}/api/task-runs/from-spec`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body), Connection: 'close' },
    }, (response) => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { text += chunk; });
      response.on('end', () => {
        if (response.statusCode === 201) resolve();
        else reject(new Error(`创建真实运行失败：${response.statusCode} ${text}`));
      });
    });
    request.on('error', reject);
    request.end(body);
  });
}

async function main() {
  const executable = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ].find((candidate) => existsSync(candidate));
  if (!executable) throw new Error('未找到 Chrome/Edge');

  await createDemoRun();

  const child = spawn(executable, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--hide-scrollbars',
    '--window-size=1680,1020', `--remote-debugging-port=${PORT}`, `--user-data-dir=${join(profile, 'ud')}`, 'about:blank',
  ], { stdio: 'ignore' });

  try {
    const version = await waitFor(`http://127.0.0.1:${PORT}/json/version`);
    const socket = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', () => reject(new Error('CDP 连接失败')), { once: true });
    });
    const cdp = new Cdp(socket);
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    const page = {
      send: (method, params = {}) => cdp.send(method, params, sessionId),
      evaluate: (expression) => cdp.evaluate(expression, sessionId),
    };
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    await page.send('Network.enable');
    await page.send('Emulation.setDeviceMetricsOverride', { width: 1680, height: 1020, deviceScaleFactor: 1, mobile: false });
    await page.send('Page.navigate', { url: BASE });
    await sleep(2200);

    await clickButton(page, '真实接口');
    await sleep(300);
    await page.evaluate(`
      (() => {
        const setValue = (input, value) => {
          Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
        };
        setValue(document.querySelector('input[aria-label="后端地址"]'), ${JSON.stringify(API)});
        setValue(document.querySelector('input[aria-label="任务运行编号"]'), ${JSON.stringify(RUN_ID)});
      })()
    `);
    await clickButton(page, '加载运行');
    check(await waitForText(page, RUN_ID), '真实运行已加载');
    check(await page.evaluate(`document.querySelectorAll('.react-flow__node').length === 24`), '真实 DAG 渲染 24 个节点');
    const initialReadyCount = await page.evaluate(`
      Array.from(document.querySelectorAll('.react-flow__node')).filter((node) => node.textContent.includes('就绪')).length
    `);

    await clickButton(page, '执行事件回溯');
    await sleep(500);
    const replayStatuses = await page.evaluate(`
      Array.from(document.querySelectorAll('.react-flow__node')).map((node) => node.textContent)
    `);
    const waitingCount = replayStatuses.filter((text) => text.includes('等依赖')).length;
    const replayReadyCount = replayStatuses.filter((text) => text.includes('就绪')).length;
    check(
      waitingCount === 24 - initialReadyCount && replayReadyCount === initialReadyCount,
      `真实回溯状态分布与初始 DAG 一致（就绪 ${replayReadyCount}/${initialReadyCount}，等依赖 ${waitingCount}/${24 - initialReadyCount}）`,
    );
    await clickButton(page, '开发任务 DAG');
    await sleep(350);

    check(await clickButton(page, '暂停派发'), '点击暂停派发');
    check(await waitForText(page, '已暂停后续任务派发'), '显示暂停控制回执');
    check(await waitForText(page, '已暂停'), '运行状态更新为已暂停');
    check(await page.evaluate(`Array.from(document.querySelectorAll('button')).find((b) => b.textContent.trim() === '推进一步')?.disabled === true`), '暂停后单步按钮禁用');

    check(await clickButton(page, '继续运行'), '点击继续运行');
    check(await waitForText(page, '运行已继续'), '显示继续控制回执');
    check(await clickButton(page, '推进一步'), '继续后执行一个调度步');
    check(await waitForText(page, '1/24'), '单步后完成计数更新为 1/24');
    check(await page.evaluate(`
      (() => {
        const node = Array.from(document.querySelectorAll('.react-flow__node')).find((item) => item.textContent.includes('T01'));
        if (!node) return false;
        node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        return true;
      })()
    `), '打开已完成真实任务详情');
    await sleep(400);
    const realDetail = await page.evaluate(`document.querySelector('.detail-panel')?.innerText ?? ''`);
    check(realDetail.includes('TC-AUTH-01') && realDetail.includes('模拟通过'), '真实任务详情展示实际测试结果');
    check(realDetail.includes('logs/T01/'), '真实任务详情展示执行日志及摘要');

    await clickButton(page, '监控与控制');
    await page.evaluate('window.confirm = () => true');
    check(await clickButton(page, '取消运行'), '确认并点击取消运行');
    check(await waitForText(page, '运行已取消，任务现场已保留'), '显示取消控制回执');
    check(await page.evaluate(`Array.from(document.querySelectorAll('button')).find((b) => b.textContent.trim() === '推进一步')?.disabled === true`), '取消后禁止继续派发');

    await page.evaluate(`
      (() => {
        const select = document.querySelector('select[aria-label="事件筛选"]');
        Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set.call(select, 'control');
        select.dispatchEvent(new Event('change', { bubbles: true }));
      })()
    `);
    await sleep(400);
    const controlEvents = await page.evaluate(`Array.from(document.querySelectorAll('.event-item')).map((item) => item.innerText).join('\\n')`);
    check(controlEvents.includes('运行暂停') && controlEvents.includes('运行继续') && controlEvents.includes('运行取消'), '人工控制筛选显示三类审计事件');

    const { data } = await page.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT, Buffer.from(data, 'base64'));
    console.log(`wrote ${OUT}`);

    await clickButton(page, '需求共识');
    await sleep(350);
    check(await clickButton(page, '创建真实讨论'), '在页面创建真实讨论');
    check(await waitForText(page, '可推进'), '真实讨论创建完成并等待推进');
    const discussionRunId = await page.evaluate(`document.querySelector('input[aria-label="讨论运行编号"]')?.value ?? ''`);
    check(discussionRunId.startsWith('RUN-'), `页面获得真实讨论编号 ${discussionRunId}`);
    check(await clickButton(page, '运行至人工介入'), '在页面推进到人工控制点');
    check(await waitForText(page, '等待人工'), '讨论停在六人确认控制点');
    check(await page.evaluate(`document.querySelectorAll('.claim-item').length === 4`), '真实讨论展示四角色观点');
    check(await page.evaluate(`
      (() => {
        const boxes = Array.from(document.querySelectorAll('input[name="member-confirmation"]'));
        boxes.slice(0, 5).forEach((box) => box.click());
        return boxes.length === 6;
      })()
    `), '页面显示六名成员确认框');
    check(await waitForText(page, '已确认 5 / 6'), '五名成员勾选状态可见');
    check(await page.evaluate(`Array.from(document.querySelectorAll('button')).find((b) => b.textContent.trim() === '六人确认并发布 R1')?.disabled === true`), '仅五人确认时禁止发布 R1');
    const memberGateOut = OUT.replace(/[^\\/]+$/, 'real-member-gate.png');
    const { data: memberGateImage } = await page.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(memberGateOut, Buffer.from(memberGateImage, 'base64'));
    console.log(`wrote ${memberGateOut}`);
    await page.evaluate(`Array.from(document.querySelectorAll('input[name="member-confirmation"]'))[5]?.click()`);
    check(await waitForText(page, '已确认 6 / 6'), '六名成员全部确认');
    check(await clickButton(page, '六人确认并发布 R1'), '在页面发布已确认 R1');
    check(await waitForText(page, 'CONFIRMED'), '真实讨论展示已确认 R1');
    const discussionOut = OUT.replace(/[^\\/]+$/, 'real-discussion-confirmation.png');
    const { data: discussionImage } = await page.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(discussionOut, Buffer.from(discussionImage, 'base64'));
    console.log(`wrote ${discussionOut}`);
    check(await clickButton(page, '进入第二阶段任务编排'), '点击 R1 到第二阶段桥接');
    check(await waitForText(page, '第二阶段入口'), '桥接后回到开发任务 DAG');
    await sleep(2500);
    const phase2RunId = await page.evaluate(`document.querySelector('input[aria-label="任务运行编号"]')?.value ?? ''`);
    check(phase2RunId.startsWith('TASK-RUN-') && phase2RunId !== RUN_ID, `生成新的真实任务运行 ${phase2RunId}`);
    check(await page.evaluate(`document.querySelectorAll('.react-flow__node').length > 0`), '由 R1 生成并渲染真实任务 DAG');
    check(await page.evaluate(`document.querySelector('.control-ack') === null`), '新运行不残留上一运行的取消回执');
    check(await page.evaluate(`
      (() => {
        const canvas = document.querySelector('.canvas')?.getBoundingClientRect();
        const nodes = Array.from(document.querySelectorAll('.react-flow__node'));
        if (!canvas || nodes.length === 0) return false;
        return nodes.every((node) => {
          const rect = node.getBoundingClientRect();
          return rect.right > canvas.left && rect.left < canvas.right
            && rect.bottom > canvas.top && rect.top < canvas.bottom;
        });
      })()
    `), '桥接后 DAG 节点实际位于可视区域');
    const phaseBridgeOut = OUT.replace(/[^\\/]+$/, 'real-phase-bridge.png');
    const { data: phaseBridgeImage } = await page.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(phaseBridgeOut, Buffer.from(phaseBridgeImage, 'base64'));
    console.log(`wrote ${phaseBridgeOut}`);

    check(consoleErrors.length === 0, `浏览器控制台无错误（${consoleErrors.length}）`);
    check(httpErrors.length === 0, `无 HTTP 4xx/5xx 资源请求（${httpErrors.length}）`);
    console.log(`  INFO 请求 ${requestCount} 个，传输约 ${(responseBytes / 1024).toFixed(1)} KiB`);
    if (consoleErrors.length) console.error(consoleErrors.join('\n'));
    if (httpErrors.length) console.error(httpErrors.join('\n'));
    socket.close();
  } finally {
    child.kill();
    await sleep(500);
    rmSync(profile, { recursive: true, force: true });
  }

  console.log(failures === 0 ? '\nB05 real controls: PASS' : `\nB05 real controls: ${failures} FAILURE(S)`);
  if (failures) process.exitCode = 1;
}

try {
  await main();
} catch (error) {
  console.error(`ERROR ${error.message}`);
  process.exitCode = 1;
} finally {
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
}
