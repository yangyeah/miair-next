# 可选音乐应用

普通 MiAir 镜像即可独立运行。需要点歌联动时，以现有 MiAir 镜像为基础，选择一种构建目标。以下命令在仓库根目录执行。

```sh
# 飞牛音乐：只搜索已收录的本地文件
docker build -f integrations/music/Dockerfile --target fnmusic \
  --build-arg MIAIR_IMAGE=miair-next:local -t miair-next:fnmusic .

# 自定义应用接口：由应用提供者单独交付构建目录
docker build -f integrations/music/Dockerfile --target custom \
  --build-arg MIAIR_IMAGE=miair-next:local --build-context application=/path/to/adapter \
  -t miair-next:custom .
```

标准目标为 `standard`，直接使用基础镜像内容。自定义应用目录提供 `app.py` 和 `register(protected_router, public_router)` 入口。业务路由应使用已鉴权的 `protected_router`。专用镜像包含交付的适配器，应由应用提供者管理其分发。

## 飞牛音乐

使用 `fnmusic` 镜像，将飞牛音乐的数据库目录只读挂载到容器（包含 SQLite 的 WAL），并按 NAS 上的原始绝对路径只读挂载允许播放的音乐目录。在 MiAir 的持久化目录保存 `fnmusic.json`：

```json
{
  "database": "/catalog/music.db",
  "music_root": "/vol2/1000/example/music",
  "public_url": "http://NAS-IP:8300",
  "did": "所选小爱音箱的设备编号"
}
```

确认容器运行用户能读取挂载文件。说「播放大眠」「播放王心凌的大眠」或「播放王心凌的歌」时，音箱只从指定目录中的曲库命中项取音频。无匹配时保持当前播放。音箱使用只对当前歌曲有效的随机地址读取音频。

如需将「收藏」写入飞牛音乐账号，可额外只读挂载该用户的音乐 token 文件和飞牛音乐服务的 Unix socket，并在配置中填写 `music_token_file`、`music_socket` 两个容器内路径。收藏通过飞牛音乐服务提交。部署时为每套实例选择一个音箱与授权目录。

### 设置与诊断

在 `fnmusic` 可选镜像中打开 `http://NAS-IP:8300/api/v1/fnmusic/settings`，使用原 MiAir 管理员账号登录。页面可选择音箱、配置数据库与音乐目录、收藏账号文件以及启用开关；保存前会验证目录、数据库和设备。页面只填写凭据文件路径，不显示凭据内容。保存后立即生效，关闭时会先停止本扩展正在播放的音箱。

页面显示最近口令及播放、收藏、本地无匹配、服务不可用等结果。“刷新状态”读取最新结果。本地模式还支持“暂停”“继续播放”“换一首”“加入收藏”。继续播放当前曲目；此适配器不提供云端曲库与推荐。

配置读取与保存接口均挂在已鉴权的管理路由，音频只允许使用当前歌曲的随机票据，并重新检查文件位于允许的音乐目录内。标准构建和自定义应用构建不包含这套页面与路由。

### 验证可选镜像

```sh
docker run --rm -i --entrypoint python -w /app/backend miair-next:fnmusic - < integrations/music/test_fnmusic.py
```

测试覆盖目录边界、无结果搜索、下一首、收藏、停止、播放失败恢复、管理接口鉴权、非法配置、启用开关及音频 Range 请求。
