// Minimal i18n-ready strings module.
//
// Full Crowdin wiring is out of scope (spec §5.7 / §6.2). This provides a
// single dictionary keyed by string id and a `t()` lookup, structured so a
// second locale can be dropped in later without touching call sites.

export type Locale = 'en' | 'zh-Hans';

type Dict = Record<string, string>;

const en: Dict = {
  'app.name': 'MangaCouch',
  'nav.library': 'Library',
  'nav.downloads': 'Downloads',
  'nav.settings': 'Settings',
  'nav.lock': 'Lock',

  'auth.title': 'MangaCouch',
  'auth.subtitle': 'Enter your passcode to continue',
  'auth.passcode': 'Passcode',
  'auth.unlock': 'Unlock',
  'auth.unlocking': 'Unlocking…',
  'auth.error': 'Incorrect passcode',
  'auth.locked': 'Locked',

  'library.search': 'Search… (namespace:value, comma-separated)',
  'library.sort.title': 'Title',
  'library.sort.date_added': 'Date added',
  'library.sort.lastread': 'Last read',
  'library.allCategories': 'All categories',
  'library.random': 'Random',
  'library.newonly': 'New only',
  'library.empty': 'No archives found.',
  'library.loadMore': 'Load more',
  'library.pages': 'pages',
  'library.read': 'Read',
  'library.loading': 'Loading…',
  'library.results': 'results',

  'detail.read': 'Read',
  'detail.continue': 'Continue',
  'detail.download': 'Source',
  'detail.delete': 'Delete',
  'detail.tags': 'Tags',
  'detail.language': 'Language',
  'detail.rating': 'Rating',
  'detail.pages': 'Pages',
  'detail.comments': 'Comments',
  'detail.preview': 'Preview',
  'detail.similar': 'Similar',
  'detail.sameSeries': 'Same series',
  'detail.favorites': 'Favorites',
  'detail.addFavList': 'New list…',
  'detail.noComments': 'No comments.',
  'detail.confirmDelete': 'Delete this archive permanently?',

  'reader.loading': 'Loading…',
  'reader.failed': 'Failed to load',
  'reader.retry': 'Retry',
  'reader.loadAll': 'Load all images',
  'reader.settings': 'Reader settings',
  'reader.mode': 'Mode',
  'reader.mode.paged': 'Paged',
  'reader.mode.scroll': 'Webtoon',
  'reader.direction': 'Direction',
  'reader.direction.ltr': 'Left → Right',
  'reader.direction.rtl': 'Right → Left (manga)',
  'reader.double': 'Double page',
  'reader.fit': 'Fit',
  'reader.fit.width': 'Width',
  'reader.fit.height': 'Height',
  'reader.fit.container': 'Screen',
  'reader.fit.original': 'Original',
  'reader.fullscreen': 'Fullscreen',
  'reader.autoplay': 'Autoplay',
  'reader.preload': 'Preload',
  'reader.bookmark': 'Bookmark',
  'reader.bookmarks': 'Bookmarks',
  'reader.page': 'Page',
  'reader.close': 'Close',
  'reader.theme': 'Theme',

  'downloads.title': 'Downloads',
  'downloads.url': 'Gallery URL (e-hentai / exhentai)',
  'downloads.submit': 'Download',
  'downloads.checkBalance': 'Check GP',
  'downloads.balance': 'GP balance',
  'downloads.cost': 'Cost',
  'downloads.jobs': 'Jobs',
  'downloads.noJobs': 'No download jobs.',
  'downloads.priority': 'Priority',
  'downloads.state': 'State',

  'settings.title': 'Settings',
  'settings.config': 'Configuration',
  'settings.save': 'Save',
  'settings.scan': 'Scan library',
  'settings.regen': 'Regenerate thumbnails',
  'settings.upload': 'Upload archive',
  'settings.plugins': 'Plugins',
  'settings.theme': 'Theme',
  'settings.theme.dark': 'Dark',
  'settings.theme.light': 'Light',
  'settings.autolock': 'Auto-lock idle timeout',
  'settings.autolock.off': 'Off',
  'settings.minutes': 'min',

  'common.cancel': 'Cancel',
  'common.save': 'Save',
  'common.close': 'Close',
  'common.retry': 'Retry',
  'common.error': 'Something went wrong.',
  'common.owner': 'owner',
  'common.reader': 'reader',
  'common.back': 'Back',
};

const dictionaries: Record<Locale, Dict> = {
  en,
  // Stub second locale — falls through to English for missing keys.
  'zh-Hans': {
    'nav.library': '书库',
    'nav.downloads': '下载',
    'nav.settings': '设置',
    'nav.lock': '锁定',
    'auth.unlock': '解锁',
    'reader.loadAll': '加载全部图片',
    'reader.retry': '重试',
  },
};

let current: Locale = 'en';

export function setLocale(locale: Locale): void {
  current = locale;
}

export function getLocale(): Locale {
  return current;
}

export function t(key: string, vars?: Record<string, string | number>): string {
  const dict = dictionaries[current];
  let str = dict[key] ?? en[key] ?? key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      str = str.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
    }
  }
  return str;
}
