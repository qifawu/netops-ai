import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 前端产物交给现有的 FastAPI 进程托管，不再起第二个服务。
// 开发时 /api 代理到后端，后端没起就自动退回 public/fixtures。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: { outDir: "dist" },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
