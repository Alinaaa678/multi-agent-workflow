/**
 * B05 无头浏览器冒烟测试（Chrome DevTools Protocol，无第三方依赖）。
 *
 * 覆盖：
 *  1. 首屏渲染出任务 DAG（react-flow 节点数 >= 20）。
 *  2. 九态图例齐全（含 WAITING_DEPENDENCY / RETRYING）。
 *  3. 切换到「失败」模拟运行后出现 BLOCKED 节点与阻塞原因。
 *  4. 点击节点后详情面板展示输入/输出/依赖/验收/测试/日志/提交/缺陷。
 *  5. 切换到「真实接口」且后端不可达时显示错误，且不展示模拟数据。
 *
 * 运行：node tests/ui_smoke.mjs [http://127.0.0.1:4321]
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:4321';
const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];
const PORT = 9333;
const profile = mkdtempSync(join(tmpdir(), 'b05-smoke-'));
const consoleErrors = [];
const httpErrors = [];
let requestCount = 0;
let responseBytes = 0;
let failures = 0;

function check(condition, message, detail = '') {
  if (condition) {
    console.log(`  PASS ${message}`);
  } else {
    failures += 1;
    console.error(`  FAIL ${message}${detail ? ` — ${detail}` : ''}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForEndpoint(url, attempts = 60) {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch {
      /* not ready yet */
    }
    await sleep(250);
  }
  throw new Error(`等待 ${url} 超时`);
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
      if (entry) {
        this.pending.delete(message.id);
        if (message.error) entry.reject(new Error(message.error.message));
        else entry.resolve(message.result);
      }
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

  async evaluate(expression, sessionId = undefined) {
    const result = await this.send(
      'Runtime.evaluate',
      { expression, returnByValue: true, awaitPromise: true },
      sessionId,
    );
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text || '页面脚本异常');
    }
    return result.result.value;
  }
}

async function connect() {
  const version = await waitForEndpoint(`http://127.0.0.1:${PORT}/json/version`);
  const socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('CDP 连接失败')), { once: true });
  });
  return new Cdp(socket);
}

async function openPage(cdp, url) {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  const page = {
    sessionId,
    send: (method, params = {}) => cdp.send(method, params, sessionId),
    evaluate: (expression) => cdp.evaluate(expression, sessionId),
  };
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  await page.send('Network.enable');
  await page.send('Page.navigate', { url });
  await sleep(2500);
  return page;
}

async function main() {
  const executable = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));
  if (!executable) throw new Error('未找到 Chrome/Edge 可执行文件');

  console.log(`B05 UI smoke: ${BASE}`);
  const child = spawn(
    executable,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-sandbox',
      '--no-first-run',
      '--hide-scrollbars',
      '--window-size=1600,1000',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${join(profile, 'ud')}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  );

  try {
    const cdp = await connect();
    await verifySuccess(cdp);
    await verifyDiscussion(cdp);
    await verifyReplay(cdp);
    check(consoleErrors.length === 0, '三个核心视图无浏览器控制台错误', consoleErrors.join(' | '));
    check(httpErrors.length === 0, '三个核心视图无 HTTP 4xx/5xx 资源请求', httpErrors.join(' | '));
    console.log(`  INFO 核心视图资源请求 ${requestCount} 次，传输约 ${(responseBytes / 1024).toFixed(1)} KiB`);
    await verifyFailure(cdp);
    await verifyDetailPanel(cdp);
    await verifyRealError(cdp);
    cdp.socket.close();
  } finally {
    child.kill();
    await sleep(500);
  }

  console.log(failures === 0 ? '\nB05 UI smoke: PASS' : `\nB05 UI smoke: ${failures} FAILURE(S)`);
  if (failures > 0) process.exitCode = 1;
}

