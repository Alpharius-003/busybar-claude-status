# busybar-claude-status（中文）

把 Claude Code 终端 StatusBar 的信息（Model、effort、context window、plan 用量）
和当前会话状态实时显示到 Busy Bar 的前置 LED 屏（72×16）上。

[English docs](README.md) ｜ 支持 macOS / Linux / **Windows**（Windows 用
`py setup_claude.py install`，胶水层为纯 Python 无 bash 依赖；硬件实测：
macOS 直连 Bar，Windows 作为枢纽客户端经 Wi-Fi 转发；备用枢纽在 Mac 上用第二个 daemon 冻结枢纽验证，尚未在真实 Windows 上跑过）｜ 安装：`python3 setup_claude.py install`
（自动备份并接入 `~/.claude` 的 statusline 与 hooks，`uninstall` 可完整还原；
动画资产用 `python3 animgen.py anims/` 生成后经 `/api/assets/upload` 上传，
详见英文 README 的 Install 一节）。

## 双样式（BUSYBAR_STYLE，可写入 env.sh 持久化）

- **`minimal`**（默认）——状态词 + 配额常驻，一屏尽览：

  ![minimal](docs/img/working.png)

- **`avatar`**——1:1 复刻终端 Clawd 的像素小形象在右侧演出状态（打字带
  眨眼 / 灯泡思考 / 咖啡休息 / X 眼报错 / zzz 睡觉）+ 竖向 ctx 量条；
  左下平时显示状态词，完工后换成配额：

  ![avatar](docs/img/avatar-working.png)

样式是运行时配置而非分支，每个 Release 都同时包含两种。

## 显示布局（minimal）

```
████████████████████████████   1px 环形灯带：预渲染 .anim 由固件原生 25fps 播放
█  Fable 5 max      [██----] █   模型+effort（/effort 档位色）│ ctx 进度条
█  5h88% 7d98%        WORK  █   plan 剩余（配对紧凑格式）│ 状态词（状态色）
████████████████████████████
```

**环形动画**（固件原生播放，与内置 keep_out 主题同一解码器，丝滑度一致）：
- WORKING — 彩虹跑马灯（Claude Code 主题 rainbow_* 七色，逐像素渐变旋转，3.2s/圈）
- THINKING — effortUltra 紫双波峰行波（2s 周期）
- COMPLETE — 绿色呼吸（2.8s；30 秒后回落 IDLE）
- WAIT — 橙色急促脉冲（0.88s）+ 设备状态 LED 同闪
- ERROR / FAILED — 红色 2Hz 爆闪
- IDLE — 暗灰常亮；空闲 10 分钟后清屏交还设备

**effort 档位色**（取自 Claude Code CLI 主题色板）：low 灰 `inactive` /
medium 蓝 `permission` / high 黄 `warning` / xhigh 橙 `fastMode` /
max 紫 `effortUltra`(175,135,255)。

**ctx 进度条**：20×4px，填充随占用率变色（<50 绿 / ≥50 黄 / ≥80 橙 / ≥90 红）。
**plan 剩余**：`5h88% 7d98%` 配对格式，任一窗口 ≤25% 剩余转橙、≤10% 转红。

## 架构

```
statusline-command.sh --.
                        +--> daemon.py (127.0.0.1 + 10.0.4.21 :8765；--lan 后 0.0.0.0)
settings.json hooks ----'        |            |
                                 |            +--> GET /status（给设备端 JS 应用轮询）
                                 +--> 直推渲染（RENDER_MODE=auto|theme|off）
                                        anim 元素换文件 + 文本/进度条增量更新
```

- **daemon.py** — 数据枢纽 + 直推渲染。`RENDER_MODE`（环境变量
  `BUSYBAR_RENDER_MODE` 可覆盖）：
  - `auto`（默认）— Claude 活跃就显示（插上即用）；
  - `theme` — **设备端手动开关**：只有当设备当前选中的 BUSY/CUSTOM 主题是
    "claude" 时才显示。设备的主题选择器（CUSTOM → SETUP → 主题）就是开关；
    配合 `claude_card.py install` 可把 CUSTOM 实体键的卡片换成 "Claude"
    （自动备份原卡片，`restore` 还原）。注意：1.1.1 上任何专注会话运行期间
    canvas 被完全屏蔽（优先级 100 也被拒），显示会暂停、会话结束后恢复；
  - `off` — 仅做数据桥（供未来 ≥1.2.0 的设备端 JS 应用轮询 /status）。
  渲染开销极小：状态变化才换 .anim、文本变化才重发。
- **claude 主题** — `python3 install_theme.py` 安装：claude 橙呼吸环 +
  居中打字的小形象，在设备主题选择器里可见可选，也是 `theme` 模式的开关
  载体；专注会话运行时它就是屏幕画面。注意 `auto` 模式下主题与状态屏无关。
  空闲 10 分钟清屏可用 `BUSYBAR_IDLE_CLEAR_S` 调整（0=永不清屏）。
