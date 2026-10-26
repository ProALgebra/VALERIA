import { EnvironmentProviders, Provider } from '@angular/core';
import { KbqAlertColors, KbqAlertStyles } from '@koobiq/components/alert';
import { KbqBadgeColors } from '@koobiq/components/badge';
import { KbqButtonStyles } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';

/**
 * Initialises the Koobiq enums that components keep as class fields (`colors = KbqComponentColors`)
 * before any spec loads.
 *
 * Vitest's SSR transform copies an import used directly as a class field value into a module-level
 * `const`, taken when the chunk loads. In the test bundle esbuild initialises modules lazily, so
 * depending on how specs share chunks that copy could be taken before the Koobiq module ran and
 * stay `undefined` (`Cannot read properties of undefined (reading 'Contrast')`). This file is bundled
 * into the test-bed init that runs before every spec file, which forces the initialisation first.
 */
const koobiqEnums = [KbqComponentColors, PopUpPlacements, KbqBadgeColors, KbqAlertColors, KbqAlertStyles, KbqButtonStyles];
if (koobiqEnums.some((e) => !e)) {
    throw new Error('Koobiq enums are not initialised');
}

const providers: (Provider | EnvironmentProviders)[] = [];
export default providers;
