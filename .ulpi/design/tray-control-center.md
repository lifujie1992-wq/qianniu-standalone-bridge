# Tray Control Center

Bound to [DESIGN.md](./DESIGN.md). Every screen must read as the same product if placed side by side.

## Primary flow

Goal: let a non-technical operator know whether the assistant is running and give them one reliable place to open, restart, or completely exit it.

```text
[Launch shortcut]
      |
      v
[Acquire single-instance ownership]
   | existing instance -> [Bring control center forward]
   v
[Start bridge + dock + bundled Qianniu]
      |
      v
[Tray icon remains visible] <---- status poll ----> [healthy / starting / degraded / stopped]
      |
      +-- double click -> [Control center]
      +-- right click -> [Status menu]
                             +-- Open control center
                             +-- Open workbench
                             +-- Restart all
                             +-- Settings
                             +-- Completely exit
```

## State model

| state | tray tooltip/menu | control center | available primary action |
|---|---|---|---|
| starting | `正在启动` with warning icon | progress copy, actions disabled | wait |
| healthy | `运行正常` with success/info icon | bridge, Qianniu and dock all marked running | open workbench |
| degraded | `连接异常` with warning/error icon | failed component and last error visible | restart all |
| stopped | `已停止` with neutral icon | components marked stopped | start service |
| exiting | `正在彻底退出` | controls disabled | none |

Status refreshes every 2 seconds. A failed poll does not immediately show stopped; two consecutive failures produce degraded, and absence of managed processes produces stopped.

## Components

### TrayStatusIcon

- Purpose: persistent, glanceable ownership and the application’s canonical exit point.
- Interaction: single right click opens menu; double left click opens the control center.
- Tooltip: `千牛 AI 客服助手 · <status>`; never color-only.
- Accessibility: Windows shell tooltip and menu text expose the same status; menu supports arrow keys, Enter, and Escape.
- Edge cases: if Explorer restarts, recreate the icon; always remove it on normal exit.

### TrayContextMenu

- First line is a disabled status label.
- Commands in order: `打开控制中心`, `打开工作台`, `重新启动全部`, `设置`, separator, `彻底退出`.
- During starting/restarting/exiting, conflicting commands are disabled.
- `彻底退出` shows one confirmation: `将关闭客服助手、浮层和随包千牛。确定退出？`
- Acceptance: choosing exit removes the tray icon, stops bridge/dock, closes their Edge window, terminates only Qianniu processes whose executable path is inside this installation’s `runtime` directory, and removes owned PID files.

### NativeControlCenter

- Native Windows product window, 460×360 minimum, resizable vertically only if needed.
- Header contains the large status signal and version. No decorative cards.
- Body is a divided list: `桥接服务`, `千牛客户端`, `右侧浮层`, each with textual state and diagnostic detail.
- Footer has one primary action determined by state (`打开工作台` or `启动服务`) and quiet secondary actions (`重新启动`, `设置`).
- Window close hides to tray; it does not exit. Copy: `程序仍在右下角运行，可右键图标彻底退出。`
- Keyboard: Tab order follows visual order; Enter activates focused button; Escape hides the window; Alt+F4 hides to tray.

## Failure and edge handling

| scenario | required handling |
|---|---|
| shortcut clicked twice | second instance must not start components; it asks the owner to show its control center and exits |
| port 42110 occupied by owned bridge | attach to the healthy instance |
| port 42110 occupied by another process | show degraded state with port/PID; never claim startup success |
| stale PID file | validate PID plus executable path before terminating or deleting |
| bridge starts but a required listener fails | fail the whole bridge process and return degraded state |
| Explorer restarts | add tray icon again after `TaskbarCreated` |
| normal shutdown/logoff | run bounded cleanup; do not block Windows indefinitely |
| control center closed | hide to tray and show at most one explanatory notification per install |

## Build handoff

- Target engineering agent: Windows Python desktop engineer.
- Design system: Windows native common controls, following Fluent 2 interaction conventions. Use actual native menu, focus, confirmation, and window behavior; do not hand-recreate web components.
- Implement exactly this spec. Theme the design system with our locked tokens; do NOT redesign or re-implement its components.

## Acceptance criteria

- [ ] One default launch creates exactly one tray owner, one bridge and at most one dock.
- [ ] Repeated launch does not produce Windows error 10048.
- [ ] Tray tooltip and menu expose healthy, starting, degraded and stopped states in text.
- [ ] Double click opens the control center; closing the window keeps the tray process running.
- [ ] Right-click `彻底退出` stops bridge, dock, bundled Qianniu and tray, then removes owned PID files.
- [ ] No process outside this installation directory is terminated.
- [ ] Existing configure, status, update and dock CLI commands continue to work.
- [ ] Unit tests cover status mapping, single-instance behavior, restart and scoped cleanup.

## Design pre-flight

- Identity lock: passed; all values are from `DESIGN.md`, one accent/radius/icon/type vocabulary.
- Anti-slop: passed; no gradients, glass, generic cards, fake metrics, buzzwords or decorative motion.
- State/edge coverage: passed; start, healthy, degraded, stopped, exiting, duplicate launch, stale PID and Explorer restart are specified.
- Accessibility: passed; all states have text, native keyboard behavior, visible focus, AA contrast and reduced-motion behavior.
- Layout craft: passed for a compact utility window; signal header, divided component list and action footer are three distinct families.
- Cognitive load: passed; five menu commands and one primary action per state.
- Self-critique: distinctiveness 3, hierarchy 4, consistency 4, accessibility 4, state coverage 4, copy 4, restraint 4, motion 4. Total 31/32; no axis below 3.
- Revise-and-justify: exit copy was changed from generic `退出` to `彻底退出` so scope is explicit before destructive cleanup.
