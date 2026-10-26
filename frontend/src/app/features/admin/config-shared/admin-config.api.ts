import { inject, Injectable } from '@angular/core';
import { ApiService } from '../../../core/api/api.service';

/** Admin task as returned by `GET /api/admin/tasks`. Booleans arrive as 0/1 from SQLite. */
export interface AdminTask {
    id: string;
    title: string;
    description: string | null;
    content: string;
    agent: string;
    provider_id: string;
    model_id: string;
    model_variant: string | null;
    queue_id: string;
    is_published: number | boolean;
    visibility: 'all' | 'selected';
    /** Kept verbatim and echoed back in PATCH (optimistic concurrency). */
    updated_at: string;
    /** Raw user id (RECONCILE §contract). Not always present. */
    updated_by?: string | null;
    plan_first: number | boolean;
    input_hint: string | null;
    model: string;
    audience_user_ids: string[];
    next_task_ids: string[];
}

export interface TaskPayload {
    title: string;
    description: string | null;
    input_hint: string | null;
    content: string;
    agent: string;
    model: string;
    model_variant: string | null;
    queue_id: string;
    plan_first: boolean;
    visibility: 'all' | 'selected';
    user_ids: string[];
    next_task_ids: string[];
    is_published?: boolean;
    updated_at?: string;
}

export interface AdminUser {
    id: string;
    login: string;
    status: string;
    role: string;
    tasks_only: number | boolean;
}

export interface ModelOption {
    id: string;
    name: string;
    provider: string;
}

export interface Options {
    agents: string[];
    default_agent: string;
    default_model: string;
    models: ModelOption[];
}

export interface QueueRow {
    id: string;
    name: string;
    priority: number;
    max_workers: number;
    status: string;
    is_system: number | boolean;
    created_at: string;
    updated_at: string;
    updated_by?: string | null;
    active: number;
    queued: number;
}

export interface QueueJob {
    id: string;
    status: string;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    effective_login: string | null;
    chat_title: string | null;
    content: string | null;
    task_id: string | null;
    task_title: string | null;
    consultation: number | boolean;
    queue_id: string | null;
    queue_name: string | null;
}

export interface QueueOverview {
    limit: number;
    env_limit: number;
    hard_limit: number;
    active: number;
    queued: number;
    default_chat_queue_id: string;
    queues: QueueRow[];
    jobs: QueueJob[];
}

export interface ConsultationSettings {
    model: string;
    queue_id: string;
}

export type RunOutcome = 'completed' | 'failed' | 'cancelled' | null;

export interface StatRun {
    id: string;
    kind: 'question' | 'task';
    task_id: string | null;
    task_title: string | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    outcome: RunOutcome;
    /** Login of the run's owner (the chat's user); null if the user row is gone. */
    login: string | null;
}

export interface StatRunsResponse {
    items: StatRun[];
    generated_at: string;
}

export const isOn = (v: number | boolean | null | undefined): boolean => !!v;

/** Endpoints used by «Статистика», «Настройка задач» and «Очередь». */
@Injectable({ providedIn: 'root' })
export class AdminConfigApi {
    private readonly api = inject(ApiService);

    tasks(): Promise<AdminTask[]> {
        return this.api.get<AdminTask[]>('/api/admin/tasks');
    }

    createTask(body: TaskPayload): Promise<AdminTask> {
        return this.api.post<AdminTask>('/api/admin/tasks', body);
    }

    patchTask(id: string, body: Partial<TaskPayload>): Promise<AdminTask> {
        return this.api.patch<AdminTask>(`/api/admin/tasks/${encodeURIComponent(id)}`, body);
    }

    users(): Promise<AdminUser[]> {
        return this.api.get<AdminUser[]>('/api/admin/users');
    }

    options(): Promise<Options> {
        return this.api.get<Options>('/api/options');
    }

    queue(): Promise<QueueOverview> {
        return this.api.get<QueueOverview>('/api/admin/queue');
    }

    stopJob(id: string): Promise<unknown> {
        return this.api.post(`/api/admin/jobs/${encodeURIComponent(id)}/stop`);
    }

    createQueue(body: { name: string; priority: number; max_workers: number }): Promise<QueueRow> {
        return this.api.post<QueueRow>('/api/admin/queues', body);
    }

    patchQueue(id: string, body: { name: string; priority: number; max_workers: number; updated_at: string }): Promise<QueueRow> {
        return this.api.patch<QueueRow>(`/api/admin/queues/${encodeURIComponent(id)}`, body);
    }

    archiveQueue(id: string): Promise<void> {
        return this.api.delete(`/api/admin/queues/${encodeURIComponent(id)}`);
    }

    setConcurrency(value: number): Promise<unknown> {
        return this.api.patch('/api/admin/settings/concurrency', { value });
    }

    resetConcurrency(): Promise<void> {
        return this.api.delete('/api/admin/settings/concurrency/override');
    }

    setDefaultChatQueue(queueId: string): Promise<unknown> {
        return this.api.put('/api/admin/settings/default-chat-queue', { queue_id: queueId });
    }

    consultations(): Promise<ConsultationSettings> {
        return this.api.get<ConsultationSettings>('/api/admin/settings/consultations');
    }

    setConsultations(body: ConsultationSettings): Promise<ConsultationSettings> {
        return this.api.put<ConsultationSettings>('/api/admin/settings/consultations', body);
    }

    statRuns(params: { from: string; to: string; type: string | null }): Promise<StatRunsResponse> {
        return this.api.get<StatRunsResponse>('/api/admin/statistics/runs', {
            params: { from: params.from, to: params.to, type: params.type || undefined },
        });
    }
}
