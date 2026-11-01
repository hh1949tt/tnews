# 飞牛 fnOS 部署

飞牛私有云（fnOS）自带 Docker 与图形化 Compose，**全程不需要 git**，也不需要命令行就能装好。需要命令行的地方只用 SSH 传文件。

> 界面按钮名称可能随 fnOS 版本略有差异，路径以「文件管理」里显示的实际路径为准。本文示例假设：存储空间 1 挂载在 `/vol1`，首个用户 uid 为 `1000`，项目目录 `/vol1/1000/docker/tnews`。

## 一、准备

1. fnOS 已初始化并建好存储空间；
2. 应用中心装好 **Docker**（没装的话搜「Docker」安装，装完在桌面能看到图标）；
3. NAS 与手机/电脑在同一局域网，记下 NAS 的 IP（如 `192.168.1.100`）；
4. 到「设置 → 系统 → SSH」确认 SSH 已开启（只用图形界面上传文件的可以跳过）。

资源方面 1 核 512 MB 就够跑，首次构建会临时占几百 MB 空间。

## 二、建项目目录

图形界面：**文件管理 → 存储空间 1 → 新建文件夹 `docker`**，进去再建 **`tnews`**。

SSH 更快：

```bash
ssh 你的用户名@192.168.1.100
mkdir -p /vol1/1000/docker/tnews
```

## 三、把项目文件放进去

### 方式 A：图形界面上传（推荐给不熟悉命令行的）

1. 电脑上从 GitHub 下载 `Code → Download ZIP`（或 Release 里的 `tnews-<版本>.zip`）；
2. 文件管理进入 `/vol1/1000/docker/tnews`，点「上传」选这个 zip；
3. 上传完成后右键 zip →「解压」；
4. 如果解压出的是 `tnews-1.0.0/` 这样的一层目录，把里面的文件**全部剪切**到 `tnews` 根目录（`Dockerfile` 必须直接位于 `tnews` 下）。

### 方式 B：SSH 直接下（NAS 能上外网时最省事）

> 仓库若是**私有**的，下面的 `wget` 会拿到 404（需要登录凭证），改用方式 A 或 C。

```bash
cd /vol1/1000/docker/tnews
wget -O tnews.tar.gz https://github.com/hh1949tt/tnews/archive/refs/tags/v1.0.0.tar.gz
tar -xzf tnews.tar.gz --strip-components=1
rm tnews.tar.gz
```

### 方式 C：电脑传上去

```bash
scp tnews.tar.gz 你的用户名@192.168.1.100:/vol1/1000/docker/tnews/
ssh 你的用户名@192.168.1.100 'cd /vol1/1000/docker/tnews && tar -xzf tnews.tar.gz --strip-components=1 && rm tnews.tar.gz'
```

放好后目录应该是这样（缺 `Dockerfile` 一定构建失败）：

```
/vol1/1000/docker/tnews
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── config.yaml
├── app/
├── web/
└── data/          ← 首次启动自动生成，不用手动建
```

> **不要**把旧机器的 `data/` 一起传上来覆盖（除非你就是想迁移数据）——里面是数据库和往期音频。

## 四、按需改 `docker-compose.yml`

打开 `/vol1/1000/docker/tnews/docker-compose.yml`，只改这几处：

```yaml
ports:
  - "18282:18282"          # 左边被占用就改成 18283、8090 等
environment:
  - TZ=Asia/Shanghai
  - TNEWS_ADMIN_PASSWORD=换成你自己的密码   # 可选，只在首次建库时生效
volumes:
  - /vol1/1000/docker/tnews/data:/app/data
  - /vol1/1000/docker/tnews/config.yaml:/app/config.yaml:ro
```

两点说明：

- 卷路径用**绝对路径**最稳。`./data` 这种相对写法在 Compose 里是相对「项目目录」，由 fnOS 的 Docker 应用创建时一般也能正确解析，但路径写死就不用赌。
- 一旦改成绝对路径里的 `data/` 还不存在，先 `mkdir -p data`（Compose 会把不存在的宿主机目录建成 root 所有，之后容器写文件偶尔会别扭）。

