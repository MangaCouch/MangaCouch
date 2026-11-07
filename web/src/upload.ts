import type { Translate } from "./types";

export function prepareUpload(
  files: File[],
  maximum: number | null,
  t: Translate,
) {
  const archives = files.filter((file) => /\.(zip|cbz|pdf)$/i.test(file.name));
  if (files.length > 3 || archives.length !== 1)
    throw new Error(
      t(
        "请选择一本 ZIP、CBZ 或 PDF，可同时选择对应的 native/Eze JSON。",
        "Choose one ZIP, CBZ or PDF, optionally with its matching native/Eze JSON files.",
      ),
    );
  const archive = archives[0]!;
  if (maximum !== null && archive.size > maximum)
    throw new Error(
      t("文件超过上传上限。", "This file exceeds the upload limit."),
    );
  const names = {
    native: `${archive.name}.mangacouch.json`.normalize("NFC"),
    eze: `${archive.name}.eze.json`.normalize("NFC"),
  };
  const metadata = new Map<string, File>();
  for (const file of files) {
    if (file === archive) continue;
    const name = file.name.normalize("NFC");
    const kind =
      name === names.native
        ? "native"
        : name === names.eze ||
            name ===
              archive.name.replace(/\.[^.]+$/, ".eze.json").normalize("NFC")
          ? "eze"
          : null;
    if (!kind || metadata.has(kind))
      throw new Error(
        t(
          "JSON 文件名不匹配或重复。native 使用“本子文件名.mangacouch.json”，Eze 使用“同名.eze.json”。",
          "JSON names must match and cannot repeat: archive.ext.mangacouch.json for native, or archive.ext.eze.json for Eze.",
        ),
      );
    if (file.size > (kind === "native" ? 260 * 1024 : 2 * 1024 ** 2))
      throw new Error(
        t(
          "随书信息文件过大：native 上限 260 KiB，Eze 上限 2 MiB。",
          "Metadata is too large: native allows 260 KiB and Eze allows 2 MiB.",
        ),
      );
    metadata.set(kind, file);
  }
  let body: File | FormData = archive;
  if (metadata.size) {
    body = new FormData();
    const transportName =
      "upload" +
      archive.name.slice(archive.name.lastIndexOf(".")).toLowerCase();
    for (const [kind, file] of metadata)
      body.append(
        kind,
        file,
        kind === "native"
          ? `${transportName}.mangacouch.json`
          : `${transportName}.eze.json`,
      );
    body.append("archive", archive, transportName);
  }
  return { archive, body, paired: metadata.size > 0 };
}
