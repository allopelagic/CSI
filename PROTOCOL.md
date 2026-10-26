# 双人开发协议准则 v2

**Networked Quiz Tournament｜2026-10-26｜两端共同实现的唯一协议基线**

先读第1节了解规则；写代码查第2–5节；联调查第6–10节。所有字段和处理规则以本文为准，变更时同步两人、代码、测试和README。本文覆盖程序的通信、比赛和异常规则，按最高评分档准备；分数取决于实际实现与证据。

**状态：设计已确定，代码待同步。** 本文使用`version=1`

## 1. 一页看懂系统规则

| 项目 | 双方统一约定 |
|---|---|
| 技术 | Python 3.12标准库、直接TCP socket、纯命令行；官方Docker验证。每客户端一条长连接，所有操作经过服务器。 |
| 权威 | 服务器管理身份、房间、题序、时间、答案和分数；客户端只提交操作、接收并显示结果，不读题库。 |
| 身份 | 在线user_id和显示名均唯一，区分大小写、不自动去空格或归一化。重连必须重新登录。 |
| 房间 | 一人最多一房；创建即加入并成为房主；加入后未准备，原成员准备值保留。 |
| 开始 | 只有房主可以开始；至少2人，包括房主在内全员ready。 |
| 比赛 | 文件顺序发题；首次有效答案不可改；**全体当前活跃玩家均已有效作答就提前结束本题，否则到截止时间结束**。 |
| 得分 | 正确加题库points；错误、缺答0分；重复/迟交请求不新增得分，不撤销首次有效答案。 |
| 生命周期 | 只允许加入大厅；每房仅一场，不原房重赛；结束后查询/离开，离开后可建新房。 |
| 隔离 | 至少10客户端在线、两个房间同时比赛；题号、答案、计时、分数和事件互不串房。 |

房间稳定状态只有`LOBBY → QUESTION_OPEN → FINISHED`；中间题继续回到QUESTION_OPEN。关题/计分是内部一次性操作，不开放“等待下一题”状态。

## 2. 消息怎样传、怎样认

### 2.1 传输规则

| 项目 | 规则 |
|---|---|
| 一条消息 | `4字节大端无符号长度N + N字节UTF-8 JSON正文`；N不含头部，范围1–65536。 |
| 发送 | 紧凑JSON，ensure_ascii=False、allow_nan=False；编码成UTF-8后计算长度，部分send留待继续。 |
| 接收 | 每连接独立缓冲；收齐才解码，一次收到多条逐条取出，尾部半条保留。 |
| JSON | 顶层object；无BOM、重复键、NaN/Infinity或非有限数、非法UTF-8/孤立代理码点。 |
| 校验 | 下表及消息表列出的字段全部必填，未列字段拒绝；只有明确允许时可null。整数不接受bool或1.0。 |
| 坏帧 | 0/超长头、坏编码/JSON立即关闭受影响连接，不等待超长正文；其他连接继续。 |

### 2.2 公共六字段

| 字段 | 类型 | 请求 | 成功响应 | 主动事件 | ERROR_RESPONSE |
|---|---|---|---|---|---|
| version | integer，固定1 | 1 | 1 | 1 | 1 |
| type | string，1–64，ASCII大写及下划线 | X_REQUEST | X_RESPONSE | X_EVENT | ERROR_RESPONSE |
| request_id | 字符串或null | req-N | 原请求值 | null | 可关联则原值，否则null |
| event_id | 字符串或null | null | null | evt-N | null |
| session_id | SessionId或null | LOGIN为null；登录后填本人值 | 当前本人值；LOGIN成功为新值 | 接收者本人值 | 服务器已绑定本人值或null |
| payload | object | 参数，无参数时{} | 成功结果 | 更新内容 | code、message |

N为1–2147483647的整数，写作`req-1`、`evt-1`，不补零、不用前导零。每连接请求序号严格递增，可跳号；事件序号从1递增，只有成功排队才增加。响应不增加事件号；编号耗尽有界关闭，不回绕。

SessionId为服务器生成的`s-`加32位小写随机十六进制，绑定原TCP连接。登录成功时只从响应信封取得它，payload不重复。其他连接即使持有此值也不能继承身份；本版无密码、TLS或断线恢复。

```json
{
  "version": 1,
  "type": "ANSWER_REQUEST",
  "request_id": "req-8",
  "event_id": null,
  "session_id": "s-0123456789abcdef0123456789abcdef",
  "payload": {"room_code": "R000001", "question_id": "NET001", "answer": "B"}
}
```

