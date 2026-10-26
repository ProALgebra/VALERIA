/** Subset of OpenCode message shapes returned by `GET /api/chats/:id/messages` and `opencode.event`. */
export interface OcToolState {
    status?: 'pending' | 'running' | 'completed' | 'error' | string;
    input?: { filePath?: string; path?: string; pattern?: string; command?: string; description?: string; [k: string]: unknown };
    title?: string;
    output?: string;
    error?: string;
    metadata?: {
        matches?: number;
        count?: number;
        exit?: number;
        filediff?: { file?: string; additions?: number; deletions?: number };
        [k: string]: unknown;
    };
    time?: { start?: number; end?: number };
}

export interface OcPart {
    id: string;
    messageID?: string;
    sessionID?: string;
    type: 'text' | 'reasoning' | 'tool' | 'step-start' | 'step-finish' | string;
    text?: string;
    synthetic?: boolean;
    tool?: string;
    callID?: string;
    state?: OcToolState;
}

export interface OcMessageInfo {
    id: string;
    sessionID?: string;
    role: 'user' | 'assistant';
    agent?: string;
    mode?: string;
    parentID?: string;
    model?: { providerID?: string; modelID?: string };
    providerID?: string;
    modelID?: string;
    time?: { created?: number; completed?: number };
    error?: { name?: string; data?: { message?: string } } | null;
}

export interface OcMessage {
    info: OcMessageInfo;
    parts: OcPart[];
}

/** `provider/model` of a message, wherever OpenCode put it. */
export function messageModel(info: OcMessageInfo): string {
    const provider = info.model?.providerID ?? info.providerID;
    const model = info.model?.modelID ?? info.modelID;
    if (!model) return '';
    return provider ? `${provider}/${model}` : model;
}

/** Plain text of a message: its non-synthetic text parts. */
export function messageText(message: OcMessage): string {
    return message.parts
        .filter((p) => p.type === 'text' && !p.synthetic && p.text)
        .map((p) => p.text as string)
        .join('\n\n')
        .trim();
}
