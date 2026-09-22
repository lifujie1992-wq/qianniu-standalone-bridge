---
project: Qianniu AI Customer Service Assistant
register: product
aesthetic_direction: industrial / signage
color_strategy: restrained
design_system: Windows native controls guided by Fluent 2
design_variance: 4
motion_intensity: 2
visual_density: 7
---

# Design Read

像一块可靠的设备状态牌：先看清是否在工作，再决定是否操作。

## Signature

“状态信号牌”是唯一的视觉记忆点。托盘图标、控制面板标题区和右键菜单首行共享同一组状态词：`运行正常`、`正在启动`、`连接异常`、`已停止`。颜色只辅助文字，不单独承担含义。

## Color (locked)

| role | OKLCH | hex | use | contrast |
|---|---|---|---|---|
| background | 0.965 0.008 165 | `#EFF5F1` | window background | text 13.1:1 |
| surface | 0.995 0.003 165 | `#FCFEFD` | panels and menus | text 14.6:1 |
| elevated | 0.935 0.012 165 | `#E4EEE8` | selected/hover rows | text 11.8:1 |
| text | 0.245 0.025 165 | `#183029` | primary text | background 13.1:1 |
| muted | 0.485 0.025 165 | `#60766E` | secondary text | background 4.8:1 |
| subtle | 0.60 0.018 165 | `#82948D` | quiet metadata | surface 3.2:1, large text only |
| border | 0.86 0.015 165 | `#CBD9D2` | dividers and control borders | UI 3.1:1 |
| accent | 0.49 0.115 165 | `#087A5B` | primary action and active state | white 4.7:1 |
| success | 0.49 0.105 158 | `#14765A` | healthy state | white 4.6:1 |
| warning | 0.56 0.145 70 | `#AD6200` | starting/degraded state | white 4.5:1 |
| danger | 0.50 0.16 20 | `#BD3F50` | exit and failed state | white 4.6:1 |
| info | 0.51 0.13 255 | `#2868B7` | links and diagnostics | white 4.7:1 |

## Type (locked)

| role | family | use | notes |
|---|---|---|---|
| display | Microsoft YaHei UI Semibold | status and window title | 16–20 px, restrained |
| body | Microsoft YaHei UI | labels and explanations | 12–14 px |
| utility | Cascadia Mono, Consolas | ports, PIDs, version and timestamps | 10–12 px |

The pairing is humanist UI text with monospace operational data.

## Scales (locked)

- spacing: `0, 4, 8, 12, 16, 20, 24, 32, 40`
- radius: `{sm: 4, md: 8, lg: 12, full: 9999}`
- motion: `{fast: 120ms, base: 220ms, emphasis: 400ms}`, easing `cubic-bezier(0.16, 1, 0.3, 1)`
- icon family: Windows system status icons and one application tray icon vocabulary
- reduced motion: no animation; status changes update immediately

## Voice

- register: plain, operational, unambiguous
- action vocabulary: `打开控制中心`、`启动服务`、`重新启动`、`彻底退出`
- successful outcomes use past tense: `已启动`、`已停止`、`已彻底退出`
- errors name the failed component and give one recovery action

Every screen must read as the same product if placed side by side.
