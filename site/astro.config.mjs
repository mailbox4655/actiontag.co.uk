// @ts-check
import { defineConfig } from 'astro/config';

// All locales are pre-rendered to static HTML at build time (SEO requirement).
export const LOCALES = [
  'en', 'es', 'fr', 'de', 'pt', 'it', 'nl', 'pl',
  'cs', 'sk', 'ro', 'hu', 'bg', 'el', 'hr', 'sl',
  'lt', 'et', 'no', 'sv', 'da', 'fi', 'ar',
];

export default defineConfig({
  site: 'https://actiontag.co.uk',
  trailingSlash: 'always',
  i18n: {
    locales: LOCALES,
    defaultLocale: 'en',
    routing: {
      prefixDefaultLocale: true, // keeps /en/ URLs identical to the old site
      redirectToDefaultLocale: false,
    },
  },
});
