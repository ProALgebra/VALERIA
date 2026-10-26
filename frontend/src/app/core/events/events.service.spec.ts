import { TestBed } from '@angular/core/testing';
import { EventsService } from './events.service';

/** Minimal EventSource: the test drives `onopen` / `onerror` by hand. */
class FakeEventSource {
    static instances: FakeEventSource[] = [];
    onopen: (() => void) | null = null;
    onmessage: ((m: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;
    closed = false;
    constructor(readonly url: string) {
        FakeEventSource.instances.push(this);
    }
    close(): void {
        this.closed = true;
    }
}

describe('EventsService polling fallback', () => {
    let events: EventsService;
    let poll: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        vi.useFakeTimers();
        FakeEventSource.instances = [];
        vi.stubGlobal('EventSource', FakeEventSource);
        events = TestBed.inject(EventsService);
        poll = vi.fn();
    });

    afterEach(() => {
        events.stop();
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    const source = () => FakeEventSource.instances.at(-1)!;

    it('does not poll before start()', () => {
        events.pollWhileDisconnected(poll, 1000);
        vi.advanceTimersByTime(5000);
        expect(poll).not.toHaveBeenCalled();
    });

    it('polls while started and disconnected, pauses while the stream is open', () => {
        events.pollWhileDisconnected(poll, 1000);
        events.start();
        vi.advanceTimersByTime(1000);
        expect(poll).toHaveBeenCalledTimes(1);

        source().onopen!();
        vi.advanceTimersByTime(3000);
        expect(poll).toHaveBeenCalledTimes(1);

        source().onerror!();
        vi.advanceTimersByTime(1000);
        expect(poll).toHaveBeenCalledTimes(2);
    });

    it('stop() (sign-out or 401) stops every poller, including ones that were running', () => {
        events.pollWhileDisconnected(poll, 1000);
        events.start();
        source().onopen!();
        source().onerror!();
        vi.advanceTimersByTime(1000);
        expect(poll).toHaveBeenCalledTimes(1);

        events.stop();
        vi.advanceTimersByTime(60_000);
        expect(poll).toHaveBeenCalledTimes(1);
        expect(events.connected()).toBe(false);
    });

    it('a poller registered after stop() stays idle until the next start()', () => {
        events.start();
        events.stop();
        events.pollWhileDisconnected(poll, 1000);
        vi.advanceTimersByTime(5000);
        expect(poll).not.toHaveBeenCalled();

        events.start();
        vi.advanceTimersByTime(1000);
        expect(poll).toHaveBeenCalledTimes(1);
    });

    it('the unregister function stops the poller', () => {
        const unregister = events.pollWhileDisconnected(poll, 1000);
        events.start();
        unregister();
        vi.advanceTimersByTime(5000);
        expect(poll).not.toHaveBeenCalled();
    });
});
