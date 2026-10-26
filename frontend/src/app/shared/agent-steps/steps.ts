import { OcPart } from './opencode.model';

export type StepIcon = 'read' | 'search' | 'edit' | 'run' | 'tool';

/** One line under an agent message: «Прочитал 6 файлов · conanfile.py, …». */
export interface AgentStep {
    id: string;
    icon: StepIcon;
    text: string;
    sub: string;
    running: boolean;
    failed: boolean;
}

/** Content of an assistant message in order: grouped tool steps and markdown text blocks. */
export type MessageBlock = { kind: 'steps'; steps: AgentStep[] } | { kind: 'text'; id: string; text: string };

/** Russian plural: plural(5, 'файл', 'файла', 'файлов') → 'файлов'. */
export function plural(n: number, one: string, few: string, many: string): string {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
}

/** «2 мин 14 с», «45 с», «1 ч 3 мин». */
export function formatDuration(ms: number): string {
    const total = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h) return m ? `${h} ч ${m} мин` : `${h} ч`;
    if (m) return s ? `${m} мин ${s} с` : `${m} мин`;
    return `${s} с`;
}

function filePathOf(part: OcPart): string {
    const s = part.state;
    return String(s?.metadata?.filediff?.file ?? s?.input?.filePath ?? s?.input?.path ?? s?.title ?? '').trim();
}

function dirOf(path: string): string {
    const i = path.lastIndexOf('/');
    return i > 0 ? path.slice(0, i) : '';
}

function baseOf(path: string): string {
    return path.slice(path.lastIndexOf('/') + 1);
}

function listWithRest(names: string[], shown = 3): string {
    const unique = [...new Set(names.filter(Boolean))];
    if (unique.length <= shown) return unique.join(', ');
    return `${unique.slice(0, shown).join(', ')}, ещё ${unique.length - shown}`;
}

function toolKind(part: OcPart): StepIcon | null {
    switch (part.tool) {
        case 'read':
            return 'read';
        case 'grep':
        case 'glob':
        case 'list':
        case 'codesearch':
            return 'search';
        case 'edit':
        case 'write':
        case 'patch':
        case 'multiedit':
            return 'edit';
        case 'bash':
            return 'run';
        default:
            return part.tool ? 'tool' : null;
    }
}

const isRunning = (p: OcPart) => p.state?.status === 'running' || p.state?.status === 'pending';
const isFailed = (p: OcPart) => p.state?.status === 'error';

function readStep(group: OcPart[]): AgentStep {
    const files = [...new Set(group.map(filePathOf).filter(Boolean))];
    const n = files.length || group.length;
    return {
        id: group[0].id,
        icon: 'read',
        text: n === 1 && files[0] ? `Прочитал ${files[0]}` : `Прочитал ${n} ${plural(n, 'файл', 'файла', 'файлов')}`,
        sub: n === 1 ? '' : listWithRest(files),
        running: group.some(isRunning),
        failed: false,
    };
}

function searchStep(group: OcPart[]): AgentStep {
    const patterns = [...new Set(group.map((p) => String(p.state?.input?.pattern ?? '').trim()).filter(Boolean))];
    const counts = group.map((p) => p.state?.metadata?.matches ?? p.state?.metadata?.count);
    const known = counts.filter((c): c is number => typeof c === 'number');
    const quoted = patterns.map((p) => `«${p}»`).join(', ');
    let sub = quoted;
    if (known.length && !group.some(isRunning)) {
        const total = known.reduce((a, b) => a + b, 0);
        sub = `${quoted ? `${quoted}: ` : ''}${total} ${plural(total, 'совпадение', 'совпадения', 'совпадений')}`;
    }
    return { id: group[0].id, icon: 'search', text: 'Искал в репозитории', sub, running: group.some(isRunning), failed: false };
}

function diffText(part: OcPart): string {
    const d = part.state?.metadata?.filediff;
    if (!d || (d.additions === undefined && d.deletions === undefined)) return '';
    return `+${d.additions ?? 0} −${d.deletions ?? 0}`;
}

