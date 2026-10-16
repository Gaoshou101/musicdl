# 管理前端

前端使用 Vue 3、TypeScript、Vite、Vue Router、Pinia 和 Tailwind CSS 4。
生产构建由 Python 应用同源托管，不需要常驻 Node 服务。

## 目录

- `src/pages/`：登录及各管理页面。
- `src/features/`：音源导入、Telegram 和配置的领域组件与逻辑。
- `src/components/`、`src/layouts/`：共用组件和响应式后台布局。
- `src/composables/`：请求串行化、页面可见性及卸载清理等轮询逻辑。
- `src/stores/`：会话和主题；兼容已有 `tgmusic.theme` 用户偏好。
- `src/lib/`：唯一 API 客户端、格式化和音源导入工具。
- `src/styles/`、`public/`：全局主题样式和项目品牌资源。
- `scripts/`：真实后端契约及 Playwright 浏览器回归入口。

## 开发

需要 Node.js 22.12+ 或受支持的新版本。先在仓库根目录准备 Python 3.12
及项目依赖，然后启动本机后端。在另一个终端进入 `web/`：

```powershell
npm ci
npm run dev
```

Vite 在 3001 端口提供页面，开发代理把 `/admin` 请求转发到本机后端。
代理目标可用 `MUSICDL_DEV_API_TARGET` 配置，默认 `http://127.0.0.1:8000`。
本机 HTTP 调试时后端应使用 `MUSICDL_ADMIN__COOKIE_SECURE=false`；HTTPS 部署
继续使用安全 Cookie。

## 检查与生产构建

```powershell
npm run typecheck
npm test
npm run check:source-import
npm run build
```

`npm run build` 输出 `dist/`，应用镜像将其复制到 `/app/panel`。本机托管可把
`MUSICDL_ADMIN__PANEL_ROOT` 指向 `web/dist` 的绝对路径。服务端只为已知管理
页面提供 SPA 入口；未知页面、缺失资源及 API 错误保留其真实 HTTP 状态。

- `npm run check:api:remote`：仅对明确授权的测试服务运行契约检查，包含一次试下载。
- `npm run check:api:local`：对临时本机应用运行完整契约流程。
- `npm run check:api:local-nostore`：验证插件存储不可用时的拒绝路径。
- `npm run smoke:browser`：设置 `MUSICDL_BROWSER_PANEL_ROOT` 为构建目录，并将
  `PYTHON_EXECUTABLE` 指向安装了项目的 Python 3.12，运行真实后端浏览器回归。

浏览器测试使用临时数据和模拟的 Telegram 响应，不会向真实账号发送验证码。
迁移的功能与验收约束见 `docs/vue-frontend-migration.md`。
