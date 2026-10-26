# CSI201 Networked Quiz Tournament

Python 3.12 标准库、直接 TCP socket、纯命令行。服务器管理身份、房间、题序、截止时间、答案与分数；客户端只通过网络获取公开目录和当前题目。无需安装依赖，不使用 GUI、外部数据库或互联网服务。

协议全文见 [PROTOCOL.md](PROTOCOL.md)，设计与评分项追踪见 [docs/Development_Documentation.md](docs/Development_Documentation.md)。

## 1. 启动

在包含源码的目录运行。端口可自行选择 1–65535；下例使用 5000。

服务器：

```sh
python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
```

完整题库通过校验且监听成功后输出 `SERVER_READY 127.0.0.1 5000`。题库错误时输出诊断到 stderr，非零退出，不监听、不输出 READY。也可传入另一个符合任务书 schema 的题库路径，不依赖默认题号、答案、题数、分值或时限。

另开两个终端，分别运行：

```sh
python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
```

```sh
python3 client.py --server 127.0.0.1 --port 5000 --id bob --name Bob
```

自动登录成功后输出 `LOGIN_RESPONSE {}`；不会显示 session token。操作结果和主动事件按 `消息类型 JSON载荷` 逐行显示，异步到达时无需按回车。终端控制字符会转义，选项无法执行 ANSI 控制序列。

Alice 输入：

```text
/quizzes
/create network-basics
/ready
```

Bob 输入（以实际返回的房号为准）：

```text
/rooms
/join R000001
/players
/ready
```

Alice 输入 `/start`。各客户端收到 QUESTION_EVENT 后，使用事件中的 question_id 作答，例如 `/answer NET001 B`。程序会等待依赖的登录/房间操作确认；不能把全部比赛答案一次性提前输入，后题尚未开放时服务器会拒绝。

## 2. CLI 命令

| 命令 | 作用 |
|---|---|
| `/help` | 本地帮助 |
| `/quizzes` | 公开题库目录，不含题干、选项或答案 |
| `/rooms` | 可加入且未满的大厅，按房号升序 |
| `/create quiz_id` | 创建并加入房间，成为房主，初始未准备 |
| `/join room_code` | 加入大厅，初始未准备 |
| `/players` | 房间、成员、房主、准备状态和当前题号 |
| `/ready`、`/unready` | 设置准备状态；仅大厅有效 |
| `/start` | 房主开始，至少两人且包括房主在内全员准备 |
| `/answer question_id A` | 首次有效作答；选项小写转大写，ID 不转换 |
| `/score` | 已结算排行榜；大厅全零 |
| `/leave` | 离房但保留登录；房主自动转给最早加入的剩余成员 |
| `/quit` | 注销并关闭连接 |

非法命令和缺参数在本地提示；重复答案、非法选项和旧题迟交可通过正式客户端发送，演示服务器拒绝。错误显示固定 code 和安全说明，不泄露开放题正确答案。

## 3. 比赛与边界规则

- 按题库文件顺序出题；每题使用自己的 `time_limit` 和 `points`。所有当前活跃玩家均有效作答时立即结算，否则等服务器 monotonic 截止时间；等于截止时间也算迟交。
- 首次有效答案不可修改；非法选项不占机会。正确得题库 points，错误/缺答为零；开放期榜单不计本题分。
- 最后一位答题者先收到 ANSWER_RESPONSE，再收到个人 RESULT，再下一题或 FINISHED。结果只包含接收者自己的答案；最终榜含离开者的原始参赛记录。
- 按分数降序、同分 user_id ASCII 升序排列；竞争排名 1/1/3；最高分并列者全部获胜，全零也全部共同获胜。
- 仅大厅可加入。每房只有一场，结束后可查询或离开；离开后可建新房。空房立即回收，房号不复用。
- 离开/断线后保留已接受答案和已得分，记录 inactive；若其他活跃玩家都已答则提前结算。剩一人继续。重连必须重新登录，不能继承旧参赛资格。
- user_id 和显示名分别在在线会话中唯一、区分大小写；不自动 trim 或 Unicode 归一化。

## 4. 自动测试

```sh
python3 -m unittest discover -s tests -v
```

测试使用标准库 unittest，无需 pytest。真实网络测试自动选择空闲本地端口、创建临时替换题库，并启动本项目 server.py/client.py 子进程；持续读取 stdout/stderr，结束时清理进程。约需 30–45 秒，含真实 5 秒超时；不需要手工启动服务器。

