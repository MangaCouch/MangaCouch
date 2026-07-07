For decisions:

use uv as package management.

LANraragi have a chrome plugin "https://github.com/Difegue/Tsukihi", that can retrieve the url and pass that into downloader of LANraraig. I want that as well. So the MangaCoach would be:

trigger "Archive Download" in e(x)hentai page to download the whole manga.
organize the mange that already on disk.
upload and parse user-uploaded pdf/zip/cbz file. (encourage user to use zip rather than cbz)

the paths would be:
main executable path, databaes path (for things like config, hashes, and metadata), cache path (for things like db index, thumbnails, extracted cache), manga path (pdf/zip only, and sidecar files)

Front end can be more recent python, just make sure dependencies support later python.
download architecture. focus on "Archive Download" on e(x)hentai, and don't think about bittorrent support now.
for downloader, it need to support proxy. (in case the website is blocked)

Yes. I can start at SQLite FTS5. But if Rebuildable sidecar index is very easy to implement and will not introduce a great chance of error, also consider to use that.

Q4, use LANraragi typed plugins for Metadata / Login / Download / Script. but don't make it too complicated now, as the major usage would only be e(x)hentai for now. The trust model can assume server owner trust all plugins, and use a in-process.

Q5. store gallery as zip/pdf/cbz + <filename>.json (eze) + <filename>.mc.json (mangacoach json storage, in additon to eze) as sidecar. You may not write into existing files, as that will break the hash of the file. Don't think about interop now. use both hash. full-file as primary key, fingerprint as a dedup index

Q6. do both, but focus on build a web reader first.

Q7. don't provide LANraragi API compatibility

Q8 give up rar/cbr.

Q9. use Responsive PWA. give up native gaps is acceptable.

Q10. design both as plugin, as just leave an API for now in design phrase, so it is possible in the future, to implement auto-tagging and auto translate (intercept the original image and replace that with the translated image, or pass through extra information to the web browser for the translated overlay).

Q11. oldest target windows is windows 10/11. use python 3.14/3.15

For requirements:
R5, support on zip and pdf is a must. 7z, cbz are optional. you may postpone these support now.
For dependencies. use native libs, but use fastest as possible (if there's a better option than pillow, like pyvips). as long as they are well supported, and support python on macos (arm64), window (amd64) and linux (amd64)

Zoom & inspection is optional.
check Source / stack references collected to make sure they are well-maintained (last update at least 6 months, also with a lot of stars). Don't make it too complicated. make it easy for a simple project, but still try to use lightweight best-practices.

