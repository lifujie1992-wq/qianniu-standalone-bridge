# Brain Workbench

This feature is bound to [DESIGN.md](DESIGN.md). No visual values outside the locked tokens are allowed.

## Primary Flow

1. Operator opens `127.0.0.1:18767`; local sessions are useful even when the brain is offline.
2. Operator opens Settings, enters brain URL, seat token, optional agent identity, and enables AI.
3. Save validates locally, persists atomically, and wakes the connector without restarting receive/send.
4. New buyer events are committed locally, enriched if possible, then queued to the brain.
5. Brain commands are claimed durably by `command_id`; `send_text` uses that ID as the send idempotency key.
6. Safety, shop scope, and per-session AI/human mode are checked before the existing send path is called.
7. Result is reported until the brain durably acknowledges it. The command is never executed twice.
8. The signal rail exposes each boundary and its latest error/latency/raw receipt.

Historical messages that predate brain enablement are not uploaded automatically. Events captured while
an already-configured brain is offline remain queued and retry after reconnection.

## Layout Families

- Desktop: fixed session index, flexible conversation, fixed evidence rail.
- Medium: session index plus conversation; evidence rail opens as an inspector drawer.
- Narrow: session index or conversation is shown at a time; settings and evidence are full-width drawers.
- Dock mode: session index only. Clicking a row may open Qianniu; main workbench clicks never do.

## States

| Area | Loading | Empty | Success | Error / Degraded |
|---|---|---|---|---|
| Sessions | stable skeleton rows | “暂无接待消息” | ordered by latest timestamp | local DB error in status strip |
| Conversation | fixed-height progress | prompt to select | messages plus receipt state | failed/unknown send stays visible |
| Brain | “正在注册/重连” | “大脑未配置” | registered and heartbeat timestamp | exact HTTP/status error plus retrying |
| Event queue | count remains stable | 0 waiting | last ACK and latency | retry count, oldest age, last error |
| Commands | polling state | no command yet | latest result and ACK | blocked/rejected/indeterminate reason |
| Context | subtle loader | “暂未获取商品/订单” | product/order facts | enrichment error does not block messages |

Refresh preserves the selected conversation in memory. Network loss never disables drafting. A settings
save error keeps entered values. An existing token is never returned to browser JavaScript.

## Components

### Connection Strip

Two compact status groups: Qianniu and Brain. Each has a status dot, label, last successful activity,
and button opening its evidence. `aria-live="polite"`; status includes text, not color only.

### Session Index

Search plus button rows with stable avatar, name, preview, time, and AI paused marker. Arrow keys may move
focus; Enter selects. Long text truncates; IDs remain available in the details pane.

### Conversation Surface

Header identifies buyer and account. Message bubbles show time, direction, durable status, and source.
Composer remains editable while transport is unavailable; Send explains why it cannot submit. Ctrl+Enter
sends. Pending, confirmed, unknown, and rejected states have distinct labels.

The header uses a compact `AI / 人工` segmented control. Manual handoff pauses only brain commands; the
operator can still type and send. Automatic handoff reasons and expiry are reflected in the same control.

### Context Ledger

Unframed key/value region for current goods and orders. Missing fields are omitted. URLs are external links
with descriptive labels. Raw context is expandable and uses mono type.

### Signal Rail

Chronological entries have stage, timestamp, state, detail, and optional expandable JSON. It supports
local receive, brain upload, command claim, SDK submission, and confirmation. Empty state explains only
that no evidence exists yet. Failed entries expose retry state without an automatic resend control.

### Brain Settings Drawer

Fields: server URL, token, agent ID, agent name, enabled toggle, AI reply toggle, optional remote open-chat
toggle. Token placeholder says “已保存；留空则不修改”. Save is the sole primary action. URL and token errors
are inline. Escape closes; focus returns to trigger; saving announces result.

## Acceptance Criteria

- Existing receive identity, local durability, MessageSDK send adapter, and dock click boundary do not change.
- Brain protocol matches the legacy five endpoints and authentication headers.
- Registration, heartbeat, event delivery, command result ACK, and queue state are visible.
- New events are local-first and retry through brain outages; pre-enable history is not flushed.
- A repeated `command_id` cannot invoke Qianniu twice, including after process restart.
- A renewed lease for the same command updates the result lease without invoking Qianniu again.
- Safety, server shop scope, and sticky handoff are evaluated before AI `send_text` reaches Qianniu.
- Token is masked in status/log/UI responses and config writes are atomic.
- Main layout works at 1440x900, 1024x768, and 390x844 without overlap.
- Dock mode remains usable at 286px wide and only dock row clicks open Qianniu.
- Keyboard focus, live status announcements, reduced motion, and contrast requirements pass.

## Pre-Flight

Identity lock: pass. One accent, one radius scale, one icon approach, one type pairing, zero off-system values.
Anti-slop: pass. Zero gradients, blobs, nested cards, generic KPI rows, buzzwords, or decorative hero content.
States and edge cases: pass. Loading, empty, partial, success, error, offline, refresh, and crash recovery covered.
Accessibility: pass. Contrast targets, visible focus, keyboard path, ARIA live state, and reduced motion specified.
Layout craft: pass. Three layout families and a single focal action per surface.
Cognitive load: pass. Two top connection groups and progressive evidence/config disclosure.

Self-critique: distinctiveness 4, hierarchy 4, consistency 4, accessibility 4, state coverage 4,
copy quality 4, restraint 4, motion motivation 4. Total 32/32. No axis required revision.

## Build Handoff

Target: current static workbench engineering phase. Design system: bespoke CSS variables because the existing
runtime is dependency-free static HTML. Implement this spec exactly; do not redesign or introduce a UI library.
