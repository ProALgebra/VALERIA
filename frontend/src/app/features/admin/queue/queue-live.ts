import { KbqBadgeColors } from '@koobiq/components/badge';
import { adminStatusLabel, statusBadgeColor } from '../../../core/status/status';
import { isOn, QueueJob, QueueOverview } from '../config-shared/admin-config.api';
import { durLive, EMPTY } from '../config-shared/format';

export type JobKind = 'question' | 'task' | 'folder';

const KIND_LABEL: Record<JobKind, string> = { question: 'Вопрос', task: 'Задача', folder: 'Работа в папке' };

/** Unfinished jobs, in the order the queue serves them. */
const LIVE = ['running', 'cancelling', 'dispatching', 'waiting_permission', 'waiting_question', 'queued'];

/** Running longer than this is highlighted and gets the error-tinted «Остановить». */
export const LONG_RUN_MS = 60 * 60 * 1000;

export interface LiveRow {
    id: string;
    n: number;
    login: string;
    title: string;
    kind: JobKind;
    kindLabel: string;
    queue: string;
    status: string;
    statusLabel: string;
    badge: KbqBadgeColors;
    time: string;
    long: boolean;
    /** «Отменить» for a queued job, «Остановить» otherwise; `null` while it is already stopping. */
    action: 'cancel' | 'stop' | null;
    /** Running time for the stop modal, e.g. «1 ч 42 мин». */
    runFor: string;
}

export const kindOf = (j: QueueJob): JobKind => (j.task_id ? 'task' : isOn(j.consultation) ? 'question' : 'folder');

const since = (iso: string | null, now: number) => (iso ? now - Date.parse(iso) : 0);

function timeLabel(j: QueueJob, now: number): string {
    const started = durLive(since(j.started_at ?? j.created_at, now));
    switch (j.status) {
        case 'queued':
            return `Ждёт ${durLive(since(j.created_at, now))}`;
        case 'dispatching':
            return `Подготовка ${started}`;
        case 'waiting_question':
            return `Ждёт ответа пользователя ${started}`;
        case 'waiting_permission':
            return `Ждёт разрешения ${started}`;
        case 'cancelling':
            return `Останавливается ${started}`;
        default:
            return `Работает ${started}`;
    }
}

export function liveRows(ov: QueueOverview, now: number): LiveRow[] {
    const jobs = ov.jobs
        .filter((j) => LIVE.includes(j.status))
        .sort((a, b) => LIVE.indexOf(a.status) - LIVE.indexOf(b.status) || Date.parse(a.created_at) - Date.parse(b.created_at));
    return jobs.map((j, i) => {
        const kind = kindOf(j);
        const long = j.status === 'running' && since(j.started_at, now) >= LONG_RUN_MS;
        return {
            id: j.id,
            n: i + 1,
            login: j.effective_login ?? EMPTY,
            title: j.chat_title || j.task_title || j.content || EMPTY,
            kind,
            kindLabel: KIND_LABEL[kind],
            queue: j.queue_name ?? EMPTY,
            status: j.status,
            statusLabel: adminStatusLabel(j.status, kind),
            badge: statusBadgeColor(j.status, kind),
            time: timeLabel(j, now),
            long,
            action: j.status === 'cancelling' ? null : j.status === 'queued' ? 'cancel' : 'stop',
            runFor: durLive(since(j.started_at ?? j.created_at, now)),
        };
    });
}

export interface LiveStats {
    active: number;
    limit: number;
    queued: number;
    /** «дольше всех ждёт 2 мин»; `null` when nobody waits. */
    longestWait: string | null;
    waitingUser: number;
}

export function liveStats(ov: QueueOverview, now: number): LiveStats {
    const queued = ov.jobs.filter((j) => j.status === 'queued');
    const oldest = Math.min(...queued.map((j) => Date.parse(j.created_at)));
    return {
        active: ov.active,
        limit: ov.limit,
        queued: ov.queued,
        longestWait: queued.length ? `дольше всех ждёт ${durLive(now - oldest)}` : null,
        waitingUser: ov.jobs.filter((j) => j.status === 'waiting_question').length,
    };
}

const STOP_TITLE: Record<JobKind, string> = {
    folder: 'Остановить работу в папке?',
    task: 'Остановить задачу?',
    question: 'Остановить ответ на вопрос?',
};

export const stopTitle = (kind: JobKind) => STOP_TITLE[kind];

export const stopText = (r: Pick<LiveRow, 'title' | 'login' | 'runFor'>) =>
    `Сессия «${r.title}» пользователя ${r.login} работает ${r.runFor}. Агент прервёт текущий шаг. Изменения в файлах, сделанные до остановки, останутся.`;
