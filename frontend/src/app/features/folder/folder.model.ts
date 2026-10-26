import { OcMessage, OcMessageInfo, OcPart } from '../../shared/agent-steps/opencode.model';

/** `GET /api/chats/:id` row. */
export interface FolderChat {
    id: string;
    title: string | null;
    status: string;
    kind?: string;
    workspace_snapshot: string | null;
    opencode_session_id: string | null;
    preferred_agent?: string | null;
    preferred_provider_id?: string | null;
    preferred_model_id?: string | null;
    created_at?: string;
    updated_at?: string;
}

/** OpenCode permission request (`GET /api/chats/:id/permissions`). */
export interface PermissionRequest {
    id: string;
    sessionID?: string;
    permission?: string;
    patterns?: string[];
    always?: string[];
    metadata?: { command?: string; description?: string; [k: string]: unknown };
}

export type PermissionResponse = 'once' | 'always' | 'reject';

export interface McpServer {
    name: string;
    status: string;
}

export interface ModelOption {
    id: string;
    name: string;
    provider: string;
}

/** `GET /api/options?chat_id=`. */
export interface FolderOptions {
    agents: string[];
    default_agent?: string;
    default_model: string;
    models: ModelOption[];
    selected_agent?: string;
    selected_model?: string;
}

/** What the screen shows for the chat right now (prototype folder states). */
export type FolderPhase = 'working' | 'permission' | 'clarify' | 'plan' | 'done' | 'empty';

const WORKING = ['queued', 'dispatching', 'running', 'cancelling'];

export function isFolderBusy(status: string): boolean {
    return WORKING.includes(status) || status === 'waiting_permission' || status === 'waiting_question';
}

export function folderPhase(status: string, messages: OcMessage[]): FolderPhase {
    if (status === 'waiting_permission') return 'permission';
    if (status === 'waiting_question') return 'clarify';
    if (WORKING.includes(status)) return 'working';
    const last = lastAssistant(messages);
    if (!last) return 'empty';
    return agentOf(last.info) === 'plan' ? 'plan' : 'done';
}

export function lastAssistant(messages: OcMessage[]): OcMessage | undefined {
    for (let i = messages.length - 1; i >= 0; i--) if (messages[i].info.role === 'assistant') return messages[i];
    return undefined;
}

export function agentOf(info: OcMessageInfo): string {
    return info.agent ?? info.mode ?? '';
}

/** «Plan» / «Build» for the mode line under a user message. */
export function agentLabel(agent: string): string {
    if (!agent) return '';
    return agent.charAt(0).toUpperCase() + agent.slice(1);
}

/**
 * «Состояние» in the folder aside, worded as in the prototype: «Работает», «Составляет план», «Ждёт вашего сообщения»
 * after a plan, «Готово». Queue, failure and cancel states keep the common status label (`fallback`).
 */
export function folderStateText(status: string, messages: OcMessage[], fallback: string): string {
    if (status === 'running') return workingText(messages);
    if (status === 'waiting_permission') return 'Ждёт разрешения';
    if (status === 'completed' || status === 'idle') {
        const phase = folderPhase(status, messages);
        if (phase === 'plan') return 'Ждёт вашего сообщения';
        if (phase === 'done') return 'Готово';
    }
    return fallback;
}

/** Status line text while the agent works: the plan agent «Составляет план», others «Работает». */
export function workingText(messages: OcMessage[]): string {
    const last = messages[messages.length - 1];
    return last && agentOf(last.info) === 'plan' ? 'Составляет план' : 'Работает';
}

/** Applies `message.updated`: replaces the info, keeps known parts. */
export function upsertMessageInfo(messages: OcMessage[], info: OcMessageInfo): OcMessage[] {
    const idx = messages.findIndex((m) => m.info.id === info.id);
    if (idx < 0) return [...messages, { info, parts: [] }];
    const next = messages.slice();
    next[idx] = { ...next[idx], info };
    return next;
}

/** Applies `message.part.updated`: replaces the part with the same id or appends it. Unknown message → unchanged. */
export function upsertPart(messages: OcMessage[], part: OcPart): OcMessage[] {
    const idx = messages.findIndex((m) => m.info.id === part.messageID);
    if (idx < 0) return messages;
    const message = messages[idx];
    const pi = message.parts.findIndex((p) => p.id === part.id);
    const parts = pi < 0 ? [...message.parts, part] : message.parts.map((p, i) => (i === pi ? part : p));
    const next = messages.slice();
    next[idx] = { ...message, parts };
    return next;
}

/** «Всегда разрешать <команда>»: the first `always` pattern without the trailing wildcard. */
export function alwaysLabel(request: PermissionRequest): string {
    const pattern = request.always?.[0] ?? request.patterns?.[0] ?? '';
    return pattern.replace(/\s*\*$/, '').trim();
}

export function mcpConnected(server: McpServer): boolean {
    return server.status === 'connected';
}
