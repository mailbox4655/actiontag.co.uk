export interface LocaleInfo {
  code: string;
  /** Language name in its own language (endonym) — shown in the switcher */
  name: string;
  dir: 'ltr' | 'rtl';
  /** BCP-47 tag used for <html lang> and hreflang */
  hreflang: string;
}

export const locales: LocaleInfo[] = [
  { code: 'en', name: 'English',    dir: 'ltr', hreflang: 'en' },
  { code: 'es', name: 'Español',    dir: 'ltr', hreflang: 'es' },
  { code: 'fr', name: 'Français',   dir: 'ltr', hreflang: 'fr' },
  { code: 'de', name: 'Deutsch',    dir: 'ltr', hreflang: 'de' },
  { code: 'pt', name: 'Português',  dir: 'ltr', hreflang: 'pt' },
  { code: 'it', name: 'Italiano',   dir: 'ltr', hreflang: 'it' },
  { code: 'nl', name: 'Nederlands', dir: 'ltr', hreflang: 'nl' },
  { code: 'pl', name: 'Polski',     dir: 'ltr', hreflang: 'pl' },
  { code: 'cs', name: 'Čeština',    dir: 'ltr', hreflang: 'cs' },
  { code: 'sk', name: 'Slovenčina', dir: 'ltr', hreflang: 'sk' },
  { code: 'ro', name: 'Română',     dir: 'ltr', hreflang: 'ro' },
  { code: 'hu', name: 'Magyar',     dir: 'ltr', hreflang: 'hu' },
  { code: 'bg', name: 'Български',  dir: 'ltr', hreflang: 'bg' },
  { code: 'el', name: 'Ελληνικά',   dir: 'ltr', hreflang: 'el' },
  { code: 'hr', name: 'Hrvatski',   dir: 'ltr', hreflang: 'hr' },
  { code: 'sl', name: 'Slovenščina',dir: 'ltr', hreflang: 'sl' },
  { code: 'lt', name: 'Lietuvių',   dir: 'ltr', hreflang: 'lt' },
  { code: 'et', name: 'Eesti',      dir: 'ltr', hreflang: 'et' },
  { code: 'no', name: 'Norsk',      dir: 'ltr', hreflang: 'no' },
  { code: 'sv', name: 'Svenska',    dir: 'ltr', hreflang: 'sv' },
  { code: 'da', name: 'Dansk',      dir: 'ltr', hreflang: 'da' },
  { code: 'fi', name: 'Suomi',      dir: 'ltr', hreflang: 'fi' },
  { code: 'ar', name: 'العربية',    dir: 'rtl', hreflang: 'ar' },
];

export const localeCodes = locales.map((l) => l.code);

export function getLocale(code: string): LocaleInfo {
  const found = locales.find((l) => l.code === code);
  if (!found) throw new Error(`Unknown locale: ${code}`);
  return found;
}

const catalogs = import.meta.glob<{ default: Record<string, any> }>('./catalog/*.json', {
  eager: true,
});

/**
 * Returns the translation catalog for a locale, deep-falling back to English
 * for any key a translation file doesn't provide yet.
 */
export function getCatalog(code: string): Record<string, any> {
  const en = catalogs['./catalog/en.json']?.default;
  if (!en) throw new Error('English master catalog missing');
  const target = catalogs[`./catalog/${code}.json`]?.default;
  if (!target || code === 'en') return en;
  return deepMerge(en, target);
}

function deepMerge(base: any, override: any): any {
  if (Array.isArray(base) || typeof base !== 'object' || base === null) {
    return override ?? base;
  }
  const out: Record<string, any> = {};
  for (const key of Object.keys(base)) {
    out[key] = key in (override ?? {}) ? deepMerge(base[key], override[key]) : base[key];
  }
  return out;
}
