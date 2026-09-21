web/ 目录结构：
- package.json (Next.js + React + Tailwind v4)
- next.config.mjs (output: 'export'，构建出静态导出；没有重写、没有后端地址)
- postcss.config.mjs (Tailwind v4 的 PostCSS 插件；缺了它页面没有样式)
- src/
  - app/ (Next.js App Router；dashboard/ 下是各管理页面)
  - components/ (UI 组件)
  - lib/ (工具函数：api.ts 是唯一的后端客户端，store.ts 管主题)
  - styles/ (全局样式)
- scripts/check-api-contract.mjs (拿真实后端跑一遍请求契约：登录、CSRF、信封；接口与页面同源，默认打 /admin/*)

常用命令：
- npm run build                     构建静态导出到 out/（由应用镜像复制到 /app/panel 并由应用托管）
- npm run check:api:remote          对已部署控制台跑只读契约检查
- npm run check:api:local           对本机 App 跑完整流程（需先起 App，并把 MUSICDL_ADMIN__PANEL_ROOT 指到 out/）
- npm run check:api:local-nostore   对没有插件存储的本机 App 跑拒绝路径
