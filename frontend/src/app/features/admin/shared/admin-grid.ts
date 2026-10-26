import { DOCUMENT, NgTemplateOutlet } from '@angular/common';
import {
    booleanAttribute,
    ChangeDetectionStrategy,
    Component,
    computed,
    contentChildren,
    Directive,
    effect,
    inject,
    input,
    output,
    signal,
    TemplateRef,
    untracked,
} from '@angular/core';
import { KbqAgGridTheme, KbqAgGridToNextRowByTab } from '@koobiq/ag-grid-angular-theme';
import { AgGridAngular, ICellRendererAngularComp } from 'ag-grid-angular';
import {
    CellKeyDownEvent,
    CellStyleModule,
    ClientSideRowModelModule,
    ColDef,
    ColumnApiModule,
    ColumnAutoSizeModule,
    FullWidthCellKeyDownEvent,
    GetRowIdParams,
    GridApi,
    ICellRendererParams,
    IRowNode,
    LocaleModule,
    ModuleRegistry,
    ProcessRowParams,
    RowApiModule,
    RowAutoHeightModule,
    RowClassParams,
    RowClickedEvent,
    RowSelectionModule,
    RowSelectionOptions,
    RowStyleModule,
    SelectionChangedEvent,
    SuppressKeyboardEventParams,
} from 'ag-grid-community';

/** Only what the admin grids use, instead of `AllCommunityModule` (sorting, filters, editors, export are not used). */
ModuleRegistry.registerModules([
    ClientSideRowModelModule,
    RowSelectionModule,
    RowAutoHeightModule,
    RowStyleModule,
    CellStyleModule,
    ColumnAutoSizeModule,
    // The Koobiq theme directives call `isPinning` and `getDisplayedRowCount`; selection sync walks the rows.
    ColumnApiModule,
    RowApiModule,
    LocaleModule,
]);

/**
 * How a column lays its cell out (see admin-grid.scss):
 * - `text`: wraps, the row grows with it; the first line is centred like a single-line row.
 * - `nowrap`: one line, the column is sized to its content (dates, logins).
 * - `line`: inline content such as a badge, centred on the first text line of the row, sized to its content.
 * - `num`: a number, one line, right-aligned with its header, sized to its content.
 * - `action`: buttons, centred in the row and right-aligned, as in the old tables.
 * - `control`: form fields (`editable` grids), centred in a single-line row; the keys inside them are theirs.
 */
export type AdminGridColumnKind = 'text' | 'nowrap' | 'num' | 'line' | 'action' | 'control';

/** Columns sized to their content after every render. */
const AUTO_SIZED_KINDS: ReadonlySet<AdminGridColumnKind> = new Set(['nowrap', 'num', 'line']);

/** One column: `<ng-template vCol="id" header="Заголовок" let-row>…</ng-template>` inside `v-admin-grid`. */
@Directive({ selector: 'ng-template[vCol]' })
export class AdminGridColumnDirective {
    readonly id = input.required<string>({ alias: 'vCol' });
    readonly header = input('');
    readonly kind = input<AdminGridColumnKind>('text');
    readonly flex = input<number>();
    readonly width = input<number>();
    readonly minWidth = input<number>();
    readonly hidden = input(false, { transform: booleanAttribute });
    readonly template = inject<TemplateRef<{ $implicit: unknown }>>(TemplateRef);
}

interface TemplateCellParams extends ICellRendererParams {
    template: TemplateRef<{ $implicit: unknown }>;
}

/** Renders a screen's column template with the row as `$implicit`, so cells keep the screen's own markup. */
@Component({
    selector: 'v-admin-grid-cell',
    imports: [NgTemplateOutlet],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `<ng-container *ngTemplateOutlet="template(); context: context()" />`,
    host: { class: 'v-admin-grid__cell' },
})
export class AdminGridCellComponent implements ICellRendererAngularComp {
    protected readonly template = signal<TemplateRef<{ $implicit: unknown }> | null>(null);
    protected readonly context = signal<{ $implicit: unknown }>({ $implicit: null });

    agInit(params: TemplateCellParams): void {
        this.template.set(params.template);
        this.context.set({ $implicit: params.data });
    }

