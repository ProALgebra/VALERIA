/**
 * Human labels for `audit_log.action` (API.md §3.3) and the «Действие» filter categories.
 * The first 12 labels and the 5 categories are from the prototype; the rest are written in the same voice.
 */

export type AuditCategory = 'auth' | 'users' | 'tasks' | 'queues' | 'imp';

export const AUDIT_CATEGORIES: readonly { id: AuditCategory; name: string }[] = [
    { id: 'auth', name: 'Входы' },
    { id: 'users', name: 'Пользователи' },
    { id: 'tasks', name: 'Задачи и запуски' },
    { id: 'queues', name: 'Очереди и лимиты' },
    { id: 'imp', name: 'Действия за пользователя' },
];

/** code → [label, category]. Category null: shown only under «Все действия». */
export const AUDIT_ACTIONS: Readonly<Record<string, readonly [string, AuditCategory | null]>> = {
    // Prototype
    'auth.login': ['Вошёл в VALERIA', 'auth'],
    'user.create': ['Создал пользователя', 'users'],
    'user.update': ['Изменил пользователя', 'users'],
    'user.password_reset': ['Сбросил пароль', 'users'],
    'task.update': ['Изменил задачу', 'tasks'],
    'task.run': ['Запустил задачу', 'tasks'],
    'task_access.update': ['Изменил доступ к задачам', 'tasks'],
    'queue.update': ['Изменил очередь', 'queues'],
    'concurrency.update': ['Изменил общий лимит', 'queues'],
    'job.stop.request': ['Остановил задание', 'queues'],
    'permission.admin_response': ['Ответил на запрос разрешения', 'imp'],
    'consultation.admin_view': ['Открыл вопрос пользователя', 'imp'],
    // API.md §3.3, not in the prototype
    'admin.bootstrap': ['Создал первого администратора', 'users'],
    'user.profile_update': ['Изменил свой профиль', 'users'],
    'task.create': ['Создал задачу', 'tasks'],
    'task.delete': ['Удалил задачу', 'tasks'],
    'task.audience.update': ['Изменил доступ к задаче', 'tasks'],
    'task.pipeline.run': ['Запустил следующую задачу', 'tasks'],
    'task_run.result': ['Открыл итог запуска', 'tasks'],
    'template.create': ['Создал шаблон', 'tasks'],
    'template.update': ['Изменил шаблон', 'tasks'],
    'template.delete': ['Удалил шаблон', 'tasks'],
    'job.stop.completed': ['Задание остановлено', 'queues'],
    'job.stop.retry': ['Повтор остановки задания', 'queues'],
    'queue.create': ['Создал очередь', 'queues'],
    'queue.archive': ['Архивировал очередь', 'queues'],
    'queue.chat-default.update': ['Изменил очередь для чатов', 'queues'],
    'concurrency.reset': ['Сбросил общий лимит', 'queues'],
    'consultation.settings.update': ['Изменил настройки вопросов', 'queues'],
    'task_run.messages': ['Открыл запуск пользователя', 'imp'],
    'chats.admin_list': ['Открыл чаты пользователя', 'imp'],
    'task_runs.admin_list': ['Открыл запуски пользователя', 'imp'],
    'task_access.admin_list': ['Открыл доступ пользователя к задачам', 'imp'],
    'message.impersonated': ['Написал от имени пользователя', 'imp'],
    'chat.admin_view': ['Открыл чат пользователя', 'imp'],
    'question.reply': ['Ответил на уточнение', null],
    'consultation.question.reply': ['Ответил на уточнение в вопросе', null],
    'chat.archive': ['Архивировал чат', null],
    'chat.unarchive': ['Вернул чат из архива', null],
    'chat.snooze': ['Отложил чат', null],
    'chat.unsnooze': ['Вернул отложенный чат', null],
    'chat.delete': ['Удалил чат', null],
};

/** Label for an action code; unknown codes are shown as is. */
export function auditLabel(code: string): string {
    return AUDIT_ACTIONS[code]?.[0] ?? code;
}

/** Action codes of a category, for `GET /api/admin/audit?action=a,b`. */
export function auditCategoryCodes(category: AuditCategory): string[] {
    return Object.entries(AUDIT_ACTIONS)
        .filter(([, [, cat]]) => cat === category)
        .map(([code]) => code);
}

export function isAuditCategory(value: string | null | undefined): value is AuditCategory {
    return AUDIT_CATEGORIES.some((c) => c.id === value);
}
