import { snoozeNavLabel, snoozeUntil, snoozeWhenText, toIsoWithOffset } from './snooze';

describe('snooze time', () => {
    // Saturday 24 Oct 2026, 14:30 local time
    const sat = new Date(2026, 9, 24, 14, 30, 0);

    it('computes return moments in local time', () => {
        expect(snoozeUntil('1h', sat)).toEqual(new Date(2026, 9, 24, 15, 30, 0));
        expect(snoozeUntil('tomorrow', sat)).toEqual(new Date(2026, 9, 25, 10, 0, 0));
        expect(snoozeUntil('monday', sat)).toEqual(new Date(2026, 9, 26, 10, 0, 0));
        const mon = new Date(2026, 9, 26, 9, 0, 0);
        expect(snoozeUntil('monday', mon)).toEqual(new Date(2026, 10, 2, 10, 0, 0));
    });

    it('formats like the prototype', () => {
        expect(snoozeNavLabel(snoozeUntil('1h', sat), sat)).toBe('15:30');
        expect(snoozeNavLabel(snoozeUntil('tomorrow', sat), sat)).toBe('вс, 10:00');
        expect(snoozeNavLabel(snoozeUntil('monday', sat), sat)).toBe('пн, 10:00');
        expect(snoozeWhenText(snoozeUntil('1h', sat), sat)).toBe('в 15:30');
        expect(snoozeWhenText(snoozeUntil('tomorrow', sat), sat)).toBe('завтра в 10:00');
        expect(snoozeWhenText(snoozeUntil('monday', sat), sat)).toBe('в понедельник в 10:00');
    });

    it('serializes with the local offset', () => {
        const s = toIsoWithOffset(new Date(2026, 9, 26, 10, 0, 0));
        expect(s).toMatch(/^2026-10-26T10:00:00[+-]\d{2}:\d{2}$/);
        expect(new Date(s).getTime()).toBe(new Date(2026, 9, 26, 10, 0, 0).getTime());
    });
});
