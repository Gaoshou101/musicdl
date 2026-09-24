from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
QUICK_COMPOSE = ROOT / "compose.quick.yaml"


def quick_compose():
    return yaml.safe_load(QUICK_COMPOSE.read_text(encoding="utf-8"))


def test_quick_install_uses_published_images_and_needs_no_build_or_external_redis():
    data = quick_compose()
    services = data["services"]

    assert data["name"] == "musicdl"
    assert set(services) == {"musicdl", "plugin-runner", "redis"}
    assert all("build" not in service for service in services.values())
    assert services["musicdl"]["image"] == "wit7zz/musicdl:${MUSICDL_IMAGE_TAG:-1.0.1}"
    assert services["plugin-runner"]["image"] == "wit7zz/musicdl-plugin-runner:${MUSICDL_IMAGE_TAG:-1.0.1}"
    assert services["musicdl"]["environment"]["MUSICDL_REDIS__URL"] == "redis://redis:6379/0"
    assert "ports" not in services["redis"]
    compose_text = QUICK_COMPOSE.read_text(encoding="utf-8")
    assert "${MUSICDL_REDIS__URL" not in compose_text
    assert "set an external Redis URL" not in compose_text


def test_quick_install_defaults_admin_cookie_to_secure_and_allows_override():
    services = quick_compose()["services"]
    variable = "MUSICDL_ADMIN__COOKIE_SECURE"

    assert services["musicdl"]["environment"][variable] == "${MUSICDL_ADMIN__COOKIE_SECURE:-true}"
    assert variable not in services["plugin-runner"].get("environment", {})


def test_quick_install_keeps_application_volumes_and_persists_bundled_redis():
    data = quick_compose()
    services = data["services"]

    assert set(services["musicdl"]["volumes"]) == {
        "musicdl-media:/data/music",
        "musicdl-app-data:/data/app",
        "musicdl-telegram:/data/telegram-sessions",
    }
    assert services["redis"]["volumes"] == ["musicdl-redis-data:/data"]
    assert data["volumes"] == {
        "musicdl-media": None,
        "musicdl-app-data": None,
        "musicdl-telegram": None,
        "musicdl-redis-data": None,
    }
    assert "--appendonly" in services["redis"]["command"]


def test_quick_install_preserves_loopback_and_network_isolation_boundaries():
    data = quick_compose()
    services = data["services"]
    app = services["musicdl"]
    runner = services["plugin-runner"]
    redis = services["redis"]

    assert app["ports"] == ["127.0.0.1:${MUSICDL_PORT:-8000}:8000"]
    assert set(app["networks"]) == {"default", "state", "plugin-control"}
    assert runner["networks"] == ["plugin-control"]
    assert redis["networks"] == ["state"]
    assert data["networks"]["state"]["internal"] is True
    assert data["networks"]["plugin-control"]["internal"] is True
    assert "ports" not in runner
    assert runner.get("environment", {}) == {}
    assert runner.get("volumes", []) == []
    assert runner.get("secrets", []) == []
    assert not any("docker.sock" in str(value) for value in runner.get("volumes", []))


def test_quick_install_hardens_services_and_waits_for_dependencies():
    data = quick_compose()
    services = data["services"]

    for name in ("musicdl", "plugin-runner"):
        service = services[name]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["restart"] == "unless-stopped"
        assert "healthcheck" in service

    runner = services["plugin-runner"]
    assert runner["mem_limit"] == "512m"
    assert runner["cpus"] == "1.0"
    assert runner["pids_limit"] == 64
    assert runner["init"] is True
    assert runner["tmpfs"] == ["/tmp:size=32m,noexec,nosuid,nodev"]

    redis = services["redis"]
    assert redis["image"] == "redis:8.8.3-alpine"
    assert redis["user"] == "redis"
    assert redis["read_only"] is True
    assert redis["cap_drop"] == ["ALL"]
    assert redis["security_opt"] == ["no-new-privileges:true"]
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]
    assert set(services["musicdl"]["depends_on"]) == {"redis", "plugin-runner"}
    assert all(
        services["musicdl"]["depends_on"][name]["condition"] == "service_healthy"
        for name in ("redis", "plugin-runner")
    )


