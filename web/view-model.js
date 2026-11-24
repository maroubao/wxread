// 统一日期、统计和进度口径，展示按成功次数折算的分钟数。
const DAY_MS = 86400000;
const dateFormat = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
const clockFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

export function count(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

export function targetCount(account) {
  return Math.max(1, Math.min(200, count(account.target_count) || 40));
}

export function minutes(value) {
  return String(Math.round(count(value) * 5) / 10);
}

export function dayKey(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? dateFormat.format(date) : "";
}

export function formatTime(value, now = Date.now()) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return "—";
  const day = dayKey(date);
  const today = dayKey(now);
  const yesterday = new Date(Date.parse(today + "T00:00:00Z") - DAY_MS).toISOString().slice(0, 10);
  const prefix = day === today ? "今天" : day === yesterday ? "昨天" : day.slice(5);
  return prefix + " " + clockFormat.format(date);
}

export function recentDays(now = Date.now(), size = 7) {
  const midnight = Date.parse(dayKey(now) + "T00:00:00Z");
  return Array.from({ length: size }, (_value, index) => new Date(midnight - (size - index - 1) * DAY_MS).toISOString().slice(0, 10));
}

export function stateMonths(now = Date.now(), size = 7) {
  const start = Date.parse(recentDays(now, size)[0] + "T00:00:00+08:00");
  const cursor = new Date(start);
  cursor.setUTCDate(1);
  cursor.setUTCHours(0, 0, 0, 0);
  const months = [];
  while (cursor.getTime() <= now) {
    months.push(cursor.toISOString().slice(0, 7));
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  }
  return months.reverse();
}

export function runTime(run) {
  return run.finished_at || run.at || run.started_at || "";
}

export function runKey(run) {
  return String(run.run_id || [run.account_id, run.started_at || run.at, run.read_count].join("/"));
}

export function sortRuns(runs) {
  return [...runs].sort((left, right) => (Date.parse(runTime(right)) || 0) - (Date.parse(runTime(left)) || 0));
}

export function payloadReady(account) {
  const payload = account.read_payload || {};
  return ["appId", "ps", "pc", "b", "c"].every(key => Boolean(payload[key]));
}

export function runnable(account) {
  return account.enabled !== false && account.status === "ok" && payloadReady(account);
}

export function successful(run) {
  return count(run.read_count) >= targetCount(run) && Number(run.exit_code || 0) === 0;
}

export function withProgress(runs, accounts) {
  const combined = [...runs];
  const recorded = new Set(runs.map(runKey));
  for (const account of accounts) {
    const progress = account.progress;
    if (!progress?.run_id || !count(progress.read_count) || recorded.has(String(progress.run_id))) continue;
    combined.push({
      ...progress, account_id: account.id, label: account.label,
      virtual: true, started_at: progress.started_at, finished_at: progress.updated_at,
    });
  }
  return combined;
}

export function summary(accounts, runs, now = Date.now()) {
  const today = dayKey(now);
  const combined = withProgress(runs, accounts);
  const todays = combined.filter(run => dayKey(run.started_at || run.at) === today);
  const byAccount = new Map();
  for (const run of todays) {
    const id = String(run.account_id || "");
    byAccount.set(id, (byAccount.get(id) || 0) + count(run.read_count));
  }
  const active = accounts.filter(runnable);
  const recent = runs.filter(run => recentDays(now).includes(dayKey(run.started_at || run.at)));
  const wins = recent.filter(successful).length;
  return {
    byAccount,
    readCount: todays.reduce((sum, run) => sum + count(run.read_count), 0),
    hasToday: todays.length > 0,
    target: active.reduce((sum, account) => sum + targetCount(account), 0),
    remaining: active.reduce((sum, account) => sum + Math.max(0, targetCount(account) - (byAccount.get(String(account.id)) || 0)), 0),
    completed: active.filter(account => (byAccount.get(String(account.id)) || 0) >= targetCount(account)).length,
    activeCount: active.length,
    needsLogin: accounts.filter(account => account.status === "needs_login" && !["payload_invalid", "no_synckey"].includes(account.note)),
    needsPayload: accounts.filter(account => account.status !== "bad" && (!payloadReady(account) || ["payload_invalid", "no_synckey"].includes(account.note))),
    successRate: recent.length ? Math.round(100 * wins / recent.length) : null,
    wins, totalRuns: recent.length,
    lastRun: sortRuns(runs)[0] || null,
  };
}

export function chartData(accounts, runs, now = Date.now()) {
  const combined = withProgress(runs, accounts);
  const today = dayKey(now);
  return recentDays(now).map(day => {
    const records = combined.filter(run => dayKey(run.started_at || run.at) === day);
    if (!records.length) return { day, value: null, target: 0, full: false };
    const byAccount = new Map();
    for (const run of records) {
      const id = String(run.account_id || "");
      const entry = byAccount.get(id) || { count: 0, target: targetCount(run) };
      entry.count += count(run.read_count);
      entry.target = Math.max(entry.target, targetCount(run));
      byAccount.set(id, entry);
    }
    if (day === today) {
      for (const account of accounts.filter(runnable)) {
        const id = String(account.id);
        const entry = byAccount.get(id) || { count: 0, target: targetCount(account) };
        entry.target = targetCount(account);
        byAccount.set(id, entry);
      }
    }
    const values = [...byAccount.values()];
    const active = day === today ? accounts.filter(runnable) : [];
    return {
      day, value: values.reduce((sum, entry) => sum + entry.count / 2, 0),
      target: day === today ? active.reduce((sum, account) => sum + targetCount(account) / 2, 0) : values.reduce((sum, entry) => sum + entry.target / 2, 0),
      full: day === today ? active.length > 0 && active.every(account => (byAccount.get(String(account.id))?.count || 0) >= targetCount(account)) : values.every(entry => entry.count >= entry.target),
    };
  });
}