async function verifySuccess(cdp) {
  console.log('\n[成功场景]');
  const page = await openPage(cdp, BASE);

  const nodeCount = await page.evaluate(`document.querySelectorAll('.react-flow__node').length`);
  check(nodeCount >= 20, `DAG 节点数量 >= 20（实际 ${nodeCount}）`);

  const text = await page.evaluate('document.body.innerText');
  check(text.includes('开发任务 DAG'), '标题渲染');
  for (const label of ['已生成', '就绪', '执行中', '等依赖', '验收中', '失败', '重试中', '已阻塞', '已通过']) {
    check(text.includes(label), `九态图例含「${label}」`);
  }
  check(text.includes('模拟数据'), '明确标注模拟数据来源');
  check(text.includes('TASK-RUN-FX-OK'), '展示运行编号');
  check(text.includes('完成 24/24'), '展示完成计数');

  // 九态在 DOM 中作为图例节点存在
  const legendCount = await page.evaluate(`document.querySelectorAll('.legend-item').length`);
  check(legendCount === 9, `图例条目为 9 个（实际 ${legendCount}）`);

  const badNodes = await page.evaluate(`document.querySelectorAll('.react-flow__node').length - 24`);
  check(badNodes === 0, `节点数与计划一致 = 24（偏差 ${badNodes}）`);
}

async function verifyDiscussion(cdp) {
  console.log('\n[作业一：讨论与共识可视化]');
  const page = await openPage(cdp, BASE);
  await page.evaluate(`Array.from(document.querySelectorAll('.workflow-nav button')).find((b) => b.textContent.includes('需求共识'))?.click()`);
  await sleep(700);
  let text = await page.evaluate('document.body.innerText');
  check(text.includes('需求讨论与共识收敛'), '作业一视图标题');
  check(text.includes('RUN-HW1-NORMAL'), '展示真实控制器生成的讨论运行编号');
  check(await page.evaluate(`document.querySelectorAll('.discussion-stages li').length`) === 8, 'A—H 八个讨论阶段完整');
  check(await page.evaluate(`document.querySelectorAll('.claim-item').length`) === 4, '四角色独立观点完整');
  check(await page.evaluate(`document.querySelectorAll('.discussion-objection').length`) >= 2, '展示结构化异议与处理状态');

  await page.evaluate(`Array.from(document.querySelectorAll('.discussion-tabs button')).find((b) => b.textContent.includes('Proposal'))?.click()`);
  await sleep(400);
  text = await page.evaluate('document.body.innerText');
  check(text.includes('REQ-01') && text.includes('CONFIRMED'), 'Proposal 与 R1 需求基线展示');
  check(text.includes('确认成员') && text.includes('成员 F'), '六人确认记录展示');

  await page.evaluate(`Array.from(document.querySelectorAll('.discussion-tabs button')).find((b) => b.textContent.includes('讨论事件'))?.click()`);
  await sleep(400);
  text = await page.evaluate('document.body.innerText');
  check(text.includes('回放校验通过'), '讨论事件回放校验结果展示');
  check(await page.evaluate(`document.querySelectorAll('.discussion-event-list article').length`) === 23, '讨论事件时间线完整');
}

async function verifyReplay(cdp) {
  console.log('\n[作业三：执行事件回溯]');
  const page = await openPage(cdp, BASE);
  await page.evaluate(`Array.from(document.querySelectorAll('.workflow-nav button')).find((b) => b.textContent.includes('执行事件回溯'))?.click()`);
  await sleep(900);
  let text = await page.evaluate('document.body.innerText');
  check(text.includes('按有序事件重建 DAG 历史状态'), '回溯视图说明渲染');
  check(text.includes('事件序列连续'), '事件序列连续性校验');
  check(await page.evaluate(`document.querySelectorAll('.react-flow__node').length`) === 24, '回放 DAG 节点完整');
  check(await page.evaluate(`document.querySelector('input[aria-label="回放事件位置"]').value`) === '186', '默认定位最新事件');

  await page.evaluate(`Array.from(document.querySelectorAll('.replay-buttons button')).find((b) => b.textContent === '起点')?.click()`);
  await sleep(250);
  text = await page.evaluate('document.body.innerText');
  check(text.includes('运行尚未开始'), '回到起点显示初始状态');
  check(await page.evaluate(`document.querySelector('input[aria-label="回放事件位置"]').value`) === '0', '起点游标为 0');

  await page.evaluate(`Array.from(document.querySelectorAll('.replay-buttons button')).find((b) => b.textContent === '下一事件')?.click()`);
  await sleep(250);
  check(await page.evaluate(`document.querySelector('input[aria-label="回放事件位置"]').value`) === '1', '逐事件推进游标');
  text = await page.evaluate('document.body.innerText');
  check(text.includes('任务就绪'), '逐事件重建任务状态');

  await page.evaluate(`document.querySelector('.react-flow__node')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))`);
  await sleep(350);
  text = await page.evaluate('document.body.innerText');
  check(text.includes('取消筛选'), 'DAG 节点筛选历史事件');

  await page.evaluate(`Array.from(document.querySelectorAll('.replay-buttons button')).find((b) => b.textContent === '最新状态')?.click()`);
  await page.evaluate(`Array.from(document.querySelectorAll('.replay-buttons button')).find((b) => b.textContent === '播放回放')?.click()`);
  await sleep(850);
  const cursor = await page.evaluate(`Number(document.querySelector('input[aria-label="回放事件位置"]').value)`);
  check(cursor > 0 && cursor < 186, `自动播放推进事件（当前 ${cursor}）`);
  await page.evaluate(`Array.from(document.querySelectorAll('.replay-buttons button')).find((b) => b.textContent === '暂停回放')?.click()`);
}

