import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  build: {
    outDir: "../src/mangacouch/web",
    emptyOutDir: true,
    target: "es2022",
    rolldownOptions: {
      onLog(level, log, defaultHandler) {
        // radix-ui, lucide-react and sonner ship "use client" directives that
        // mean nothing in a browser bundle; dropping them is expected, and the
        // ~45 warnings would bury any real one.
        if (log.code === "MODULE_LEVEL_DIRECTIVE") return;
        defaultHandler(level, log);
      },
    },
  },
});
