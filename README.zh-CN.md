# MangaCouch

[English](README.md) | **简体中文**

自托管的个人漫画书库：导入 CBZ/ZIP 和 PDF，在任何设备的浏览器里阅读，也能通过 OPDS 或兼容 Komga 的接口接入常用的阅读器 App。

![书库](docs/screenshots/zh/library.png)

## 特性

- **书库**：网页上传，或把文件放进 `import/` 文件夹扫描导入；支持 GIF、JPEG、WebP、PNG 页面。
- **查找**：标签自动补全并显示中文译名；按语言、类型、评分、阅读状态筛选；支持手动合集和按搜索条件自动收录的动态合集。
- **阅读器**：单页、双页、竖向滚动；左右翻页方向可选；自动翻页和缩略图预览进度条。
- **多端同步**：阅读进度、书签和评分跨设备同步，另有阅读统计。
- **元数据整理**：待核对队列、逐页比对的重复检测，每本书旁边生成 `.mangacouch.json` 元数据文件。
- **下载**：在网页或 Chrome 扩展里加入 E(x)Hentai、nHentai 作品，可比较不同版本；付费存档须确认后才会购买。
- **外部阅读器**：OPDS 1.2 和 Komga API 子集，API key 按权限分级。
- **安全稳妥**：口令登录、导入中断可恢复、七天回收站、一键备份。
- 中英文界面，浅色/深色主题，适配手机。

## 截图

<table>
  <tr>
    <td><img src="docs/screenshots/zh/detail.png" alt="书籍详情"></td>
    <td><img src="docs/screenshots/zh/reader.png" alt="双页阅读"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/zh/search.png" alt="标签补全"></td>
    <td><img src="docs/screenshots/zh/downloads.png" alt="下载与版本选择"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/zh/activity.png" alt="阅读统计"></td>
    <td><img src="docs/screenshots/zh/dark.png" alt="深色主题"></td>
  </tr>
</table>

<p align="center"><img src="docs/screenshots/zh/mobile.png" alt="手机界面" width="300"></p>

截图使用合成的示例书库，书名混合中文、日文和英文。

## 快速开始

Docker Compose（拉取 `ghcr.io/mangacouch/mangacouch`）：

```sh
cp .env.example .env            # 设置 MANGACOUCH_ORIGIN、UID、GID
mkdir -p deploy/state deploy/books
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch init
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch auth-init
docker compose --env-file .env -f deploy/compose.yaml up -d
```

从源码运行：需要 Python 3.12–3.15 和 [uv](https://docs.astral.sh/uv/)，支持 Apple Silicon macOS、Windows x64、glibc Linux x64/ARM64，无需编译器或 Node.js：

```sh
uv sync --locked
mkdir -p data/state data/books
uv run mangacouch --db data/state/library.sqlite --root data/books init
uv run mangacouch --db data/state/library.sqlite auth-init
uv run mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

然后打开 `http://localhost:8780`，用设置的口令登录。

不想装 Python：从 [Releases](https://github.com/MangaCouch/MangaCouch/releases) 下载对应平台的压缩包，用同样的参数运行 `mangacouch/mangacouch`，详见[安装文档](docs/install.md#standalone-executable)。

## 文档（英文）

- [安装](docs/install.md)：独立程序、Docker、源码、Windows、局域网与 HTTPS
- [书库、存储与恢复](docs/library.md)：导入、格式、缓存、备份
- [客户端与集成](docs/clients.md)：API key、OPDS、Komga、浏览器扩展
- [开发](docs/development.md)：检查与测试

## 许可证

[MIT](LICENSE)