    refresh(params: TemplateCellParams): boolean {
        this.agInit(params);
        return true;
    }
}

/** Russian strings for what the grid announces itself (selection checkboxes, empty grid). */
const LOCALE_TEXT: Record<string, string> = {
    noRowsToShow: 'Нет строк',
    ariaChecked: 'отмечено',
    ariaUnchecked: 'не отмечено',
    ariaIndeterminate: 'отмечены не все',
    ariaRowToggleSelection: 'Отметить',
    ariaRowSelectAll: 'Отметить всех',
    ariaHeaderSelection: 'Отметить всех',
    ariaRowSelect: 'Пробел — отметить строку',
    ariaRowDeselect: 'Пробел — снять отметку',
    ariaRowSelectionDisabled: 'Строку нельзя отметить',
    ariaLabelSelectionColumn: 'Отметка',
};

/** Selection changes the user made; programmatic syncs (`api`, new row data) are not echoed back. */
const USER_SELECTION_SOURCES = new Set(['checkboxSelected', 'rowClicked', 'spaceKey', 'uiSelectAll', 'uiSelectAllFiltered', 'uiSelectAllCurrentPage']);

/** Clicks on these do their own thing and do not open the row. */
const OWN_CLICK_TARGETS = 'a, button, input, label, kbq-checkbox, .ag-selection-checkbox, [col-id="ag-Grid-SelectionColumn"]';

let gridStyles: Promise<void> | null = null;

/**
 * The grid theme is a stylesheet of its own (angular.json `admin-grid`, `inject: false`): ag-grid's CSS is large and
 * only the admin screens need it. It sits next to the chunks, so it resolves against this chunk's URL (`/static/` in
 * production). Resolves on error too: a grid without its theme is still usable.
 */
function loadGridStyles(doc: Document): Promise<void> {
    gridStyles ??= new Promise<void>((resolve) => {
        const link = doc.createElement('link');
        link.rel = 'stylesheet';
        link.href = new URL('admin-grid.css', import.meta.url).href;
        link.onload = link.onerror = () => resolve();
        doc.head.appendChild(link);
    });
    return gridStyles;
}