- **animgen.py** — 固件自研 `bicycle0` 动画格式（`.anim`）的 Python 编码器
  （BGRA8888 + RLE + 帧间合并 + default section），生成六个状态环动画并本地
  解码回环校验。资产已传至设备 `/ext/apps_assets/claude_status/`。
- **device_app/ + install_app.py** — 设备端 "Claude Status" JS 应用
  （Apps 菜单手动选择，轮询 `http://10.0.4.21:8765/status` 本地渲染）。
  **已就绪但被固件卡住**：JS 应用支持在 1.2.0-rc 才加入（设备现为 1.1.1
  稳定版，稳定通道暂无更新）。固件升级后运行 `python3 install_app.py`，
  然后把渲染模式设为 `BUSYBAR_RENDER_MODE=off`。
- **report.sh / screenshot.py** — 上报转发（自动拉起 daemon）/ 前屏截图调试。
- **多工具扩展**：核心与 Claude 解耦——任何工具（Codex、Cursor、CI 脚本）
  一条 curl 调 `POST /v1/report`（标准化字段：state/label/label_color/
  context_pct/quotas）即可接管显示；Claude 的 effort 配色与 5h/7d 语义
  全部在内置适配器里。传输层 `BUSYBAR_TRANSPORT=usb|wifi|cloud` 可切换，
  BLE 方案已完成设计。详见 [docs/EXTENDING.md](docs/EXTENDING.md)。

## 常用操作

```bash
curl -s http://127.0.0.1:8765/status     # 渲染器视角的当前状态
curl -s http://127.0.0.1:8765/health     # 各会话原始快照
python3 screenshot.py /tmp/front.png     # 截取前屏
python3 animgen.py anims/                # 重新生成动画
python3 install_app.py                   # (固件>=1.2.0) 安装设备端应用
python3 setup_claude.py install --lan    # 本机 daemon 接受局域网上报（多机共用）
tail ~/.claude/busybar-daemon.log
```

接入点：`setup_claude.py install` 会在 `~/.claude/settings.json` 各 hook
事件上**并列追加**上报命令（不动你已有的 hooks），并让 statusline 命令
把 JSON 转发给 daemon（已有 statusline 则原样包裹，没有则装一个极简版）；
一切修改前自动备份，`uninstall` 完整还原。

## 多台电脑共用一个 Bar（Mac + Windows 自动切换）

Mac 和 Windows 各开着若干 Claude Code，一块屏幕跟着你走：插着 Bar 的那台
把 daemon 当**枢纽**跑，其余电脑什么都不跑，hooks 和 statusline 经局域网
转发给枢纽。

```bash
# 插着 Bar 的电脑（枢纽）
python3 setup_claude.py install --lan

# 其他每台电脑（Windows 用 py setup_claude.py ...）
python3 setup_claude.py install --hub http://<枢纽主机名>.local:8765 --tag "#00A4EF"
```

- `--lan` 让枢纽监听 `0.0.0.0:8765`（`BUSYBAR_LISTEN`）；`--hub` 在客户端
  写入 `BUSYBAR_HUB`，该机不起本地 daemon，`report.py` 直接 POST 到枢纽
  （每次 hook 最多耗时 1.2 s，枢纽不可达则退避 20 s 只试一次，枢纽睡着
  也不会拖慢 Claude Code）。两者都持久化在 `env.sh`，正在跑的枢纽 daemon
  会被自动重启。
- `<枢纽主机名>.local` 是枢纽的 Bonjour/mDNS 名（macOS：系统设置 → 通用 →
  共享 → 本地主机名；Windows 10 1703+ 原生能解析 `.local`）。解析不了就
  换成枢纽 IP，并在路由器里给它绑定 DHCP 保留地址。
- `--tag` 标记这台电脑的会话：`#RRGGBB` 颜色 = 在模型名左侧的两列空位画
  一面 2×5 小旗（不占字符）；一两个字母（`--tag W`）= 写在模型名后面，
  必要时缩短模型名（`Fabl 5 max W`）。
- `--token 密钥`（枢纽和所有客户端同值）让枢纽拒绝没带密钥的局域网上报，
  本机回环永远免检；默认关闭（面向家庭网络）。枢纽若开了防火墙，放行
  Python 的 TCP 8765 入站。
- 客户端上的 Codex 同样适用：适配器直接上报到枢纽。

**显示谁？** 跟着注意力走，不跟着动静走：正在干活的会话里，你最后说过
话的那个占屏——你提交的提示词、权限询问、从空闲开始的新任务会拉走屏幕；
工具调用和 statusline 刷新永远不会。它空闲后才轮到仍在跑的后台会话；
全都空闲时停在你最后说话的那个。`GET /status` 带 `host` / `host_tag`，
`GET /health` 列出每个会话的 `focus_ts`。

