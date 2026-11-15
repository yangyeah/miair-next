# 小米妙播接收服务（实验性）

将小米手机系统妙播（MiPlay）的音频接入现有 MiAir Next。
手机控制中心选择接收器后，Rust 进程接收并解码音频，Python 桥接器提供 HTTP WAV，
再通过 MiAir Next 的 `play_url` 接口让指定小爱音箱播放。

提供两种部署方式：与原 MiAir 并行的附加容器，或替换原容器的**集成镜像**。
集成镜像保留所选基础镜像的主应用版本，增加妙播进程与双服务健康检查；
不会自动升级主应用或修改账号数据格式。主 Dockerfile、安装脚本和飞牛安装包
尚未改动，更新上游标准镜像不会自动包含妙播。

## 已验证范围

2026-11-14，ARM64 飞牛、MiAir Next 0.6.0、小爱音箱 Play（LX05）、
小米 15 / HyperOS 3.0.307 组合已由用户确认：系统妙播可发现、连接并播放出声。
后台确认收到 PCM 且音箱建立 HTTP 拉流连接。当前版本在 ARM64 飞牛上通过了 92 项 Rust 测试和 21 项妙播 Python 测试。

仅针对单音箱音乐播放验证。集成版已在真机上验证暂停和断开均不再播报播放失败。音量、切歌和长时间稳定性仍需更多实测；
不保证视频口型同步或多房间声学同步。其他手机版本的设备准入行为可能不同。

## 配置并启动

要求 Linux、Docker Compose、host 网络，以及可正常登录和播放的 MiAir Next。
附加模式要求主 API 已提供 `play_url` 和 `stop`；0.6.0 等缺少 `stop` 的版本请用下文集成模式。
手机、NAS、音箱应能在局域网互访。以下命令均从仓库根目录执行：

```sh
cp miplay/config.example.json miplay/config.json
chmod 600 miplay/config.json
mkdir -p miplay/state
chmod 700 miplay/state
```

编辑 `miplay/config.json`：

- `host`：音箱可访问的 NAS 局域网 IPv4，不能填 `127.0.0.1`。
- `interface`：承载该地址的网卡，例如 `eth0` 或 `br-lan`。
- `name`：主 API 暂不可用时的备用广播名。正常使用目标音箱在后台设置的名称，空闲时每 10 秒检查改名并重新广播。
- `api`：原 MiAir Next API，包含 `/api/v1`；host 网络下可使用本机回环地址。
- `username` / `password`：**MiAir Next 管理后台**账号，不是飞牛管理员或小米账号。
- `speaker_did`：原管理后台启用的音箱 DID，可通过鉴权后的 `/api/v1/speakers` 获取。
- `audio_port` / `health_port`：默认 18300 / 18301，按需修改，二者必须不同。

以创建配置文件的用户 UID/GID 运行，以便读取配置和写入独立身份目录：

```sh
export MIPLAY_UID=$(id -u)
export MIPLAY_GID=$(id -g)
docker compose -f miplay/compose.yml up -d --build
```

不需要特权模式或声卡设备。接收器使用 UDP 5353、UDP 55982、TCP 8899 和协商端口；
同一主机暂只运行一个本接收器。音频端口向局域网开放并使用随机会话令牌，
健康检查仅监听回环地址。该服务仍依赖原 MiAir Next 的小米云播放控制。

配置与身份不写入镜像，已加入 Git/Docker 忽略规则。不要提交实际 `config.json`、
`state/`、日志或抓包。身份目录应持久化，避免每次启动生成新的设备身份。

## ARM64 飞牛本机编译

若镜像拉取受限，且宿主已有 gcc，可使用独立工具链目录：

```sh
CARGO_BUILD_JOBS=1 sh miplay/build-native.sh
docker build -f miplay/Dockerfile.prebuilt -t miair-next-miplay:local .
docker compose -f miplay/compose.yml up -d --no-build
```