function escapeHtml(value: string): string {
    return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * The admin tables: ag-grid (community) with the official Koobiq theme, one look and behaviour for every screen.
 * Columns come from `vCol` templates; the grid takes the page height of its rows (`domLayout: autoHeight`), so the
 * page keeps scrolling as before. Rows open by click and by Enter; Tab moves between rows (`kbqAgGridToNextRowByTab`).
 */
@Component({
    selector: 'v-admin-grid',
    imports: [AgGridAngular, KbqAgGridTheme, KbqAgGridToNextRowByTab],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <!-- Created once the theme is in: ag-grid measures rows and columns with it. -->
        @if (ready()) {
        <ag-grid-angular
            kbqAgGridTheme
            kbqAgGridThemeDisableCellFocusStyles
            [kbqAgGridToNextRowByTab]="!editable()"
            class="v-admin-grid"
            [class.v-admin-grid_openable]="openable()"
            domLayout="autoHeight"
            [rowData]="rows()"
            [columnDefs]="columnDefs()"
            [defaultColDef]="defaultColDef"
            [getRowId]="getRowId"
            [getRowClass]="getRowClass"
            [processRowPostCreate]="processRowPostCreate"
            [rowSelection]="rowSelection()"
            [localeText]="localeText"
            [overlayNoRowsTemplate]="noRowsTemplate()"
            [loading]="false"
            [suppressHeaderFocus]="!selectable() || editable()"
            [suppressCellFocus]="editable()"
            [suppressColumnVirtualisation]="true"
            [suppressDragLeaveHidesColumns]="true"
            [ensureDomOrder]="true"
            [animateRows]="false"
            (gridReady)="onGridReady($event.api)"
            (rowDataUpdated)="onRowsRendered()"
            (firstDataRendered)="onRowsRendered()"
            (rowClicked)="onRowClicked($event)"
            (cellKeyDown)="onCellKeyDown($event)"
            (selectionChanged)="onSelectionChanged($event)"
        />
        }
    `,
    styles: `
        :host {
            display: block;
            min-width: 0;
        }
    `,
})
export class AdminGridComponent<T = unknown> {
    readonly rows = input.required<T[]>();
    /** Stable id of a row: polling and new pages update rows in place. */
    readonly rowId = input.required<(row: T) => string>();
    /** Rows open by click and Enter (`rowOpen`). */
    readonly openable = input(false, { transform: booleanAttribute });
    /** Id of the row whose details are open (panel, card): highlighted and marked `aria-current`. */
    readonly current = input<string | null | undefined>(null);
    /** Checkbox selection with a «select all» header checkbox. */
    readonly selectable = input(false, { transform: booleanAttribute });
    /** Ids of the checked rows; rows that are filtered out keep their check. */
    readonly selected = input<ReadonlySet<string>>(new Set());
    /**
     * A table of form fields: the cells are not focusable, so Tab goes through the fields and buttons in page order,
     * and the grid does not take keys from them.
     */
    readonly editable = input(false, { transform: booleanAttribute });
    /** Shown inside the grid when there are no rows. */
    readonly noRowsText = input('');

    readonly rowOpen = output<T>();
    readonly selectedChange = output<ReadonlySet<string>>();

    private readonly columns = contentChildren(AdminGridColumnDirective);
    protected readonly ready = signal(false);
    private api: GridApi<T> | null = null;
    private lastCurrent: string | null = null;

    protected readonly localeText = LOCALE_TEXT;
    protected readonly defaultColDef: ColDef = {
        sortable: false,
        resizable: false,
        suppressMovable: true,
        suppressHeaderMenuButton: true,
    };

    protected readonly columnDefs = computed<ColDef<T>[]>(() =>
        this.columns().map((col) => {
            const kind = col.kind();
            const def: ColDef<T> = {
                colId: col.id(),
                headerName: col.header(),
                cellRenderer: AdminGridCellComponent,
                cellRendererParams: { template: col.template },
                cellClass: ['v-admin-grid__td', `v-admin-grid__td_${kind}`, ...(kind === 'num' ? ['ag-right-aligned-cell'] : [])],
                headerClass: kind === 'action' ? 'v-admin-grid__th_sr' : kind === 'num' ? 'ag-right-aligned-header' : undefined,
                hide: col.hidden(),
                autoHeight: kind !== 'action',
                wrapText: kind === 'text' || kind === 'control',
                flex: col.flex(),
                width: col.width(),
                minWidth: col.minWidth(),
            };
            if (kind === 'action') def.suppressKeyboardEvent = actionCellKeys;
            if (kind === 'control') def.suppressKeyboardEvent = controlCellKeys;
            return def;
        }),
    );

    protected readonly rowSelection = computed<RowSelectionOptions | undefined>(() =>
        this.selectable()
            ? { mode: 'multiRow', checkboxes: true, headerCheckbox: true, enableClickSelection: false, selectAll: 'all' }
            : undefined,
    );

    protected readonly noRowsTemplate = computed(() =>
        this.noRowsText() ? `<span class="v-admin-grid__empty">${escapeHtml(this.noRowsText())}</span>` : '<span></span>',
    );

    protected readonly getRowId = (p: GetRowIdParams<T>): string => this.rowId()(p.data);

    protected readonly getRowClass = (p: RowClassParams<T>): string | undefined =>
        p.data && this.isCurrent(p.data) ? 'v-admin-grid__tr_current' : undefined;

    protected readonly processRowPostCreate = (p: ProcessRowParams<T>): void => {
        if (p.node.data && this.isCurrent(p.node.data)) p.eRow.setAttribute('aria-current', 'true');
        else p.eRow.removeAttribute('aria-current');
    };

    constructor() {
        void loadGridStyles(inject(DOCUMENT)).then(() => this.ready.set(true));
        // The open row: re-draw the previous and the new one so their class and `aria-current` follow.
        effect(() => {
            const id = this.current() ?? null;
            untracked(() => this.redrawCurrent(id));
        });
        effect(() => {
            this.selected();
            untracked(() => this.syncSelection());
        });
    }

    protected onGridReady(api: GridApi<T>): void {
        this.api = api;
        this.syncSelection();
    }

    /** `nowrap`, `num` and `line` columns take the width of their content, the `flex` ones share the rest. */
    protected onRowsRendered(): void {
        const api = this.api;
        if (!api) return;
        this.syncSelection();
        const ids = this.columns()
            .filter((c) => AUTO_SIZED_KINDS.has(c.kind()) && !c.hidden())
            .map((c) => c.id());
        // Angular cell renderers render their template right after the grid created them.
        if (ids.length) requestAnimationFrame(() => !api.isDestroyed() && api.autoSizeColumns(ids, false));
    }

    protected onRowClicked(e: RowClickedEvent<T>): void {
        if (!this.openable() || !e.data) return;
        const target = e.event?.target as HTMLElement | null;
        if (target?.closest(OWN_CLICK_TARGETS)) return;
        // A text selection inside the row is not a click on it.
        if (window.getSelection()?.toString()) return;
        this.rowOpen.emit(e.data);
    }

    protected onCellKeyDown(e: CellKeyDownEvent<T> | FullWidthCellKeyDownEvent<T>): void {
        const ev = e.event as KeyboardEvent | null | undefined;
        if (!ev || ev.key !== 'Enter' || !e.data) return;
        const target = ev.target as HTMLElement;
        // Only the focused cell itself: Enter on a button or a link inside it is theirs.
        if (!target.classList.contains('ag-cell')) return;
        if (target.classList.contains('v-admin-grid__td_action')) {
            target.querySelector<HTMLElement>('button:not([disabled]), a[href]')?.focus();
            return;
        }
        if (this.openable()) this.rowOpen.emit(e.data);
    }

    protected onSelectionChanged(e: SelectionChangedEvent<T>): void {
        if (!USER_SELECTION_SOURCES.has(e.source)) return;
        const next = new Set(this.selected());
        e.api.forEachNode((node) => {
            if (!node.data) return;
            const id = this.rowId()(node.data);
            if (node.isSelected()) next.add(id);
            else next.delete(id);
        });
        this.selectedChange.emit(next);
    }

    private isCurrent(row: T): boolean {
        const id = this.current();
        return !!id && this.rowId()(row) === id;
    }

    private redrawCurrent(id: string | null): void {
        const api = this.api;
        const prev = this.lastCurrent;
        this.lastCurrent = id;
        if (!api || prev === id) return;
        const nodes = [prev, id]
            .map((x) => (x ? api.getRowNode(x) : undefined))
            .filter((n): n is IRowNode<T> => !!n);
        if (nodes.length) api.redrawRows({ rowNodes: nodes });
    }

    /** Puts the grid's checks in line with `selected` (after a reset, new rows or an outside change). */
    private syncSelection(): void {
        const api = this.api;
        if (!api || !this.selectable()) return;
        const selected = this.selected();
        const on: IRowNode<T>[] = [];
        const off: IRowNode<T>[] = [];
        api.forEachNode((node) => {
            if (!node.data) return;
            const want = selected.has(this.rowId()(node.data));
            if (want !== node.isSelected()) (want ? on : off).push(node);
        });
        if (on.length) api.setNodesSelected({ nodes: on, newValue: true, source: 'api' });
        if (off.length) api.setNodesSelected({ nodes: off, newValue: false, source: 'api' });
    }
}

/**
 * Action cells: the grid does not take Tab, so it moves through the buttons in the cells as in a page, and the
 * buttons get their Enter and Space themselves.
 */
function actionCellKeys(p: SuppressKeyboardEventParams): boolean {
    const key = p.event.key;
    if (key === 'Tab') return true;
    const target = p.event.target as HTMLElement;
    return (key === 'Enter' || key === ' ') && !target.classList.contains('ag-cell');
}

/** Form-field cells: every key typed inside a field (arrows, Enter, Space, Tab) belongs to the field, not the grid. */
function controlCellKeys(p: SuppressKeyboardEventParams): boolean {
    return p.event.key === 'Tab' || !(p.event.target as HTMLElement).classList.contains('ag-cell');
}

export const ADMIN_GRID = [AdminGridComponent, AdminGridColumnDirective] as const;
