# busybar-claude-status（中文）

把 Claude Code 终端 StatusBar 的信息（Model、effort、context window、plan 用量）
和当前会话状态实时显示到 Busy Bar 的前置 LED 屏（72×16）上。

[English docs](README.md) ｜ 安装：`python3 setup_claude.py install`
（自动备份并接入 `~/.claude` 的 statusline 与 hooks，`uninstall` 可完整还原；
动画资产用 `python3 animgen.py anims/` 生成后经 `/api/assets/upload` 上传，
详见英文 README 的 Install 一节）。

## 显示布局

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
                        +--> daemon.py (127.0.0.1:8765 + 10.0.4.21:8765)
settings.json hooks ----'        |            |
                                 |            +--> GET /status（给设备端 JS 应用轮询）
                                 +--> 直推渲染 DIRECT_PUSH=True（当前启用）
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
- **claude 主题** — 已安装到设备 `/ext/apps_assets/busy/themes/claude/`
  （claude 橙 4s 呼吸环 + theme.json），在设备主题选择器里可见可选，
  也是 `theme` 模式的开关载体；会话运行时它就是屏幕上的兜底画面。
- **animgen.py** — 固件自研 `bicycle0` 动画格式（`.anim`）的 Python 编码器
  （BGRA8888 + RLE + 帧间合并 + default section），生成六个状态环动画并本地
  解码回环校验。资产已传至设备 `/ext/apps_assets/claude_status/`。
- **device_app/ + install_app.py** — 设备端 "Claude Status" JS 应用
  （Apps 菜单手动选择，轮询 `http://10.0.4.21:8765/status` 本地渲染）。
  **已就绪但被固件卡住**：JS 应用支持在 1.2.0-rc 才加入（设备现为 1.1.1
  稳定版，稳定通道暂无更新）。固件升级后运行 `python3 install_app.py`，
  然后把 daemon 的 `DIRECT_PUSH` 改为 `False`。
- **report.sh / screenshot.py** — 上报转发（自动拉起 daemon）/ 前屏截图调试。

## 常用操作

```bash
curl -s http://127.0.0.1:8765/status     # 渲染器视角的当前状态
curl -s http://127.0.0.1:8765/health     # 各会话原始快照
python3 screenshot.py /tmp/front.png     # 截取前屏
python3 animgen.py anims/                # 重新生成动画
python3 install_app.py                   # (固件>=1.2.0) 安装设备端应用
tail ~/.claude/busybar-daemon.log
```

接入点：`setup_claude.py install` 会在 `~/.claude/settings.json` 各 hook
事件上**并列追加**上报命令（不动你已有的 hooks），并让 statusline 命令
把 JSON 转发给 daemon（已有 statusline 则原样包裹，没有则装一个极简版）；
一切修改前自动备份，`uninstall` 完整还原。

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
  叠在先画的上面（文字可覆盖动画）。
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