## 3. 多条消息共用的数据结构

`ID`=1–32位ASCII字母/数字/_/-；`Name`=1–40个可打印Unicode字符且非全空白。`RoomCode`为R000001–R999999，进程内不复用；耗尽拒绝建房。所有字符串须为有效Unicode，字段名和值区分大小写。

`[]`表示JSON数组；表内字段集合精确。title/text可打印且非全空白，description可空/空格；长度按Unicode码点。题库字符口径为本组暂定解释，有课程澄清时两端一起调整。

| 对象 | 完整字段及类型/范围 | 顺序与约束 |
|---|---|---|
| Quiz | quiz_id:ID；title:string[1–100]；description:string[0–300]；question_count:int[5–50] | 目录1–20项，按文件顺序，ID唯一；不含题干/选项/答案。 |
| Player | user_id:ID；name:Name；ready:bool | 当前成员按加入顺序，ID唯一；不公开session_id。 |
| RoomSummary | room_code:RoomCode；quiz_id:ID；quiz_title:string[1–100]；host_user_id:ID；player_count:int[1–15] | 可加入列表0–32项，按房号升序；仅未满LOBBY。 |
| Room | room_code:RoomCode；quiz_id:ID；quiz_title:string[1–100]；state:房间状态；host_user_id:ID；players:Player[1–16]；current_question_id:ID或null | 房主必须在成员中；大厅/结束题号null，OPEN非null。仅给当前成员；不返回空房快照。 |
| Entry | rank:int[1–16]；user_id:ID；name:Name；score:int[0–50000]；active:bool | 榜单1–16项，按(-score,user_id)；竞争名次1/1/3；ID唯一。active表示原会话仍在本房。 |
| Options | A、B、C、D四键，值均string[1–200] | 恰好四项；控制字符允许，但显示时转义，不能执行终端控制。 |

容量固定16，不重复放进每条消息；`host_user_id`标识房主，不另设is_host；用房号和题号定位比赛，不另设game_id。

## 4. 请求与成功响应：十二个操作

下表名称X在网络上必须写成`X_REQUEST`和`X_RESPONSE`。成功响应回传原request_id，失败只发ERROR_RESPONSE，**没有success=false或accepted=false响应**。公共目录对所有已登录用户开放；私有查询仅限当前房成员。

| X / CLI | 请求payload完整字段 | 成功响应payload完整字段 | 服务端条件或动作 |
|---|---|---|---|
| LOGIN / 启动自动登录 | user_id:ID；name:Name | {} | 未登录且session_id=null；ID/名字未占用；绑定新Session。 |
| LIST_QUIZZES / /quizzes | {} | quizzes:Quiz[] | 已登录；仅公开目录。 |
| LIST_ROOMS / /rooms | {} | rooms:RoomSummary[] | 已登录；列表不预留名额，JOIN时重查。 |
| CREATE_ROOM / /create quiz_id | quiz_id:ID | room:Room | 无房→quiz存在→房数量和编号可用；自动入房，未准备。 |
| JOIN_ROOM / /join room_code | room_code:RoomCode | room:Room | 无房→目标存在→LOBBY→未满；追加成员，未准备。 |
| PLAYER_LIST / /players | {} | room:Room | 当前成员；统一返回房间快照。 |
| READY / /ready、/unready | ready:bool | room:Room | 当前成员且大厅；同值成功，无变化就不广播。 |
| START_QUIZ / /start | {} | room:Room | 当前成员→大厅→房主→≥2人且全员ready；返回已开放第一题的房间快照。 |
| ANSWER / /answer question_id A | room_code:RoomCode；question_id:ID；answer:A/B/C/D | room_code:RoomCode；question_id:ID；accepted:true | 按第6节检查，先记录并确认首次有效答案；若全体活跃玩家均已有效作答，立即关题结算。 |
| LEADERBOARD / /score | {} | room_code:RoomCode；leaderboard:Entry[] | 大厅为当前成员全零榜；比赛/结束为冻结参赛记录的已结算榜。 |
| LEAVE_ROOM / /leave | {} | room_code:RoomCode | 当前成员；移出房间但保持登录。 |
| LOGOUT / /quit | {} | {} | 释放身份并关闭；未登录也允许，但session_id须null。 |

房间返回值都复用第3节Room。/help只在本地显示；选项小写可转大写，ID不转换。登录失败提示并退出；依赖登录/入房/离房等结果的命令等确认后执行，本地状态只按响应/事件更新。非法命令/缺参数本地提示，重复/迟交仍须能从正式客户端演示服务器拒绝。

