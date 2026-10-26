import { inject, Injectable, signal } from '@angular/core';
import { ApiService } from '../../core/api/api.service';
import { TaskDef } from './task.model';

/**
 * `GET /api/tasks`, loaded once per session and shared by home, the run screen and the search palette.
 * A failed load is retried on the next `ensure()`.
 */
@Injectable({ providedIn: 'root' })
export class TasksCatalog {
    private readonly api = inject(ApiService);
    private readonly _tasks = signal<TaskDef[]>([]);
    private readonly _loaded = signal(false);
    private pending: Promise<TaskDef[]> | null = null;

    readonly tasks = this._tasks.asReadonly();
    readonly loaded = this._loaded.asReadonly();

    ensure(): Promise<TaskDef[]> {
        if (this._loaded()) return Promise.resolve(this._tasks());
        if (!this.pending) {
            this.pending = this.api
                .get<TaskDef[]>('/api/tasks')
                .then((tasks) => {
                    this._tasks.set(Array.isArray(tasks) ? tasks : []);
                    this._loaded.set(true);
                    return this._tasks();
                })
                .finally(() => (this.pending = null));
        }
        return this.pending;
    }

    find(id: string | number | null | undefined): TaskDef | undefined {
        if (id === null || id === undefined) return undefined;
        return this._tasks().find((t) => String(t.id) === String(id));
    }
}
