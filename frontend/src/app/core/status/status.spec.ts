import { KbqBadgeColors } from '@koobiq/components/badge';
import { adminStatusLabel, isActive, statusBadgeColor, statusIcon, statusLabel, statusTone } from './status';

describe('statusLabel', () => {
    const table: [string, string | undefined, string, string][] = [
        ['waiting_question', 'question', 'Ждёт вашего ответа', 'warning'],
        ['waiting_permission', 'folder', 'Ждёт разрешения', 'warning'],
        ['queued', 'task', 'В очереди', 'default'],
        ['dispatching', 'task', 'Подготовка окружения', 'default'],
        ['running', 'question', 'В работе', 'theme'],
        ['cancelling', 'task', 'Отменяется', 'default'],
        ['completed', 'question', 'Ответ готов', 'success'],
        ['completed', 'task', 'Завершена', 'success'],
        ['idle', 'folder', 'Готово', 'success'],
        ['failed', 'task', 'Завершена с ошибкой', 'error'],
        ['cancelled', 'question', 'Отменена', 'default'],
    ];

    it.each(table)('%s (%s) → %s / %s', (status, kind, label, tone) => {
        expect(statusLabel(status, kind)).toBe(label);
        expect(statusTone(status, kind)).toBe(tone);
    });

    it('maps tones to fade badge colours', () => {
        expect(statusBadgeColor('running', 'task')).toBe(KbqBadgeColors.FadeTheme);
        expect(statusBadgeColor('failed', 'task')).toBe(KbqBadgeColors.FadeError);
        expect(statusBadgeColor('queued', 'task')).toBe(KbqBadgeColors.FadeContrast);
        expect(statusBadgeColor('waiting_question', 'question')).toBe(KbqBadgeColors.FadeWarning);
        expect(statusBadgeColor('completed', 'question')).toBe(KbqBadgeColors.FadeSuccess);
    });

    it('returns an unknown status as is with the neutral tone', () => {
        expect(statusLabel('weird')).toBe('weird');
        expect(statusTone('weird')).toBe('default');
    });

    it('splits active and done', () => {
        expect(['waiting_question', 'waiting_permission', 'queued', 'dispatching', 'running', 'cancelling'].every(isActive)).toBe(true);
        expect(['completed', 'idle', 'failed', 'cancelled'].some(isActive)).toBe(false);
    });

    it('picks the nav status icon like the prototype', () => {
        expect(statusIcon('waiting_permission')).toBe('waiting');
        expect(statusIcon('running')).toBe('running');
        expect(statusIcon('completed', 'question')).toBe('success');
        expect(statusIcon('idle', 'folder')).toBe('success');
        expect(statusIcon('failed')).toBe('failed');
        expect(statusIcon('cancelled')).toBe('cancelled');
        expect(statusIcon('queued')).toBe('pending');
    });
});

describe('adminStatusLabel', () => {
    it('labels waiting_question as «Ждёт пользователя» for the admin', () => {
        expect(adminStatusLabel('waiting_question', 'question')).toBe('Ждёт пользователя');
        expect(adminStatusLabel('waiting_question')).toBe('Ждёт пользователя');
    });

    it('keeps every other label as statusLabel', () => {
        expect(adminStatusLabel('waiting_permission', 'folder')).toBe('Ждёт разрешения');
        expect(adminStatusLabel('completed', 'question')).toBe('Ответ готов');
        expect(adminStatusLabel('running', 'task')).toBe('В работе');
    });
});