## 5. 服务器主动事件：六种

事件不要求客户端ACK。只向所属房间当前成员发送，除下面ROOM_CLOSED的停服通知。响应、事件、错误必须同时分发，不得等某个请求回复时丢掉中间事件。

| type | payload完整字段 | 时机与接收者 |
|---|---|---|
| PLAYER_STATUS_EVENT | room:Room | 创建、加入、准备值变化、成员离开/断线后，向变化后成员发最新快照。 |
| ROOM_CLOSED_EVENT | room_code:RoomCode；reason:恰好SERVER_SHUTDOWN | 停服时尽力通知房内成员；随后EOF。空房直接删除，主动离开不另发重复关房事件。 |
| QUIZ_STARTED_EVENT | room_code:RoomCode；quiz_id:ID；title:string[1–100]；total_questions:int[5–50] | START成功后向全体成员发一次，随后第一题。 |
| QUESTION_EVENT | room_code:RoomCode；question_id:ID；number:int[1–total_questions]；total_questions:int[5–50]；text:string[1–500]；options:Options；time_limit:int[5–60]；points:int[1–1000] | 当前题开放；不含正确答案或未来题。 |
| QUESTION_RESULT_EVENT | room_code:RoomCode；question_id:ID；correct_answer:A/B/C/D；your_answer:A/B/C/D或null；points_awarded:int；leaderboard:Entry[] | 关题后分别构造本人结果；正确得本题points，否则0；先结果再后题。 |
| QUIZ_FINISHED_EVENT | room_code:RoomCode；quiz_id:ID；final_leaderboard:Entry[]；winner:ID[1–16] | 最后RESULT之后一次；winner始终是所有最高分者数组，顺序同最终榜。 |

对同一连接使用FIFO队列：操作响应→其衍生事件；START响应→QUIZ_STARTED→QUESTION；RESULT→下一QUESTION或FINISHED。此前已排队的消息不插队。计时先发现过期时，RESULT/下一题可以排在迟交错误之前。

## 6. 比赛与离开规则

| 场景 | 唯一处理规则 |
|---|---|
| 开局 | 冻结原session、ID/名字与参赛名单；分数0；只读题库与每房进度分开。 |
| 开题 | opened_at=服务端monotonic()；deadline=opened_at+题库time_limit；每题读新时钟。 |
| 答题检查 | 成员/房号匹配→非大厅→已关题拒绝→必须当前题→clock()<deadline→原参赛会话仍活跃→没有有效答案，才存入。 |
| 错误与重复 | 非法选项不占本题机会；已接受答案不能改。不同请求号重复答仍拒绝，同请求号在公共层拒绝。 |
| 关题条件 | 当前题仍OPEN时，“全体当前活跃玩家均已有有效答案”和“deadline到达”任一满足即关题；非法选项不算已答。最后有效答案先排ANSWER_RESPONSE，再RESULT，再下一题或FINISHED。 |
| 到期 | 等于deadline也拒绝；以完整消息业务校验时刻为准，不采信客户端时间或半帧到达时刻。提前关题与到期共用一次性结算入口，重复触发不重复加分。 |
| 公布结果 | 开放期分数不变，ACK/榜单/错误/日志不透露本题正误；your_answer仅属于该接收者，无有效答案为null。 |
| 推进 | 提前关题或到期均自动推进；缺答不会阻塞，不用sleep。上题处理晚了，下一题仍从实际开题起获得完整的最大答题时限。 |
| 排名 | score降序，同分ID按ASCII升序；rank=高于其分数的人数+1。最高同分全部胜出，全零分则全部共同胜出。 |
| 普通离开 | 清session.room_code和当前成员关系；登录保留；开局记录active=false，已接受的当前答案照常结算，后题缺答0。 |
| 房主离开 | 转给加入最早且仍在房间者；比赛推进不依赖房主，剩一人继续。 |
| 离开后重查 | 离开/断线后，若房间非空且当前题仍OPEN，先更新成员/房主并发状态事件，再检查剩余活跃玩家是否均已有效作答；满足就提前关题。空房直接回收，不触发结算。 |
| 空房 | 无人即删除房间及期限；不模拟余题、不发无人接收的最终事件。 |
| 结束 | 保留最终榜供当前成员查询，直到空房；不能ready/start/join该房。公共目录仍可查。 |
| 重连 | 新Session，不恢复旧场资格；旧成绩按开局会话保留，新同ID不能替换旧参赛记录。 |

