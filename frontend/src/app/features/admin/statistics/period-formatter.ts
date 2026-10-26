import { Injectable } from '@angular/core';
import { DateFormatter } from '@koobiq/components/core';

/**
 * The «Период» chip shows a custom range the way the prototype does, both ends in full:
 * «18 окт, 00:00 — 18 окт, 23:59». Koobiq's default collapses a same-day range to «00:00–23:59, 18 окт».
 */
@Injectable()
export class PeriodChipFormatter<D> extends DateFormatter<D> {
    override rangeShortDateTime(startDate: D | null, endDate?: D | null): string {
        if (!startDate || !endDate) return super.rangeShortDateTime(startDate, endDate);
        return `${this.absoluteShortDateTime(startDate)} — ${this.absoluteShortDateTime(endDate)}`;
    }
}