| 测试文件 | 覆盖 |
|---|---|
| `test_protocol.py` | 所有字节分片点、合并帧、半帧年龄、坏长度、UTF-8、重复键、非有限数、字段与排名约束 |
| `test_question_bank.py` | 全量 schema、上下界、严格整数、跨题库重复 ID、额外字段、1MB 字节边界、替换库 |
| `test_game.py` | 身份/序号/房间/准备/开始、计时边界、一次计分、提前结算、离开/重连、并列/全零排名 |
| `test_client.py` | 响应关联、事件编号/token、终端转义、有界输入和 pending 满时退出 |
| `test_transport.py` | 部分 send、读写预算、登录/半帧/发送超时、TX 上限、64 连接、selector 无零掩码 |
| `test_integration.py` | 真实成对客户端整局、正确/错误/重复/缺答/迟交、十客户端双房间（同/不同 quiz）、畸形连接隔离、断线与 Ctrl+C |

本次云环境的运行结果仅用于开发排错。按你的要求，官方 Docker 环境复验留待后续；没有将云环境结果标成官方镜像证据。

## 5. 官方 Docker 复验

使用课程在 Learning Mall 发布的**官方镜像名和 tag** 替换 `<OFFICIAL_IMAGE:TAG>`，不构建自定义镜像，不安装包。没有官方 tag 时不能宣称完成正式环境验证。

宿主机在源码目录启动容器并运行服务器：

```sh
docker run --rm -it --name csi201-cw -v "$PWD":/submission -w /submission <OFFICIAL_IMAGE:TAG> python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
```

另开终端，在同一容器内运行客户端（给每个客户端不同 ID/name）：

```sh
docker exec -it csi201-cw python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
```

运行测试并保留真实日志和退出状态：

```sh
docker exec csi201-cw python3 -m unittest discover -s tests -v
```

宿主机可将上条命令 stdout/stderr 重定向到实际证据文件；不要用管道掩盖测试退出码。记录官方镜像 tag、image ID、Python 版本、代码 commit、命令、时间和结果，填入开发报告。使用两个以上房间完成不同 quiz 及相同 quiz 的演示。

## 6. 停服、日志与资源

客户端 `/quit`、stdin EOF 或 Ctrl+C 正常退出；服务器 Ctrl+C 停止监听，尽力向房内成员发 ROOM_CLOSED_EVENT，最多刷新 0.5 秒后关闭资源。服务进程结束后输出 SERVER_STOPPED；同端口可立即重启。所有状态只在内存中，进程重启不恢复比赛。

正常输出在 stdout，启动诊断在 stderr；默认不写日志文件，不记录 token、开放题答案或用户提交选项。客户端在题目结束时显示自己的答案与公开结果，这是比赛输出。

上限集中在 protocol.py：64 连接、32 房间、16 人/房、65536 字节正文、RX 128KiB/TX 256KiB、每轮每连接读写各 32KiB/8 帧、每轮 accept 16。登录/半帧/无发送进展超时均为 10 秒；select 最长 0.1 秒且不越过最近截止。客户端连接超时 5 秒，pending 上限 64（5 秒提示一次不重发），输入队列 128、每行 512 字符。

## 7. 源码与提交

- `protocol.py`：长度前缀、严格 JSON、两端共享 schema 和资源常量。
- `question_bank.py`：只供服务器调用的题库加载/校验。
- `game.py`：不依赖 socket 的单拥有者业务状态机和深拷贝输出。
- `server_network.py`：selectors、收发缓冲、公平调度、期限与迭代清理。
- `server.py`、`client.py`：规定的入口。
- `questions.json`：两个 quiz，每个五题，分值与时限不同。
- `docs/Development_Documentation.md`：开发文档源稿及验证追踪，团队信息与官方证据须如实补齐后排版为 PDF。

所有自动化测试和运行时源文件都只导入 Python 标准库。无需 install 脚本或虚拟环境。

## 8. 已知限制

协议 v1 没有 TLS、密码、断线恢复或持久化，只适用于受控课程实验环境。单线程同步题库校验只发生在监听前；运行时使用非阻塞 socket，不用 sleep 推进比赛。多地址主机选择首个能绑定/连接的地址，建议在同一容器演示时显式使用 127.0.0.1。

正式提交仍需团队自行完成 Development_Documentation.pdf（A4、单栏、11pt、页码、IEEE 引用、真实贡献合计 100%）及不超过 10 分钟全员参与的视频。本仓库不编造官方镜像测试证据、人员贡献、图像或截图。AI 辅助内容应按任务书要求审阅、验证并披露。

准备好实际 PDF 后生成任务书要求的目录结构（已存在的 ZIP 不会被覆盖）：

```sh
python3 tools/build_submission.py --output dist/CSI201-CW-Part-I-TeamName.zip --documentation /path/to/Development_Documentation.pdf
```

只导出代码供复验时使用：

```sh
python3 tools/build_submission.py --output dist/CSI201-code-only.zip --code-only
```

代码包包含 Codes/ 下的源码、题库、测试和说明，排除 .git、缓存、虚拟环境及无关附件；完整包额外包含 Documentation/Development_Documentation.pdf。`--code-only` 明确不构成完整作业提交。
