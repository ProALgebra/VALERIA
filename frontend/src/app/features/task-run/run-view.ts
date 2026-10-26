import { isWaiting } from '../../core/status/status';
import { clockTime, shortDate, TaskRun } from '../../shared/tasks/task.model';

export type StageState = 'done' | 'active' | 'error' | 'off' | 'future';

export interface Stage {
    name: string;
    time: string;
    state: StageState;
}

const STAGES = ['В очереди', 'Подготовка окружения', 'Выполнение', 'Готово'];
/** plan_first tasks get a «План» stage before «Выполнение». */
const STAGES_PLAN_FIRST = ['В очереди', 'Подготовка окружения', 'План', 'Выполнение', 'Готово'];

/** Index of the stage the agent is working on (plan or build). */
function workIndex(run: TaskRun): number {
    if (!run.plan_first) return 2;
    return run.phase === 'build' ? 3 : 2;
}

/** Current stage index; equals the number of stages when the run is completed. */
export function stageIndex(run: TaskRun): number {
    const names = run.plan_first ? STAGES_PLAN_FIRST : STAGES;
    switch (run.status) {
        case 'queued':
            return 0;
        case 'dispatching':
            return 1;
        case 'completed':
            return names.length;
        case 'failed':
        case 'cancelled':
            return run.started_at ? workIndex(run) : 0;
        default:
            // running, waiting_*, cancelling
            return workIndex(run);
    }
}

/** The stages row of the run screen (prototype `.v-stages`). */
export function runStages(run: TaskRun): Stage[] {
    const names = run.plan_first ? STAGES_PLAN_FIRST : STAGES;
    const at = stageIndex(run);
    const last = names.length - 1;
    return names.map((name, i) => {
        let state: StageState = 'future';
        if (i < at) state = 'done';
        else if (i === at) state = run.status === 'failed' ? 'error' : run.status === 'cancelled' ? 'off' : 'active';
        const stamp = i === 0 ? run.created_at : i === last ? run.finished_at : run.started_at;
        return { name, state, time: i <= at ? clockTime(stamp) : '' };
    });
}

/** «задание / задания / заданий». */
export function pluralTasks(n: number): string {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 'задание';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'задания';
    return 'заданий';
}

/** «4 мин», «1 ч 5 мин»; under a minute counts as 1 мин. */
export function minutesText(ms: number): string {
    const total = Math.max(1, Math.round(ms / 60_000));
    if (total < 60) return `${total} мин`;
    const h = Math.floor(total / 60);
    const m = total % 60;
    return m ? `${h} ч ${m} мин` : `${h} ч`;
}

/** «8 мин, из них 2 мин в очереди» for the aside of a finished run. */
export function durationText(run: TaskRun): string {
    const created = Date.parse(run.created_at);
    const finished = run.finished_at ? Date.parse(run.finished_at) : NaN;
    if (Number.isNaN(created) || Number.isNaN(finished)) return '';
    const total = minutesText(finished - created);
    const started = run.started_at ? Date.parse(run.started_at) : NaN;
    if (Number.isNaN(started) || started - created < 60_000) return total;
    return `${total}, из них ${minutesText(started - created)} в очереди`;
}

/** «Сегодня, 14:15», «Вчера, 09:02» or «12 окт, 09:02». */
export function startedText(iso: string | null | undefined, now: number): string {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((startOfDay(new Date(now)) - startOfDay(d)) / 86_400_000);
    const time = clockTime(iso);
    if (days <= 0) return `Сегодня, ${time}`;
    if (days === 1) return `Вчера, ${time}`;
    return `${shortDate(d)}, ${time}`;
}

/** Statuses where «Отменить» is offered. */
export function canCancel(status: string): boolean {
    return ['queued', 'dispatching', 'running'].includes(status) || isWaiting(status);
}
