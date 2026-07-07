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
  'detail.downloadFile': 'Download file',
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
  'detail.fetchMeta': 'Fetch metadata',
  'detail.fetchMeta.plugin': 'Source',
  'detail.fetchMeta.url': 'Gallery URL (optional)',
  'detail.fetchMeta.urlHint':
    'Paste a nhentai / hitomi / e-hentai gallery URL, or leave empty to auto-detect from the source tag, filename or title.',
  'detail.fetchMeta.mode': 'Tags',
  'detail.fetchMeta.merge': 'Merge with existing',
  'detail.fetchMeta.replace': 'Replace existing',
  'detail.fetchMeta.setTitle': 'Update title',
  'detail.fetchMeta.run': 'Fetch',
  'detail.fetchMeta.running': 'Fetching…',
  'detail.fetchMeta.added': 'tags added',
  'detail.fetchMeta.noNew': 'No new tags.',

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
  'reader.group.layout': 'Layout',
  'reader.group.display': 'Display',
  'reader.group.playback': 'Playback',

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
  'settings.subtitle': 'Server administration and client preferences',
  'settings.config': 'Configuration',
  'settings.save': 'Save',
  'settings.scan': 'Scan library',
  'settings.regen': 'Regenerate thumbnails',
  'settings.prewarm': 'Prewarm page thumbnails',
  'settings.prewarm.hint':
    'Pre-generate every page-grid thumbnail in the background so detail pages open instantly',
  'settings.upload': 'Upload archive',
  'settings.plugins': 'Plugins',
  'settings.theme': 'Theme',
  'settings.theme.dark': 'Dark',
  'settings.theme.light': 'Light',
  'settings.autolock': 'Auto-lock idle timeout',
  'settings.autolock.off': 'Off',
  'settings.minutes': 'min',
  'settings.maintenance': 'Maintenance',
  'settings.section.appearance': 'Appearance',
  'settings.section.appearance.sub': 'Theme and auto-lock',
  'settings.section.security': 'Security',
  'settings.section.security.sub': 'Owner and reader passcodes',
  'settings.section.library': 'Library',
  'settings.section.library.sub': 'Scan, thumbnails and uploads',
  'settings.section.plugins.sub': 'Metadata sources, downloaders and logins',
  'settings.section.advanced': 'Advanced',
  'settings.section.advanced.sub': 'Raw server configuration (JSON)',
  'settings.advanced.warning':
    'Edits the server configuration directly — invalid values can break downloads or scanning.',

  'plugins.type.metadata': 'Metadata sources',
  'plugins.type.download': 'Downloaders',
  'plugins.type.login': 'Logins',
  'plugins.type.script': 'Scripts',
  'plugins.configure': 'Configure',

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
