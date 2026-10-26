// Port of the prototype STATS generator (valeria-ui/prototype.html, "Админ: статистика").
// t is minutes from local midnight 2026-07-27; wait/work are minutes.

export interface StatRun { t: number; task: string; who: string; wait: number; work: number; st: string }

export const STAT_TASK_ID: Record<string, string | null> = {
  q: null, ci: "t-ci", mr: "t-mr", notes: "t-notes", dup: "t-dup", yt: "t-yt",
};

export function generateStats(): StatRun[] {
  let a = 20261024;
  const rnd = () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const pick = <T>(arr: T[]) => arr[Math.floor(rnd() * arr.length)];
  const WORK: Record<string, () => number> = {
    q: () => 0.4 + rnd() ** 2 * 3.5, yt: () => 1 + rnd() * 3, dup: () => 2 + rnd() * 3.5,
    ci: () => 5 + rnd() * 9 + (rnd() < 0.08 ? 10 + rnd() * 14 : 0),
    mr: () => 6 + rnd() * 11 + (rnd() < 0.06 ? 12 + rnd() * 12 : 0),
    notes: () => 4 + rnd() * 8 + (rnd() < 0.1 ? 8 + rnd() * 9 : 0),
  };
  const FAIL: Record<string, number> = { q: 0.01, yt: 0.03, dup: 0.02, ci: 0.09, mr: 0.04, notes: 0.05 };
  const runs: StatRun[] = [];
  const add = (day: number, task: string, who: string, tod: number) => {
    const busy = task !== "q" && ((tod > 660 && tod < 750) || (tod > 900 && tod < 990));
    const wait = task === "q" ? rnd() * 0.4 : busy ? (rnd() < 0.5 ? 2 + rnd() * 7 : rnd() * 1.5) : (rnd() < 0.8 ? rnd() * 0.6 : rnd() * 2);
    let work = WORK[task](), st = "done";
    const r = rnd();
    if (r < FAIL[task]) { st = "failed"; work *= 0.7; } else if (r < FAIL[task] + 0.025) { st = "cancelled"; work *= 0.4; }
    runs.push({ t: day * 1440 + tod, task, who, wait, work, st });
  };
  const tod = () => 540 + ((rnd() + rnd()) / 2) * 600;
  for (let d = 0; d < 89; d++) {
    const dow = d % 7, k = 0.5 + (0.5 * d) / 89;
    const people = ["i.sokolova", "p.volkov", "a.nikitina", "m.belov", "d.orlov", ...(d < 70 ? ["s.egorova"] : [])];
    const nT = dow < 5 ? Math.round((7 + rnd() * 7) * k) : dow === 5 ? Math.floor(rnd() * 3) : 0;
    const nQ = dow < 5 ? Math.round((8 + rnd() * 10) * k) : dow === 5 ? Math.floor(rnd() * 3) : 0;
    for (let i = 0; i < nT; i++) add(d, pick(["ci", "ci", "ci", "mr", "mr", "mr", "dup", "dup", "notes"]), pick(people), tod());
    for (let i = 0; i < nQ; i++) add(d, "q", pick(people), tod());
    for (let i = 0, n = 1 + Math.floor(rnd() * 3); i < n; i++) add(d, "yt", "youtrack-bot", 300 + rnd() * 1080);
  }
  const m = (s: string) => (s ? +s.split(":")[0] * 60 + +s.split(":")[1] : null);
  const today: [string, string, string, string, string, string?][] = [
    ["ci", "i.sokolova", "8:40", "8:41", "8:49"], ["mr", "p.volkov", "9:05", "9:05", "9:17"], ["dup", "a.nikitina", "9:20", "9:22", "9:25"],
    ["yt", "youtrack-bot", "9:48", "9:48", "9:51"], ["ci", "m.belov", "10:10", "10:11", "10:18"], ["notes", "m.belov", "10:30", "10:34", "10:44"],
    ["ci", "i.sokolova", "10:50", "10:56", "11:05"], ["mr", "a.nikitina", "10:55", "11:01", "11:12"], ["dup", "p.volkov", "11:00", "11:05", "11:08"],
    ["yt", "youtrack-bot", "11:40", "11:40", "11:42"], ["ci", "p.volkov", "12:10", "12:11", "12:20", "failed"], ["ci", "a.nikitina", "12:30", "12:30", "12:36"],
    ["dup", "m.belov", "12:45", "12:46", "12:49"], ["mr", "p.volkov", "13:05", "13:05", "13:15"], ["yt", "youtrack-bot", "13:30", "13:30", "13:32"],
    ["notes", "a.nikitina", "13:40", "13:41", "13:44", "cancelled"], ["mr", "i.sokolova", "14:20", "14:24", "", "running"],
    ["ci", "i.sokolova", "14:28", "", "", "queued"], ["yt", "youtrack-bot", "14:29", "14:29", "", "running"], ["notes", "m.belov", "14:29", "", "", "queued"],
  ];
  for (const [task, who, q, s, f, st = "done"] of today) {
    const qm = m(q)!, sm = m(s) ?? 870, fm = m(f) ?? 870;
    runs.push({ t: 89 * 1440 + qm, task, who, wait: sm - qm, work: fm - sm, st });
  }
  const questions: [string, string, number][] = [["9:12", "i.sokolova", 1.2], ["10:40", "p.volkov", 0.8], ["11:30", "a.nikitina", 2.6], ["12:52", "m.belov", 0.6], ["13:58", "i.sokolova", 1.9], ["14:12", "p.volkov", 3.1]];
  for (const [q, who, w] of questions) runs.push({ t: 89 * 1440 + m(q)!, task: "q", who, wait: 0.1, work: w, st: "done" });
  return runs.sort((x, y) => x.t - y.t);
}

/** Prototype wall-clock ms of a stats minute offset. */
export const statTime = (minutes: number) => new Date(2026, 6, 27, 0, 0, 0, Math.round(minutes * 60000)).getTime();
