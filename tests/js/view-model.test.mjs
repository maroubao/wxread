import assert from "node:assert/strict";
import test from "node:test";
import * as vm from "../../web/view-model.js";

const now = Date.parse("2026-11-23T18:00:00Z");
const ready = { id: "a", enabled: true, status: "ok", target_count: 40, read_payload: { appId: "app", ps: "ps", pc: "pc", b: "book", c: "chapter" } };

test("今日统计以北京时间归属，01点运行不能落到昨天", () => {
  const run = { account_id: "a", started_at: "2026-11-23T17:07:00Z", read_count: 40, target_count: 40, exit_code: 0 };
  const data = vm.summary([ready], [run], now);
  assert.equal(vm.dayKey(run.started_at), "2026-11-24");
  assert.equal(data.readCount, 40);
  assert.equal(data.completed, 1);
});

test("近七日跨月时读取两份UTC月份记录", () => {
  assert.deepEqual(vm.stateMonths(Date.parse("2026-11-01T04:00:00Z")), ["2026-11", "2026-10"]);
});

test("后端降序数组仍取真正的最新记录", () => {
  const recent = { started_at: "2026-11-23T17:07:00Z", finished_at: "2026-11-23T17:27:00Z" };
  const old = { started_at: "2026-11-22T17:07:00Z" };
  assert.equal(vm.summary([], [recent, old], now).lastRun, recent);
});

test("同一run的落盘记录和进度不会重复累计", () => {
  const run = { run_id: "one", account_id: "a", started_at: "2026-11-23T17:07:00Z", read_count: 40, target_count: 40 };
  const account = { ...ready, progress: { ...run, updated_at: run.started_at } };
  assert.equal(vm.summary([account], [run], now).readCount, 40);
});

test("尚未写入记录的运行进度会显示在今日统计", () => {
  const account = { ...ready, progress: { run_id: "live", status: "running", started_at: "2026-11-23T17:07:00Z", updated_at: "2026-11-23T17:17:00Z", read_count: 18, target_count: 40 } };
  assert.equal(vm.summary([account], [], now).readCount, 18);
});

test("L2自愈完成目标计为成功", () => {
  const run = { account_id: "a", started_at: "2026-11-23T17:07:00Z", read_count: 40, target_count: 40, level: "L2", exit_code: 0 };
  assert.equal(vm.summary([ready], [run], now).successRate, 100);
});

test("无记录和实际读取零次保持不同图表语义", () => {
  const run = { account_id: "a", started_at: "2026-11-23T17:07:00Z", read_count: 0, target_count: 40 };
  const days = vm.chartData([ready], [run], now);
  assert.equal(days[5].value, null);
  assert.equal(days[6].value, 0);
});