### 枢纽休眠怎么办：备用枢纽（standby）

枢纽通常是台笔记本，合盖即睡，屏幕就黑了——除非另一台电脑做**备用枢纽**：
它自己跑一个 daemon，枢纽醒着时把本机会话镜像给枢纽，枢纽一消失就经 Bar
自己的 Wi-Fi 亲自画屏。其他一概不变：只要枢纽醒着，显示谁仍由枢纽决定。

一次性准备，在插着 Bar 的电脑上经 USB 做：把 Bar 连上你的 Wi-Fi（BUSY
App → Wi-Fi；`curl http://10.0.4.20/api/wifi/status` 能看到它的局域网地址），
再给它的 Wi-Fi API 设一个 key：

```bash
curl -X POST 'http://10.0.4.20/api/access?mode=key&key=1234567890'
```

然后在备用电脑上（Windows 用 `py setup_claude.py ...`）：

```bash
python3 setup_claude.py install --hub http://<枢纽主机名>.local:8765 --standby \
    --transport wifi --device <Bar 的局域网 IP> --device-token 1234567890 --tag "#00A4EF"
```

- 连续 3 次探测失败（约 10 s）才接管——而且只在 Bar 本身仍可达时才计数，
  所以备用电脑自己从休眠醒来绝不会误画到活着的枢纽头上；枢纽报告自己
  够不着 Bar（USB 被拔）时也会接管。枢纽一应答就立刻交还：先同步，再让
  枢纽重绘，最后自己停笔。备用端 `GET http://127.0.0.1:8765/standby` 看它
  的判断；任一 daemon 的 `GET /hub` 看角色、样式和设备连通性。
- 会话镜像传"年龄"而非时间戳（两台电脑的时钟可能差几秒），`state` 只在
  变化时发（连旧版 daemon 的枢纽也能像 hooks 直达时那样仲裁；租约和
  `/redraw` 需要新版），带 90 s 租约、每 30 s 续一次——备用端消失，它的会话
  随之消失；枢纽重启、或枢纽睡着期间忘了某个会话，几秒内就被察觉并重新
  同步。
- 两台电脑的 `--style` 保持一致（不一致备用端日志会提醒）；给 Bar 和枢纽
  绑 DHCP 保留地址。这个 key 等于把 Bar 的完全控制权交给同一 Wi-Fi 上的
  任何人：用 10 位数字、只在可信网络用、电脑丢了就经 USB 换 key。
  `--no-standby` 把备用端改回纯转发。
- `install` 结束时会探测枢纽和 Bar（用刚写入的凭据），key 写错、端口没开
  当场就能看到。

## 固件坑位实录（1.1.1）

- **往 `/ext/user_assets/<app>/appmeta/` 写 manifest.json 或二进制内容会让
  固件崩溃重启**（看门狗 5.3s）——JS 应用扫描路径是半成品。纯 ASCII 普通
  文件名不触发。固件 ≥1.2.0-rc 才正式支持 JS 应用安装。
- rectangle 默认 1px 白描边（未文档化 `border_width`/`border_color`），细矩形
  必须 `border_width: 0`。
- `/api/screen` 帧缓冲为 BGR 序；draw 文本仅 ASCII；small 字体为比例字体
  （数字≈3.8px），布局需真机实测（`5h88% 7d98%`≈44px、`WORK`=20px）。
- storage API：write=POST(raw body)、remove=**DELETE**、rename 参数为
  `path`+`new_path`（跨目录可用，但移入 appmeta 的受监视文件名会被 400 拒）。
- 动画元素：`stock_path:"shared/<file>.anim"` 或 `path:"<file>.anim"`
  （相对 `/ext/apps_assets/<application_name>/`），`loop:true`；后画的元素
  叠在先画的上面（文字可覆盖动画）。正在播放的 .anim 无法覆盖上传
  （"Failed to open file for writing"），需先清掉元素释放文件句柄。
- **专注会话运行期间 canvas 全被屏蔽**：文档称会话优先级 90、draw 接受
  1–100，但实测会话运行时优先级 91/95/99/100 一律 409——1.1.1 没有任何
  办法在会话画面上叠加内容。
- 会话控制走 `PUT /api/busy/snapshot`（必填 `card_id`、`is_paused`、
  `snapshot_timestamp_ms`；PUT `type:NOT_STARTED` 可结束会话）。
  `/api/input?key=off` 的 API 短按有时无法结束会话（实体按键不受影响）。
- BUSY/CUSTOM 两个实体键各绑定一个 profile（`/api/busy/profiles/{busy|custom}`
  GET/PUT），`claude_card.py` 就是改 custom 槽位。
- snapshot 的 `busy_bar_settings.theme` 在会话结束后仍保留最近选择，
  可当作设备端持久开关读取（`theme` 渲染模式的原理）。