def test_quick_install_and_migration_docs_match_the_manifest():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    english_migration = english.split("## Migrating an Existing Deployment", 1)[1].split("## Configuration", 1)[0]
    chinese_migration = chinese.split("## 从现有部署迁移", 1)[1].split("## 配置", 1)[0]

    for readme in (english, chinese):
        assert "https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.quick.yaml" in readme
        assert "docker compose -f compose.quick.yaml up -d" in readme
        assert "MUSICDL_IMAGE_TAG" in readme
        assert "1.0.1" in readme
        assert "musicdl-redis-data" in readme
        assert "-v" in readme

    assert "does not need a repository checkout, `.env` file, or separately managed Redis" in english
    assert "无需克隆仓库、创建 `.env` 或单独准备 Redis" in chinese
    assert "MUSICDL_ADMIN__COOKIE_SECURE=false" in english
    assert "create or edit a `.env` file next to `compose.quick.yaml`" in english
    assert "if the setting is omitted, cookies remain HTTPS-only" in english
    assert "Keeping the line in `.env` preserves the HTTP setting across later Compose recreations" in english
    assert "To restore the secure default, remove the line and force-recreate `musicdl`" in english
    assert "A fresh quick install starts with username `admin` and password `password`" in english
    assert "Immediately set a new, strong password; the panel blocks the rest of the administration API until the credential change is completed" in english
    assert "Keep the service on loopback and private while the default credentials are active" in english
    assert "MUSICDL_ADMIN__COOKIE_SECURE=false" in chinese
    assert "请在 `compose.quick.yaml` 同目录创建或编辑 `.env` 文件" in chinese
    assert "未设置该变量时，Cookie 仍仅通过 HTTPS 发送" in chinese
    assert "将该行保留在 `.env` 中可使后续 Compose 重建继续使用 HTTP 设置" in chinese
    assert "要恢复安全默认值，请删除这一行并强制重新创建 `musicdl`" in chinese
    assert "全新快速安装的初始用户名为 `admin`，密码为 `password`" in chinese
    assert "请立即设置新的强密码；面板会在凭据更改完成前阻止其余管理 API 的使用" in chinese
    assert "默认凭据仍有效期间，请保持服务仅通过环回地址访问且不对外开放" in chinese
    assert "External Redis contents are not copied automatically" in english_migration
    assert "外部 Redis 内容不会自动复制" in chinese_migration
    assert "repeat every `-f` in the same order" in english_migration
    assert "按原顺序重复所有 `-f`" in chinese_migration
    assert english_migration.index("Stop the old stack before backing up") < english_migration.index(
        "Back up the actual volume names"
    )
    assert chinese_migration.index("先停止旧栈，再备份") < chinese_migration.index("备份步骤 1 查到的实际卷名")
    assert "`<project>_musicdl-media`" in english_migration
    assert "`<项目名>_musicdl-media`" in chinese_migration
    assert "`external: true`" in english_migration
    assert "`external: true`" in chinese_migration
    assert "pass its absolute path" in english_migration
    assert "传入该文件的绝对路径" in chinese_migration
    assert "up -d redis" in english_migration
    assert "import the backed-up data, and verify it before starting the app" in english_migration
    assert "先只启动内置 Redis，导入备份并验证后，再启动应用" in chinese_migration
    assert "roll back the service deployment only" in english_migration
    assert "restore the application-volume snapshots" in english_migration
    assert "Also review customized `wecom.api_base`, `telegram.proxy`, and `ai.base_url` values" in english_migration
    assert "does not include old proxy/overlay services or automatically carry their environment wiring" in english_migration
    assert "map/replace it and provide its required environment settings" in english_migration
    assert "planned migrations where required Redis data and deployment-dependent settings are imported/mapped and verified before the app starts" in english_migration
    assert "Recheck custom `wecom.api_base`, `telegram.proxy`, and `ai.base_url` endpoints and run their relevant workflows" in english_migration
    assert "integration reachability" in english_migration
    assert "including `admin-state.json` on the app-data volume" in english_migration
    assert "including the saved admin state" in english_migration
    assert "docker compose -p OLD_PROJECT -f compose.quick.yaml down" in english_migration
    assert "docker compose -p OLD_PROJECT -f compose.prod.yaml up -d" in english_migration
    assert "只回滚服务" in chinese_migration
    assert "恢复迁移前备份的应用卷" in chinese_migration
    assert "同时检查自定义的 `wecom.api_base`、`telegram.proxy`、`ai.base_url`" in chinese_migration
    assert "不包含旧代理/覆盖服务，也不会自动带上它们的环境配置" in chinese_migration
    assert "先映射/替换地址并补齐所需环境设置" in chinese_migration
    assert "已在启动应用前完成必需 Redis 数据导入、部署依赖配置映射并验证的迁移" in chinese_migration
    assert "复查自定义的 `wecom.api_base`、`telegram.proxy`、`ai.base_url` 地址并验证对应工作流" in chinese_migration
    assert "外部集成" in chinese_migration
    assert "app-data 卷内的 `admin-state.json` 也会保留" in chinese_migration
    assert "包括已保存的后台状态" in chinese_migration
    assert "docker compose -p OLD_PROJECT -f compose.quick.yaml down" in chinese_migration
    assert "docker compose -p OLD_PROJECT -f compose.prod.yaml up -d" in chinese_migration
    assert english_migration.index("Before following these steps") < english_migration.index(
        "Stop the old stack before backing up"
    )
    assert chinese_migration.index("按以下步骤迁移前") < chinese_migration.index("先停止旧栈，再备份")
    assert "panel override is persisted in `admin-state.json`" in english_migration
    assert "takes precedence over Compose environment" in english_migration
    assert "the quick file's internal Redis URL alone will not switch the app" in english_migration
    assert "Use this procedure only when the source is `Environment` (`env`)" in english_migration
    assert "or when the source is `Default`" in english_migration
    assert "do not click `Restore deployment value` while the old app or workers are running" in english_migration
    assert "the secret is masked" in english_migration
    assert "can hot-reload `redis.url` to a different endpoint immediately" in english_migration
    assert "Keep the external-Redis Compose deployment running" in english_migration
    assert "Verify the Redis source is `Environment` (`env`), `/readyz` reports general readiness" in english_migration
    assert "not universal proof of Redis connectivity" in english_migration
    assert "面板覆盖会保存在复用的应用数据卷中的 `admin-state.json`" in chinese_migration
    assert "并优先于 Compose 环境变量" in chinese_migration
    assert "仅在快速安装文件中设置内置 Redis 地址，并不能保证应用切换过去" in chinese_migration
    assert "只有来源为「部署变量」（`env`）" in chinese_migration
    assert "如果来源为「默认值」" in chinese_migration
    assert "旧应用或 Worker 运行期间不要点击「恢复为部署值」" in chinese_migration
    assert "Redis 密钥会被隐藏" in chinese_migration
    assert "可能立即热重载 `redis.url` 并切换到其他地址" in chinese_migration
    assert "保持旧的外部 Redis Compose 部署运行" in chinese_migration
    assert "来源显示「部署变量」（`env`）、`/readyz` 报告服务总体就绪" in chinese_migration
    assert "不能普遍证明所有工作流都能连接 Redis" in chinese_migration
    assert "quick install" in env_example.lower()
    assert "external redis" in env_example.lower()
    assert "MUSICDL_ADMIN_PORT" not in env_example
