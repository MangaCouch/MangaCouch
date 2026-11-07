export interface Tag {
  namespace: string;
  value: string;
}
export interface Source {
  site: "ehentai" | "exhentai" | "nhentai";
  gallery_id: number;
  gallery_token: string | null;
}
export interface SourceRating {
  average: number;
  count: number | null;
}
export interface SourceComment {
  source: Source;
  comment_id: string;
  author: string;
  body: string;
  posted_at: number | null;
  score: number | null;
  is_uploader: boolean;
}
export interface SourceNotes {
  source: Source;
  tags: Tag[];
  fetched_at: number;
  comments_sort: "source" | "newest";
  comments_pages: number;
  comments_complete: boolean;
}
/** Mirrors mangacouch.models.Metadata; the backend always returns every field. */
export interface Metadata {
  title: string | null;
  original_title: string | null;
  language: string | null;
  translated: boolean | null;
  work_type: string | null;
  tags: Tag[];
  source: Source | null;
  uploader: string | null;
  uploaded_at: string | null;
  source_rating: SourceRating | null;
  declared_page_count: number | null;
  declared_byte_size: number | null;
  parent: Source | null;
  newer_versions: Source[];
  field_sources: Record<string, string>;
  protected_fields: string[];
}
export interface Personal {
  rating: number | null;
  resume_page: number | null;
  completed: boolean;
  bookmarks: number[];
}
export type ReviewState = "needs_review" | "confirmed";
export interface Book {
  tag_counts?: { namespace: string; value: string; count: number }[];
  source_check?: {
    book_id: string;
    state: string;
    generation: number;
    error_code: string | null;
    reasons: string[];
  } | null;
  tag_translations?: {
    namespace: string;
    value: string;
    translation: string;
  }[];
  id: string;
  metadata: Metadata;
  metadata_revision: number;
  content: {
    sha256: string;
    original_filename: string;
    relative_path: string;
    format: string;
    byte_size: number;
    page_count: number;
  };
  personal: Personal;
  personal_revision: number;
  review_state: ReviewState;
  availability: string;
  sidecar: {
    pending_revision: number;
    synced_revision: number;
    last_error: string | null;
  };
}
export interface ReviewPreview {
  actual_page_count?: number;
  actual_byte_size?: number;
  compared_byte_size?: number | null;
  byte_size_basis?: "images" | "archive";
  token: string;
  state: string;
  metadata: Metadata;
  issues: string[];
  import_sources: string[];
  warnings: string[];
  ignored_fields: string[];
  candidates: Record<
    string,
    { metadata: Metadata; fields: string[]; decision: string | null }
  >;
  automatic: {
    rule: string;
    eligible: boolean;
    reasons: string[];
    evidence_event_id: number | null;
  };
  confirmation: {
    acknowledged_issues: string[];
    evidence_event_id?: number;
    checked_at?: string;
  } | null;
}
/** One side of a two-way sidecar conflict: the library record or the metadata file. */
export interface ConflictSide {
  schema: number;
  content_sha256: string;
  revision: number;
  metadata: Metadata;
}
/** A sidecar conflict; resolve with `{choice}` or `{fields}` using "library" | "file". */
export interface Conflict {
  id: string;
  book_id: string;
  fields: string[];
  token: string;
  library: ConflictSide;
  file: ConflictSide;
}
export type Lang = "zh" | "en";
export type Translate = (zh: string, en: string) => string;
export interface Collection {
  kind: "static" | "dynamic";
  rule?: { query: string; options: Record<string, unknown> } | null;
  id: string;
  name: string;
  revision: number;
  count: number;
}
export interface CollectionPage extends Collection {
  result_token?: string | null;
  items: { book_id: string; position: number; book: Book }[];
  after_position: number | null;
}
export interface CollectionContext extends Collection {
  index?: number;
  previous_id?: string | null;
  next_id?: string | null;
  continue_id: string | null;
  continue_state?: "unread" | "reading" | "completed";
  all_completed: boolean;
}
export interface CollectionNavigation {
  name: string;
  index?: number;
  count?: number;
  previous: boolean;
  next: boolean;
  busy: boolean;
  error: string;
  back: () => void;
  move: (direction: -1 | 1) => void;
}
export type BookTitle = {
  metadata: Pick<Metadata, "title" | "original_title">;
  content: Pick<Book["content"], "original_filename">;
};
export const title = (
  book: BookTitle,
  preferred: "title" | "original_title" = "title",
) =>
  book.metadata[preferred] ||
  book.metadata[preferred === "title" ? "original_title" : "title"] ||
  book.content.original_filename;

// ---- Added by frontend-reader-library (contract 3); merged into `Book` above. ----
export interface Book {
  /** `[width, height]` of every page; only on the full `GET /api/books/{id}` projection. */
  page_sizes?: [number, number][];
}