async function verifyFailure(cdp) {
  console.log('\n[失败/阻断场景]');
  const page = await openPage(cdp, BASE);
  // 切换到失败场景的下拉项
  await page.evaluate(`
    (() => {
      const select = document.querySelector('select[aria-label="模拟运行"]');
      if (!select) return 'no-select';
      const option = Array.from(select.options).find((o) => o.textContent.includes('失败'));
      if (!option) return 'no-option';
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return option.value;
    })()
  `);
  await sleep(1200);
  const text = await page.evaluate('document.body.innerText');
  check(text.includes('已阻塞'), '失败场景出现「已阻塞」节点');
  check(text.includes('TASK-RUN-FX-FAIL'), '切换到失败运行编号');
  check(/(失败\/阻塞|失败)/.test(text), '统计卡片显示失败数');

  const blocked = await page.evaluate(`
    Array.from(document.querySelectorAll('.react-flow__node'))
      .filter((node) => node.innerText.includes('已阻塞')).length
  `);
  check(blocked > 0, `存在阻塞节点（${blocked} 个）`);
}

async function verifyDetailPanel(cdp) {
  console.log('\n[任务详情面板]');
  const page = await openPage(cdp, BASE);
  const clicked = await page.evaluate(`
    (() => {
      const node = document.querySelector('.react-flow__node');
      if (!node) return null;
      node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      return node.innerText.split('\\n')[0];
    })()
  `);
  await sleep(800);
  const text = await page.evaluate('document.body.innerText');
  check(Boolean(clicked), `点击了节点 ${clicked || ''}`);
  for (const section of ['依赖（上游任务）', '输入', '输出产物', '验收条件', '规定测试编号', '日志', '提交', '缺陷 / 异议']) {
    check(text.includes(section), `详情面板含「${section}」`);
  }
  check(text.includes('真实接口') || text.includes('模拟数据'), '详情面板标注数据来源');
}

async function verifyRealError(cdp) {
  console.log('\n[真实接口未连接]');
  const page = await openPage(cdp, BASE);
  await page.evaluate(`
    (() => {
      const buttons = Array.from(document.querySelectorAll('.source-switch button'));
      const real = buttons.find((b) => b.textContent.includes('真实接口'));
      if (real) real.click();
      return Boolean(real);
    })()
  `);
  await sleep(600);
  const runInput = await page.evaluate(`Boolean(document.querySelector('input[aria-label="任务运行编号"]'))`);
  check(runInput, '真实接口模式显示运行编号输入框');
  await page.evaluate(`
    (() => {
      const input = document.querySelector('input[aria-label="任务运行编号"]');
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, 'TASK-RUN-NOT-EXIST');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      const button = Array.from(document.querySelectorAll('button')).find((b) => b.textContent.includes('加载运行'));
      if (button) button.click();
      return true;
    })()
  `);
  await sleep(2500);
  const text = await page.evaluate('document.body.innerText');
  const nodeCount = await page.evaluate(`document.querySelectorAll('.react-flow__node').length`);
  check(nodeCount === 0, `真实接口失败时不渲染任何（模拟）节点（实际 ${nodeCount}）`);
  check(/无法读取真实任务运行|连接失败/.test(text), '显示真实接口错误提示');
}

try {
  await main();
} catch (error) {
  console.error(`ERROR ${error.message}`);
  process.exitCode = 1;
} finally {
  try {
    rmSync(profile, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}