编译器和缓存都放在 `miplay/toolchain/`、`miplay/target/`，不会写入用户主目录或 shell 配置。
程序必须在与容器匹配的架构上编译。`BASE_IMAGE` 构建参数可指定已有的 MiAir 镜像标签；
Compose 对应变量是 `MIAIR_BASE_IMAGE`。不提交预编译二进制。

## 替换主应用：集成镜像

先按上面的 Dockerfile 或本机编译流程构建 `miair-next-miplay:local`。
如需保留当前主应用版本，将构建参数 `BASE_IMAGE` 指定为原容器的本地镜像标签。
集成层继续运行基础镜像内的 `/app/backend/run.py`，妙播使用本仓库独立的音频辅助模块。
0.6.0 等旧版缺少停止播放 API，构建时会向原鉴权音箱路由补充 `/speakers/{did}/stop`；
已有该接口的版本保持原实现，不会重复注册。

备份原数据目录、保留原容器和镜像，再停止原容器以及附加妙播容器，避免端口冲突。
设置实际数据目录与配置文件所有者 UID/GID：

```sh
export MIAIR_DATA_DIR=/path/to/existing/miair/data
export MIPLAY_UID=$(id -u)
export MIPLAY_GID=$(id -g)
docker compose -f miplay/compose.integrated.yml up -d --build
```

主界面仍为 8300 端口，原登录账号和音箱设置来自原数据目录。
妙播进程使用指定 UID/GID；主应用沿用基础镜像的入口脚本与权限设置。
任一进程退出会结束整个容器，交给 Docker 重启；正常关闭时先停止妙播，
再关闭主 API，确保音箱能收到停止命令。妙播配置仍位于 JSON 文件，尚未增加网页设置项。

回退时先停止集成容器，再启动保留的原容器；如需原型妙播，随后启动原附加容器。
不要同时启动新旧接收器。飞牛应用商店更新可能覆盖自定义镜像，需重新部署集成版本。

## 测试和诊断

```sh
python -m pip install -e ./backend
PYTHONPATH=backend:miplay python -m unittest discover -s miplay/tests -v
cargo test --locked --release --manifest-path miplay/vendor/FusionPlay.MiPlaySdk/Cargo.toml --no-default-features --features headless
docker compose -f miplay/compose.yml logs --tail 100 miplay
curl http://127.0.0.1:18301/health
```

`ready` 只表示接收进程启动，不能代替真机验证；`pcm_frames` 增长表示正在解码，
`audio_clients` 大于零表示音箱正在拉流。手机明确暂停时，桥接器丢弃待发音频并发送静音，最多保持拉流连接 60 秒。在此期间继续播放可复用原连接。
暂停到期或媒体流关闭时，如果音箱仍在拉取本会话，桥接器会请求停止音箱，在等待响应期间补静音，然后关闭 HTTP 流。
音箱开始拉流时最多保留最近 2 个 AAC 块（约 43 毫秒）。播放中队列可容纳 8 块以吸收短时突发，收到数据即发送。暂停或调音量仍会清除旧音频。`playback_dropped_blocks` 统计本次会话在音箱拉流期间因队列溢出丢弃的块数。
在上述 LX05 真机组合上，暂停、继续和音量调节的听感延时仍约 2 秒，尚未达到端到端 200 毫秒。
修改 JSON 配置后执行 `docker compose -f miplay/compose.yml restart miplay`。

附加模式回退：`docker compose -f miplay/compose.yml down`。集成模式按上节回退。

## 目录与许可

`receiver/` 是 Rust 命令行包装；`vendor/` 保留指定版本的 FusionPlay 接收协议代码；
`bridge.py` 管理 PCM 会话与 MiAir API；音频 HTTP 输出直接复用 `backend/app/engine/`。
本目录新增代码和 FusionPlay 组件为 AGPL-3.0-only，复用的 MiAir 代码保留 GPL-3.0-or-later。
详见 [NOTICE](NOTICE) 和 [LICENSE](LICENSE)。
