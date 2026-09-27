# Releasing musicdl

A numbered release is four outputs that all have to describe one source commit:

1. the version metadata in this repository,
2. the annotated `v<version>` tag,
3. both Docker images, `wit7zz/musicdl` and `wit7zz/musicdl-plugin-runner`,
4. the published GitHub Release for that tag.

The Docker images and the GitHub Release are produced by
`.github/workflows/docker-publish.yml` when the tag is pushed, so the fourth output
does not depend on anyone remembering to run `gh release create`. A tag without a
published GitHub Release is an incomplete release; backfill it before announcing.

## 1. Prepare the version bump

Branch from `main` and raise the version everywhere it is written. The version
literal is duplicated on purpose so each artifact can be read on its own, so a
release that only edits `pyproject.toml` is incomplete.

| File | What changes |
|---|---|
| `pyproject.toml`, `docker/plugin/pyproject.toml` | `project.version` |
| `docker/main/Dockerfile`, `docker/plugin/Dockerfile` | the wheel filename in `COPY`, `pip install`, and `rm` |
| `web/package.json`, `web/package-lock.json` | `version` |
| `.env.example` | `MUSICDL_IMAGE_TAG` and the "published N images include it" note |
| `compose.prod.yaml` | the example `MUSICDL_IMAGE_TAG` and the pinned-release hint |
| `compose.quick.yaml` | the `MUSICDL_IMAGE_TAG:-` default in both services and the header |
| `README.md`, `README.zh-CN.md` | image table, example commands, quick-manifest default, lite note |
| `.github/workflows/ci.yml` | the comments that name the published images |
| `tests/unit/test_dockerfiles.py`, `test_lite_install.py`, `test_main_packaging.py`, `test_quick_ci.py`, `test_quick_install.py`, `test_runner_packaging.py` | the literals those tests pin |

Leave genuinely historical claims alone: `1.0.1` is still where the cookie-policy
fix landed, and `v1.0.0` images still lack it. Write such sentences so they keep
being true next release ("included in `1.0.3` and later") instead of re-pinning
them to the current version.

Before editing, run `git grep -n "<previous version>" -- .` and account for every
remaining hit in the pull request.

## 2. Write the release notes

`docs/release-notes/v<version>.md` is the source of truth for the published
release body, and `tests/unit/test_release_notes.py` fails the version bump when
it is missing. Keep the shape of the existing files:

- `# musicdl <version>`
- `## English` -- one bullet per user-visible change
- `## 中文` -- the same bullets in Chinese

Do not write the source commit or the image digests by hand; the workflow appends
both after the images are pushed. Describe the change, not the implementation, and
name the version the fix first shipped in rather than "this release".

## 3. Merge and tag

```bash
git push origin codex/release-<version>
gh pr create --fill
```

Wait for every `ci.yml` job on the pull request to pass, merge it, and confirm the
same jobs are green on `main`. The quick-install CI smoke pins the last published
images during this stage because the candidate images do not exist until the tag
is pushed. Then tag the merge commit without changing the local checkout:

```bash
git fetch origin main
git tag -a v<version> origin/main -m "musicdl <version>"
git push origin v<version>
```

## 4. Let the workflow publish, then verify

Pushing the tag starts `docker-publish.yml`, whose two jobs push the images and
then create the GitHub Release. Verify all four outputs name the same commit:

```bash
gh run list --workflow docker-publish.yml --limit 3
gh release view v<version> --json name,tagName,body,url
docker buildx imagetools inspect wit7zz/musicdl:<version> --format '{{.Manifest.Digest}}'
docker buildx imagetools inspect wit7zz/musicdl-plugin-runner:<version> --format '{{.Manifest.Digest}}'
```

The digests in the release body must equal the ones the registry reports, and the
release body's `Source commit` must equal `git rev-parse v<version>^{commit}`.
After publication, run the quick-install smoke without `MUSICDL_IMAGE_TAG` to
exercise the newly published default image pair. The pre-tag CI smoke used the
previous version and does not prove this last step.

If the images finished but the release job failed, re-run just that job; it edits
an existing release rather than failing on it. Re-running overwrites the body from
`docs/release-notes/v<version>.md`, so edit that file if the published text is
wrong.

## 5. Point deployments at the new version

Promote the version in `compose.quick.yaml`'s default and in the READMEs in the
same release, so a fresh quick install and the documented commands both land on
the images that were just verified. Change the deployment host's
`MUSICDL_IMAGE_TAG`, pull, and recreate the services.

## Backfilling a missing release

If a tag was pushed while the release job did not exist or could not run, verify
its images and source commit before creating the missing release. Start from the
committed notes for that version, append `Source commit` and the two registry
digests in the same format as the workflow, and create the release with
`gh release create v<version> --title "musicdl <version>" --notes-file <body-file> --verify-tag`.
Do not re-tag or rebuild historical images merely to backfill the release.

## 中文速查

1. 从 `main` 切分支，同步上表所有文件里的版本号；改之前先跑 `git grep -n "<旧版本>" -- .`，PR 里逐条说明剩余命中。
2. 写 `docs/release-notes/v<新版本>.md`（`# musicdl <版本>` + `## English` + `## 中文`）；源码提交和镜像摘要由工作流追加，不要手写。
3. 开 PR，等 `ci.yml` 全绿后合并，确认 `main` 上也全绿，再对合并提交打注解标签 `v<版本>` 并推送。
4. 推标签会触发 `docker-publish.yml`：先出两个镜像，再创建 GitHub Release。用 `gh release view` 和 `docker buildx imagetools inspect` 核对四份产物指向同一提交。
5. 同一版发布里把快速安装默认值和 README 里的版本一起推进，然后在部署主机改 `MUSICDL_IMAGE_TAG`、拉取并重建服务。
6. 只有标签没有 Release 就是不完整发版：用 `gh release create` 补建，并补上源码提交与两个镜像摘要。
