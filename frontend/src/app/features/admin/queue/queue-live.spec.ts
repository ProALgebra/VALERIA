import type { QueueJob, QueueOverview } from '../config-shared/admin-config.api';
import { kindOf, liveRows, liveStats, stopText, stopTitle } from './queue-live';

const NOW = Date.parse('2026-10-25T12:00:00Z');
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

const job = (id: string, status: string, extra: Partial<QueueJob> = {}): QueueJob => ({
    id,
    status,
    created_at: ago(10),
    started_at: status === 'queued' ? null : ago(5),
    finished_at: null,
    effective_login: 'p.volkov',
    chat_title: null,
    content: null,
    task_id: null,
    task_title: null,
    consultation: 0,
    queue_id: 'q1',
    queue_name: 'Основная',
    ...extra,
});

const overview = (jobs: QueueJob[]): QueueOverview => ({
    limit: 4,
    env_limit: 4,
    hard_limit: 16,
    active: 2,
    queued: 2,
    default_chat_queue_id: 'q1',
    queues: [],
    jobs,
});

describe('queue live', () => {
    it('detects the job kind', () => {
        expect(kindOf(job('a', 'running', { task_id: 't1' }))).toBe('task');
        expect(kindOf(job('a', 'running', { consultation: 1 }))).toBe('question');
        expect(kindOf(job('a', 'running'))).toBe('folder');
    });

    it('keeps only unfinished jobs, running first, queued by age', () => {
        const rows = liveRows(
            overview([
                job('q-new', 'queued', { created_at: ago(1) }),
                job('done', 'completed'),
                job('q-old', 'queued', { created_at: ago(3) }),
                job('run', 'running', { chat_title: 'Чат' }),
            ]),
            NOW,
        );
        expect(rows.map((r) => r.id)).toEqual(['run', 'q-old', 'q-new']);
        expect(rows.map((r) => r.n)).toEqual([1, 2, 3]);
    });

    it('labels time and action by status', () => {
        const [run, stopping, waiting, queued] = liveRows(
            overview([
                job('run', 'running', { started_at: ago(90) }),
                job('stopping', 'cancelling'),
                job('waiting', 'waiting_question'),
                job('queued', 'queued', { created_at: ago(2) }),
            ]),
            NOW,
        );
        expect(run.time).toBe('Работает 1 ч 30 мин');
        expect(run.long).toBe(true);
        expect(run.action).toBe('stop');
        expect(stopping.action).toBeNull();
        expect(waiting.statusLabel).toBe('Ждёт пользователя');
        expect(waiting.time).toBe('Ждёт ответа пользователя 5 мин');
        expect(queued.time).toBe('Ждёт 2 мин');
        expect(queued.action).toBe('cancel');
    });

    it('falls back to «—» for a missing title and login', () => {
        const [row] = liveRows(overview([job('a', 'running', { effective_login: null })]), NOW);
        expect(row.title).toBe('—');
        expect(row.login).toBe('—');
    });

    it('summarises the queue', () => {
        const stats = liveStats(overview([job('a', 'queued', { created_at: ago(7) }), job('b', 'queued', { created_at: ago(2) }), job('c', 'waiting_question')]), NOW);
        expect(stats).toEqual({ active: 2, limit: 4, queued: 2, longestWait: 'дольше всех ждёт 7 мин', waitingUser: 1 });
        expect(liveStats(overview([]), NOW).longestWait).toBeNull();
    });

    it('builds the stop modal texts', () => {
        expect(stopTitle('folder')).toBe('Остановить работу в папке?');
        expect(stopText({ title: 'Чат', login: 'p.volkov', runFor: '5 мин' })).toContain('Сессия «Чат» пользователя p.volkov работает 5 мин.');
    });
});
