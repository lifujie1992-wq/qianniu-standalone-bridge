# AppBiz cold-start readiness analysis

Captured: 2026-08-19 20:00 +08:00

## Symptom

After AliWorkbench restarted, the bridge remained browser-connected but the local UI fell back to receive-only readiness.

## Evidence

- Bridge build: `2778e1b87679`
- Browser bridge: `qn-standalone-browser-v6-stable-identity`
- AppBiz adapter: attached to the new AliWorkbench parent process
- Candidate scan: 3 objects, all matching the active seller account
- Selected route: none until AppBiz observes `GetNewMsg`, `OnMessageArrive`, or an official send call
- AppBiz.dll SHA-256: `5002311d9b7882354cd64521af0d1ed0ecd11fe62a458aed4564abcb54ddfcd6`
- AppBiz.dll: signed x64 PE, 32,319,488 bytes

Read-only Frida inspection showed that all three candidates share the same seller identity. Cross-process restoration by heap address or candidate ordinal would therefore be unsafe and remains prohibited.

## Finding

The old `ready` property represented both adapter availability and per-conversation route warm-up. A freshly restarted Qianniu is operationally able to receive and acquire the route on the next message, but was reported as receive-only until that first route observation.

A second live restart exposed a separate target-selection defect: CDP identified a render host whose immediate parent was a nested AliWorkbench process with zero AppBiz candidates. The original code stopped at that immediate parent instead of walking to the outer AliWorkbench process that owned all three candidates.

## Fix

- `ready`: adapter attached, validated ABI, and at least one structurally valid AppBiz candidate.
- `route_ready`: a service was selected by an observed trusted AppBiz call.
- UI and heartbeat use `ready` for operational readiness.
- Native send submission continues to require `route_ready`; no candidate guessing was introduced.
- CDP target resolution walks the complete matching AliWorkbench ancestry and attaches to the outer process.