## 五、创建 Compose 项目

1. 打开 **Docker** 应用 → 左侧 **Compose**（部分版本叫「项目」）→ **新增**；
2. **项目名**填 `tnews`；
3. **来源**选「路径」/「本地目录」，指向 `/vol1/1000/docker/tnews`——**不要选「上传 compose 文件」**，那样只有一份 yml、没有 `Dockerfile` 和源码，构建会直接失败；
4. 勾选「创建后立即启动」；
5. 确定。首次要拉 `python:3.12-slim` 并 `pip install`，**2～5 分钟**，日志出现下面这行就成了：

```
Uvicorn running on http://0.0.0.0:18282
```

## 六、第一次登录

- 浏览器打开 `http://192.168.1.100:18282`；
- 管理员密码是首次启动随机生成的，两种拿法：

```bash
# 命令行
cd /vol1/1000/docker/tnews && docker compose logs tnews | grep 管理员

# 或者直接看文件（用 TNEWS_ADMIN_PASSWORD 指定过密码的话不会有这个文件）
cat /vol1/1000/docker/tnews/data/INITIAL_ADMIN_PASSWORD.txt
```

登录后到「我的账号」改掉密码，然后点右上角 ⚙️ 生成第一期（1～2 分钟）。

## 七、日常运维

| 操作 | 做法 |
|---|---|
| 改 `config.yaml` | 改完在 Docker → Compose → 项目 `tnews` 点**重启**（或 SSH `docker compose restart`） |
| 升级版本 | 覆盖 `app/ web/ Dockerfile requirements.txt docker-compose.yml`（保留 `data/` 与 `config.yaml`）→ 点**重新构建** |
| 看日志 | Compose 项目详情 → 日志；SSH 用 `docker compose logs -f --tail=100` |
| 备份 | 文件管理把 `data/` 和 `config.yaml` 打包下载，或用 fnOS 的备份任务定期同步到别的盘 |
| 开机自启 | compose 里已有 `restart: always`；再确认 Docker 应用本身设为开机启动 |

升级只换文件不重建镜像是不生效的（代码在镜像里），必须点一次「重新构建」。

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| 构建报 `failed to read Dockerfile` | 项目路径指向的目录里没有 `Dockerfile`：多半选了「上传 compose」模式，或 zip 解压后多了一层目录 |
| 日志 `exec format error` | ARM 机型（部分 ARM NAS）用了 x86 镜像构建的镜像，见[部署 · 跨架构构建](deployment.md#13-在别的机器上构建把镜像拷过去) |
| 构建卡在 `pip install` / 超时 | NAS 出网慢。编辑 `Dockerfile`，把安装那行换成国内源：`RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` |
| 端口冲突起不来 | `ports` 左值换一个（如 `18283:18282`），访问地址随之变化 |
| 页面能开但生成失败 | 看日志里是不是语音合成超时（`speech.platform.bing.com` 不通），或新闻源被墙 |
| 容器起来又退出 | 多半是 `config.yaml` 挂载成了空目录（宿主机上该文件不存在时 Docker 会建目录），检查路径与文件名 |
| 局域网外要听 | **不要**直接把 18282 映射到公网。用 fnOS 的远程访问/DDNS 配合反向代理加 HTTPS，见[反向代理与 HTTPS](deployment.md#反向代理与-https) |
| 换了存储空间路径 | `/vol1/1000/...` 里的 `vol1`、`1000` 都可能变，以「文件管理」显示的路径为准，compose 里的卷和项目路径要一起改 |

## 九、卸载

Docker → Compose → 项目 `tnews` → 停止并删除项目，然后删掉 `/vol1/1000/docker/tnews` 目录（数据库和音频都在里面，删了就没了，先备份）。
