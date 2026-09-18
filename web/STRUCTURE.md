web/ 目录结构：
- package.json (Next.js + React + Tailwind v4)
- next.config.mjs (把浏览器的 /api/* 转发到 musicdl 的 /admin/*)
- postcss.config.mjs (Tailwind v4 的 PostCSS 插件；缺了它页面没有样式)
- src/
  - app/ (Next.js App Router；dashboard/ 下是各管理页面)
  - components/ (UI 组件)
  - lib/ (工具函数：api.ts 是唯一的后端客户端，store.ts 管主题)
  - styles/ (全局样式)
- scripts/check-api-contract.mjs (拿真实后端跑一遍请求契约：登录、CSRF、信封)

常用命令：
- npm run build                     构建（MUSICDL_API_ORIGIN 在构建期确定后端地址）
- npm run check:api:remote          对已部署面板跑只读契约检查
- npm run check:api:local           对本机 App 跑完整流程（需先起 App 与面板）
- npm run check:api:local-nostore   对没有插件存储的本机 App 跑拒绝路径