function editSteps(group: OcPart[]): AgentStep[] {
    // Consecutive edits in the same folder collapse into «Изменил 2 файла в profiles».
    const out: AgentStep[] = [];
    let i = 0;
    while (i < group.length) {
        const dir = dirOf(filePathOf(group[i]));
        let j = i + 1;
        while (dir && j < group.length && dirOf(filePathOf(group[j])) === dir) j++;
        const run = group.slice(i, j);
        const files = [...new Set(run.map(filePathOf))];
        if (files.length > 1) {
            out.push({
                id: run[0].id,
                icon: 'edit',
                text: `Изменил ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')} в ${dir}`,
                sub: listWithRest(files.map(baseOf)),
                running: run.some(isRunning),
                failed: run.some(isFailed),
            });
        } else {
            const p = run[run.length - 1];
            out.push({
                id: run[0].id,
                icon: 'edit',
                text: isRunning(p) ? `Изменяет ${files[0] || 'файл'}` : `Изменил ${files[0] || 'файл'}`,
                sub: isFailed(p) ? 'не удалось' : diffText(p),
                running: isRunning(p),
                failed: isFailed(p),
            });
        }
        i = j;
    }
    return out;
}

function runStep(part: OcPart): AgentStep {
    const s = part.state;
    const command = String(s?.input?.command ?? s?.title ?? '').trim();
    const running = isRunning(part);
    const exit = s?.metadata?.exit;
    const failed = isFailed(part) || (typeof exit === 'number' && exit !== 0);
    const took = s?.time?.start && s?.time?.end ? formatDuration(s.time.end - s.time.start) : '';
    let sub = '';
    if (!running) {
        if (failed) sub = typeof exit === 'number' ? `ошибка, код ${exit}` : 'ошибка';
        else sub = took ? `успешно, ${took}` : 'успешно';
    }
    return { id: part.id, icon: 'run', text: `${running ? 'Выполняет' : 'Выполнил'} ${command}`, sub, running, failed };
}

function otherStep(part: OcPart): AgentStep {
    const title = String(part.state?.title ?? '').trim();
    return {
        id: part.id,
        icon: 'tool',
        text: title || String(part.tool),
        sub: title ? String(part.tool) : '',
        running: isRunning(part),
        failed: isFailed(part),
    };
}

/** Steps for a run of consecutive tool parts: reads and searches are grouped, edits and commands go one by one. */
export function toolPartsToSteps(parts: OcPart[]): AgentStep[] {
    const steps: AgentStep[] = [];
    let i = 0;
    while (i < parts.length) {
        const kind = toolKind(parts[i]);
        if (kind === 'read' || kind === 'search' || kind === 'edit') {
            let j = i + 1;
            while (j < parts.length && toolKind(parts[j]) === kind) j++;
            const group = parts.slice(i, j);
            if (kind === 'read') steps.push(readStep(group));
            else if (kind === 'search') steps.push(searchStep(group));
            else steps.push(...editSteps(group));
            i = j;
        } else {
            if (kind === 'run') steps.push(runStep(parts[i]));
            else if (kind === 'tool') steps.push(otherStep(parts[i]));
            i++;
        }
    }
    return steps;
}

/**
 * Assistant message parts → blocks in display order. Tool parts between text parts form one steps block;
 * `step-start`/`step-finish`, reasoning and synthetic text are not shown.
 */
export function partsToBlocks(parts: OcPart[]): MessageBlock[] {
    const blocks: MessageBlock[] = [];
    let tools: OcPart[] = [];
    const flush = () => {
        if (!tools.length) return;
        const steps = toolPartsToSteps(tools);
        if (steps.length) blocks.push({ kind: 'steps', steps });
        tools = [];
    };
    for (const part of parts) {
        if (part.type === 'tool') {
            tools.push(part);
        } else if (part.type === 'text' && !part.synthetic && part.text?.trim()) {
            flush();
            blocks.push({ kind: 'text', id: part.id, text: part.text });
        }
    }
    flush();
    return blocks;
}
