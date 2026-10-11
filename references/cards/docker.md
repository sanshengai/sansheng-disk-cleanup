# Docker（Docker Desktop、OrbStack、Colima 等）

核心事实：容器运行时在虚拟机里，宿主机上只看到一个磁盘映像文件。删镜像、删容器、删卷之后，宿主机空间不一定立刻回来。

## 存储位置

| 路径 | 内容 | 性质 |
|---|---|---|
| `~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw` | Docker Desktop 的虚拟磁盘（路径见官方 FAQ）；稀疏文件，`ls -l` 显示的是上限 | 工具状态（内含镜像、容器、卷） |
| 虚拟磁盘内：镜像 images | 拉取或构建的镜像层 | 可再生缓存（可重新 pull 或 build） |
| 虚拟磁盘内：构建缓存 build cache | BuildKit 层缓存 | 可再生缓存 |
| 虚拟磁盘内：已停止的容器 | 容器可写层 | 通常可再生；可写层里若存了数据则是用户独有成果 |
| 虚拟磁盘内：卷 volumes | 数据库数据、上传文件等持久数据 | 用户独有成果（绝不自动删） |
| `~/.docker` | CLI 配置、登录凭据引用、context | 工具状态（不删） |
| `~/.orbstack`、`~/.colima`、`~/.local/share/containers/podman`（Podman machine） | 其他运行时的虚拟机磁盘 | 工具状态（路径为常见默认，未在本机核实） |

## 怎么判断还在用

- 先量逻辑占用：`docker system df`（总览）与 `docker system df -v`（逐镜像、逐卷、逐容器）。要求 Docker 在运行；未运行时不要为了扫描去启动它，直接报告虚拟磁盘文件大小。
- 宿主机实际占用：`ls -klsh <Docker.raw 所在目录>/Docker.raw`，第一列是实际占用 KB，文件大小列是上限。官方 FAQ 对此有说明。
- 镜像：`docker images` 看 `CREATED`；`docker ps -a` 看哪些镜像被容器使用；被运行中的容器引用的不动。
- 悬空镜像（dangling）与未使用镜像要区分：`docker image prune` 只删悬空；`-a` 会删所有未被容器使用的镜像，包括本地构建而没有推送过的。
- 卷：`docker volume ls`，用 `docker ps -a --filter volume=<卷名>` 看哪些容器挂着它；命名卷没有容器挂载并不代表没用——可能是 compose 项目暂时停掉了。
- 项目引用检查：在用户的 compose 文件、Dockerfile、CI 脚本中搜卷名和镜像名；搜不到只是辅助信号。
- 构建缓存通常是增长最快、最安全的一块，先看 `docker system df` 里 Build Cache 一行。

只读命令速查：

```
docker system df
docker system df -v
docker volume ls
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
ls -klsh ~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw
```

Docker 没有运行时 `docker` 命令会连不上守护进程；本卡不建议为了扫描去启动 Docker Desktop。此时只报告磁盘映像文件的实际占用，并让用户在 Docker Desktop 的 Troubleshoot/Resources 页面查看分类占用。

- 释放量预期：在 prune 前后各取一次 Docker.raw 的实际占用与 `df` 可用量，两者的差异单独写进收据。
- 如果用的是 OrbStack 或 Colima，命令相同，但磁盘映像路径与回收方式不同，以其各自文档为准（未核实）。

## 原生清理与保留设置

- `docker system prune`：删已停止容器、未使用网络、悬空镜像、构建缓存。官方 FAQ 明确这条命令不删带名字的卷，加 `--volumes` 才会。
- `docker builder prune`：只清构建缓存，可加 `--filter until=168h`。
- `docker image prune -a`：删所有未被容器使用的镜像。
- 空间回收：官方说明空间仅在镜像被删除时释放，容器里删文件不会自动释放；可运行 `docker run --privileged --pid=host docker/desktop-reclaim-space` 触发回收（见官方 FAQ）。
- Docker Desktop → Settings → Resources → Disk image size 可调整上限（调小是否会缩实际文件取决于版本，未核实）。
- 官方：https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/ ；https://docs.docker.com/reference/cli/docker/system/prune/

## 不能做的事

- 不要直接删除 `Docker.raw`：等于删光所有镜像、容器和卷；卷里的数据库无法恢复。
- 不要在没问用户的情况下加 `--volumes`，也不要运行 `docker volume prune`。
- 不要用 `docker system prune -a --volumes` 当"一键清理"。
- 删了镜像宿主机空间不回来时，不要重复执行 prune：先看 `ls -klsh Docker.raw` 的实际占用是否已下降，再看 APFS 快照与废纸篓。
- 不要把 `ls -l` 的 64GB 之类上限当成实际占用上报。

## 给用户的一句话问题

"Docker 里有 <N> 个卷（<名字>）看起来没有容器在用，里面可能有数据库数据；你还需要这些数据吗？镜像和构建缓存我只用 docker 自带命令清，卷不动。"