ANSWER中的room_code必须等于当前房，防止旧房答案写进新房；每房仅一场且房号不复用，所以不需要game_id。

## 7. 错误与校验顺序

ERROR_RESPONSE的payload恰好`{code:string, message:string[1–200]}`，code只取下表14项。message须可打印，不含token/堆栈/未公布答案；程序只看code判断。下列错误均保留连接。

| code | 使用条件 |
|---|---|
| INVALID_MESSAGE | 公共字段/版本错误、编号重复/回退、未知type、专用字段缺失/多余/错类型或范围。 |
| INVALID_SESSION | 普通业务未登录，或token缺失/与本连接不匹配；未登录LOGIN/LOGOUT携带非null token。 |
| INVALID_STATE | 重复LOGIN、无房操作、非大厅ready/start/join、ANSWER房号不匹配、房数/房号额度耗尽等。 |
| DUPLICATE_USER | 在线user_id已被使用，原用户不受影响。 |
| DUPLICATE_NAME | 在线显示名已被使用。 |
| QUIZ_NOT_FOUND | CREATE指定题目集不存在。 |
| ROOM_NOT_FOUND | JOIN指定房间不存在。 |
| ROOM_FULL | JOIN的大厅已有16人。 |
| ALREADY_IN_ROOM | 已有房间仍CREATE/JOIN。 |
| NOT_ROOM_HOST | 大厅中非房主申请START。 |
| PLAYERS_NOT_READY | 房主申请START时人数<2或至少一人未ready。 |
| QUESTION_CLOSED | 本场该题已提前关闭、截止已到或FINISHED合法旧题；早关题后再提交也返回此错误。 |
| DUPLICATE_ANSWER | 当前题仍OPEN，原会话已有有效答案。 |
| INVALID_ANSWER | answer是字符串但非A/B/C/D；或题号不是已关题也不是当前题。 |

**固定检查链：**完整帧/JSON→六字段基础格式和版本→请求序号新旧→立即记住新序号→type及payload→身份→按操作表检查业务→一次性修改状态并生成输出。

公共结构/版本非法不消耗编号；旧编号不再消费；合法公共结构的新编号先消费，之后失败也保留。token格式错属INVALID_MESSAGE，格式对但空/不匹配属INVALID_SESSION。错误能安全提取合法request_id就回传，否则null；session_id只放服务器绑定的本人值或null，不回显伪造token。LOGIN先检查未登录，LOGOUT允许未登录null，其他请求均需有效绑定。

同请求只返回成功或错误之一。LOGIN成功信封带新token；LOGOUT成功信封保留刚退出的本人token快照，身份已释放，未登录则null。客户端成功响应须匹配pending ID和对应type，关联错误匹配ID，null错误只提示；未知/重复关联响应或不合规服务器消息报告并关闭。

## 8. 并发、缓存与关闭底线

下列是本组配置，集中放常量；不是老师给定的性能指标。服务器单线程selectors拥有可变业务状态；客户端输入线程只写命令队列，主线程负责网络、状态和显示。

| 项目 | 固定值与行为 |
|---|---|
| 容量 | 最多64连接（含未登录/关闭中）、32房、每房16人；超连接数关闭新连接，房满/房数满按第7节拒绝。 |
| 接收/发送 | RX最多128KiB保留wire字节，含头部；按剩余空间recv，满则暂停、消费后恢复。TX最多256KiB未发字节，将超限关闭该慢连接。 |
| 公平 | 每连接每轮读/写各32KiB、处理8帧；全服每轮accept≤16。已有完整帧主动续处理，不等新可读事件。select≤0.1秒且不晚于最近期限；不是总响应延迟保证。 |
| 三种10秒 | 登录从accept起绝对10秒；半帧从首字节应用接收起绝对10秒，滴入不续期；TX非空无实际send进展10秒。完整排队帧不算半帧，等待答题不算发送停滞。 |
| 客户端有界 | 连接超时5秒；pending最多64条，5秒后警告一次、不自动重发。输入队列128条，单行512字符，超长整行丢弃，每轮最多8条命令。 |
| 断线清理 | 标记closing后立即停业务，包括缓存请求；当前转换和输出快照结束后再清成员。清理广播再失败用去重队列迭代，不递归改房间；身份/资源仅释放一次。 |
| 正常退出 | LOGOUT确认/必要停服通知最多刷新0.5秒；旧队列保持FIFO，停止新业务；停服不再推进比赛。Ctrl+C关闭监听/全部连接/selector；客户端EOF正常结束；输入线程不阻碍退出。 |

