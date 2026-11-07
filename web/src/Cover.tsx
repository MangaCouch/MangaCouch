import { coverUrl } from "./pages";

/** Book cover; pass the content hash so the URL is versioned and immutable-cached. */
export function Cover({
  id,
  sha256,
  alt = "",
}: {
  id: string;
  sha256?: string;
  alt?: string;
}) {
  return (
    <img
      key={id}
      src={coverUrl(id, sha256)}
      alt={alt}
      loading="lazy"
      decoding="async"
      onLoad={(event) => {
        event.currentTarget.style.visibility = "visible";
      }}
      onError={(event) => {
        event.currentTarget.style.visibility = "hidden";
      }}
    />
  );
}