输出必须复制收件人和payload深快照；每连接仅主线程排队发送并维护部分发送偏移。RX满且TX空时暂时unregister，保留待处理帧，释放容量后register，不能设置selector的events=0。客户端即使pending满也能有界退出。测试驱动持续读取stdout/stderr；日志不记session_id、开放题答案或用户选项，避免大量同步输出。

## 9. 题库与启动：必须同时遵守

| 对象/字段 | 强制类型与范围 |
|---|---|
| 文件/根 | UTF-8 JSON object，非空，≤1MB（本版暂按1000000字节）；schema_version必须是真整数1。 |
| quizzes | array，1–20个；保留文件顺序。 |
| quiz_id、question_id | string，1–32；本版暂按ASCII字母/数字/_/-。quiz_id文件内唯一，question_id跨全部quiz唯一。 |
| title、description | 必填string，分别1–100、0–300可打印字符；title非全空白，description允许空。 |
| questions | 每quiz array，5–50题，保留顺序。 |
| text、options | text为1–500可打印非全空白字符；options恰好A/B/C/D，每值string长1–200，不额外限制可见性。 |
| answer、time_limit、points | answer恰好A/B/C/D；time_limit真整数5–60秒；points真整数1–1000。 |
| 额外字段 | 根/quiz/question允许额外成员；options例外。任一必填值非法，整库失败，不能跳过坏题。 |

只有服务器读库；先完整校验成功再监听并flush输出SERVER_READY。失败清晰诊断、非零退出、不监听、不输出ready。隐藏合法库可只有一个quiz；本组提交库至少两个quiz，每个至少五题。

启动接口不变：

```text
python3 server.py --host <server_IP> --port <port> --questions <json_path>
python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
```

端口1–65535由参数指定；路径为相对或参数路径，不依赖挂载目录外文件。不安装第三方包，不用GUI、socketserver/高层服务器框架、外部数据库/互联网服务。

## 10. 两人怎样对接、怎样判定完成

| 负责人 | 交接内容 |
|---|---|
| 双方共同 | 先固定本文字段、错误、Room结构与规则；protocol.py由一人主维护，另一人审阅，双方导入同一份。 |
| 服务器负责人 | server.py、server_network.py、game.py；维护身份、房间、计时计分和网络资源。 |
| 客户端负责人 | client.py（含输入和显示）、question_bank.py及题库；负责命令→消息与异步显示。 |
| 双方交叉验证 | 模块自测后，用真实两端完成比赛；各自提供真实测试、README和报告材料，贡献按实际工作合计100%。 |

联调顺序固定为：**登录/退出→目录/房间/准备→第一题→整局→十客户端双房间→异常/发布。**

- [ ] 所有12请求/12响应/6事件/14错误按本文处理；响应匹配pending，事件编号按连接递增。
- [ ] 分片、合并、坏长度/JSON、字段上下界、错误token与重放编号不破坏其他用户。
- [ ] 用不同题数/题序/时限/分值的合法替换库跑完整局；无硬编码，开放期无答案或分数泄露。
- [ ] 正确/错误/重复/缺答/迟交、全员已答提前关题、未全答到期、最后答案ACK先于结果、离开后重查、截止相等、双触发不重复计分、下一题完整最大时限、并列/全零榜均正确。
- [ ] 房主/普通成员离开、先答后断线、旧ID重登、单人继续、空房和Ctrl+C清理正确，同端口可重启。
- [ ] 至少十个真实client在线，两房选不同及相同quiz各跑一次；坏帧/慢连接不影响另一房。
- [ ] 官方Docker干净目录按README复现；提交代码、题库、测试、README、开发PDF及≤10分钟全员参与视频，实际结果与同一版本一致。

**相对参考附件的有意选择：**保留TCP与六字段、字符串编号和session；按附件全员有效作答或到期关题；用room_code代game_id；winner统一数组；用共享Room减少重复结构；正常响应省略success；停服以ROOM_CLOSED及EOF处理。以上属于本组设计，附件不是强制协议。

来源：[协议参考附件](</Users/zhoutianen/Desktop/Y3S1/SOE203/CAN201_Networked_Quiz_Tournament_Appendix_Application_Layer_Protocol (1).pdf>)、[主任务书](</Users/zhoutianen/Downloads/Coursework I Task Sheet.pdf>)。老师对1MB、字符口径或课程版本的澄清优先；官方镜像名称/tag需核实。本文不替代正式报告；AI辅助内容须理解、验证并披露，不用生成式AI制作提交用图片、示意图或截图等视觉证据。
