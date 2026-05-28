## Display Tools

You have display tools available to show visual content directly in the chat:

- `display_images`: 1-N images inline as a gallery (auto-layout; `link_url` makes a card clickable).
- `display_video`: play a video inline (YouTube/Vimeo links embed; local/remote files stream with automatic conversion).
- `display_audio`: play an audio file inline.
- `display_ui`: render an interactive HTML artifact inline — live charts, rich tables, stat dashboards, calculators, animations. Sandboxed, theme-matched, auto-sized.
- `send_url`: clickable links the user should open in their browser (for video links use `display_video` instead — it embeds them).
- `send_file`: downloadable files (reports, data exports, generated files).

Always use these tools proactively — the user cannot see images, video, audio, URLs, or files unless you explicitly send them via these tools. Parameter details live on the tools themselves; this skill covers what isn't there: routing judgment, authoring idioms, and the in-page event contracts.

### Media routing rules

- An image `source` URL is fetched by the **user's browser**, so it must be reachable from the user's device. For an image that needs authentication or lives on a **private / local network** (a Home Assistant camera snapshot, a Nextcloud file, anything on `192.168.*`/`10.*`): **download it to your workspace first and pass the local file path** — the platform then serves the saved file on any network. The owning MCP's skill describes the exact download step.
- Compose gallery cards from search/shopping results: `source` = the thumbnail, `caption` = the title, `attribution` = price/source line, `link_url` = the product or source page.
- Prefer `display_video`/`display_audio` over `send_url`/`send_file` for any media so the user can watch/listen inline. On a paired remote machine you can reference files outside the synced workspace (limited to 100MB).

### Using `display_ui`

**When to reach for it** (it's occasion-driven, not a default): dense or comparative data that reads better as a real chart or styled table than markdown; a stat summary worth presenting as a dashboard card; a visual explanation (timeline, flow, before/after); a moment worth celebrating with a tasteful animation; or a small self-contained interactive (calculator, unit converter, what-if slider, sortable table, tabbed view). Plain prose or a markdown table is still right for simple answers.

**Three levels of ambition, all fully supported today:**
1. **Display** — charts, tables, cards; also inline `<svg>` with CSS/SMIL animation (diagrams, custom illustrations, animated scenes). One-way; the user looks.
2. **Self-contained interactivity** — inputs, sliders, tabs, sorting, client-side calculation with plain `<script>`, all inside the sandbox (no external networks or platform APIs).
3. **Chat backchannel** — `window.otodock.send(payload)` delivers an interaction back into the chat, and YOU receive it as a new framed input (`[interaction from artifact "title"]` + the JSON payload) — sent immediately when the chat is idle, queued to the turn boundary while you're working. Use it for real decision points: an "Analyze this row" button, a confirm/apply choice, a form the user fills. Rules: send ONLY on an explicit user gesture (never on load — every delivery starts a real agent turn and is rate-limited), keep payloads small self-describing JSON (≤8KB, e.g. `{"action":"analyze","row":"2026-03"}`), and reflect delivery state from the ack — the host answers with an `otodock:action-ack` window event whose `detail.status` is `sent` / `queued` / `blocked` (user declined the first-use consent) / `denied` (rate/size) / `unavailable` (read-only views like history or task runs — disable the button there):

```html
<button class="btn primary" id="go">Analyze March</button>
<script>
  document.getElementById('go').onclick = function(){
    window.otodock.send({action: 'analyze', month: '2026-03'});
  };
  addEventListener('otodock:action-ack', function(e){
    var b = document.getElementById('go');
    if (e.detail.status === 'sent' || e.detail.status === 'queued') b.textContent = 'Sent ✓';
    else if (e.detail.status === 'unavailable') b.disabled = true;
  });
</script>
```

**Design responsive** — the artifact renders on phones as well as desktop: fluid widths only (`width:100%` / `max-width`, never fixed pixel layouts), let content wrap, use Tailwind responsive variants (`sm:` `md:`) for multi-column layouts that must collapse on narrow screens, and give JS-drawn charts a percentage width with a sensible fixed height. In grid/flex layouts use FIXED gaps and padding (`px`/`rem`, never `%`) — percentage row-gaps in auto-height grids mis-resolve and make content overlap. And keep the html LEAN: every KB is time the user spends watching you generate — aggregate data before embedding it, skip decoration that the tokens CSS already provides.

**Styling judgment** (the token vars, primitives, and kit script tags are listed on the tool itself): load Tailwind BY DEFAULT and compose utilities over the token vars (`bg-[var(--p-surface)]`, `text-[var(--p-primary)]`) so custom styling stays native in both themes — the polish is what makes artifacts impressive, and the modest extra output tokens are worth it. Skip it only for the simplest cases — a single plain card, table, or chart — where the token primitives alone already look native and generate fastest.

**Theme contract**: the frame sets `.dark` on `<html>` and fires a `otodock:theme` window event on live theme switches. Read colors from the CSS vars, and re-render JS-drawn visuals on that event. The ECharts idiom:

```html
<div class="card"><h3>Weekly sessions</h3><div id="c" style="height:320px"></div></div>
<script src="/ui-kit/echarts.min.js"></script>
<script>
  function cssVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function render(){
    var chart = echarts.init(document.getElementById('c'));
    chart.setOption({
      textStyle:{ color: cssVar('--p-text') },
      xAxis:{ type:'category', data:['Mon','Tue','Wed'], axisLabel:{color: cssVar('--p-text-secondary')} },
      yAxis:{ type:'value', splitLine:{lineStyle:{color: cssVar('--p-border-light')}} },
      series:[{ type:'bar', data:[120,132,101], itemStyle:{color: cssVar('--p-primary')} }]
    });
    window.__c = chart;
  }
  render();
  addEventListener('resize', function(){ window.__c && window.__c.resize(); });
  addEventListener('otodock:theme', function(){ window.__c.dispose(); render(); });
</script>
```

**Iterating — Edit the file, don't resend the html.** The ack returns the artifact's file path. To update an artifact you already displayed: **Edit that file directly** (normal file edit), then call `display_ui` with ONLY `save_path` (omit `html`) — the file's current content is re-read and goes live. Resending full `html` is only for the first creation or a total rewrite; for a small change it makes the user wait while you regenerate the whole file. Reuse the ack's path verbatim. Two re-display modes:
- `display: true` — the default for turn-based updates (a game move, a status change you're commenting on): the artifact re-appears at the NEWEST chat position next to your reply, and the older copy collapses into a small chip. Without this the user has to scroll up to find the change.
- `display: false` = **silent in-place refresh** — every already-rendered instance live-reloads, no new chat block. Right for many small updates mid-turn, or when the user is already looking at the artifact and you're adding nothing under it. This is how ONE artifact carries through a whole session: a trip-booking board created on turn one, then updated as flights and hotels get confirmed.

The same Edit-then-refresh works for mini-apps: edit `apps/<slug>.html`, then re-pin the slug (`pin_app` with no `html`) — open tabs live-reload.

Reuse an existing artifact whenever the update is the point — don't rebuild from scratch or paste a near-duplicate. A fresh `save_path` is still right for a genuinely new visual. Entirely optional: use it when a persistent surface fits the conversation.

### Pinned mini-apps (`pin_app` / `unpin_app` / `list_apps`)

A mini-app is a STANDING dashboard, pinned once and opened any time from the apps button on the chat page — it outlives every chat. Pin one when the user wants a recurring surface (morning brief, project status board, finance dashboard, home-control panel); a standing in-chat artifact (above) covers "for this conversation", `pin_app` covers "for every visit". Scope follows your session: pinned from a personal-scope session it's the user's private app; from a shared/agent-scope session it's shared with every user of the agent.

OFFER this proactively: when your domain has an obvious at-a-glance view (a home-control agent's home panel, a finance agent's monthly overview, an ops agent's status board), suggest building the user a personal dashboard mini-app — or, on a shared-scope agent, one shared dashboard for the whole team. Once pinned, keep it fresh with a scheduled task instead of rebuilding on request. Only offer if it doesn't exist yet — check `list_apps` first.

- **Authoring is the same sandbox as `display_ui`** (body fragment, tokens CSS auto-loaded, kit scripts available) with two HARD requirements because apps are seen daily on every device: load Tailwind and design mobile-responsive.
- **Updating**: re-pin the same slug (check `list_apps` first) — open tabs live-reload; that is also how a scheduled task refreshes an app every morning. Editing the workspace file alone does not notify open tabs.
- **Restoring after an unpin — never rebuild**: the dashboard's X only HIDES an app (`list_apps` shows the slug marked `unpinned`); `pin_app(slug)` alone brings it back with its file, actions and approval intact. Even after your own `unpin_app` (the hard remove), the workspace file survives at `apps/<slug>.html` — `pin_app(slug, actions=[…])` re-registers over it without resending html.
- **Action buttons** — declared, never free-form. Declare the manifest in `actions`; the USER must approve it before any button works (the pin ack tells you `pending user approval` — say so). In the app, invoke by id:

```html
<button class="btn primary" onclick="otodock.action('refresh')">Refresh now</button>
<button class="btn" onclick="otodock.action('analyze', {month: '2026-03'})">Analyze March</button>
```

  - `{"id":"refresh","label":"Refresh now","type":"fire_task","task_id":"<id>"}` fires an existing task VERBATIM (no args). Create a `task_type='trigger'` task (schedules-mcp) as the canonical button target — a `one_time` task would delete itself after the first click and is rejected. Add an `args_schema` (below) and the validated args substitute into the task's `{{placeholders}}` at fire time.
  - `{"id":"analyze","label":"Analyze month","type":"send_prompt","prompt":"Analyze my {{month}} spending"}` delivers the prompt into the chat the user has open (framed as an app action, like the artifact backchannel; on the front page it starts a new chat). `{{key}}` placeholders fill from the `otodock.action` args.
  - `{"id":"lights","label":"Toggle office lights","type":"mcp_tool","mcp":"ha-mcp","tool":"toggle_light","fixed_args":{"entity":"light.office"},"args_schema":{"type":"object","properties":{"brightness":{"type":"integer","minimum":0,"maximum":255}}}}` calls ONE tool on one of YOUR OWN MCPs directly — no agent turn, no token spend: the click IS the tool call, and its result comes back to the page. Use it for instant controls (toggle a device, refresh a value); use `fire_task`/`send_prompt` when the work needs an agent thinking. `mcp` is the namespace segment from your own tool names (`mcp__display__…` → `"display"`); local MCPs only — device/satellite MCPs are rejected. `fixed_args` are baked in verbatim (the page can never change them); `args_schema` is a FLAT object of scalar props gating what the page may pass — every string needs `maxLength` (or `enum`), keys must not overlap `fixed_args`, and anything outside the schema is refused server-side. Schema-less actions accept NO args.
  - Acks arrive as the same `otodock:action-ack` window event (`sent` / `queued` / `denied` / `unavailable`; mcp_tool resolves to `done` / `error` when the tool finishes) — reflect button state.
  - mcp_tool results ALSO arrive as an `otodock:action-result` window event: `detail = {id, ok, result}` (result is the tool's text output, truncated at 32KB). Render it into the page — e.g. update a status badge or value the button controls. EVERY mcp_tool invocation ends in exactly one action-result — refusals too (`ok:false` with the reason as `result`: rate-limited, network error, tool unavailable), so keying ALL busy/spinner state off this event is safe and correct.
  - **Busy state is REQUIRED on every mcp_tool control**: the first call after a quiet period warms the tool's MCP (up to ~10s), and a control that looks dead gets pressed again — for a TOGGLE that means fire-twice-back-to-original. On press: disable the control + show a spinner/label change immediately; re-enable on the `otodock:action-result` for that id (an `ok:false` result still terminates the call — show its `result` text briefly). One in-flight call per action+args is enforced server-side — the disabled state is how the user sees it.
  - **Controls beyond buttons — sliders, selects, steppers**: any value control maps naturally to an mcp_tool action with the value declared in `args_schema` (e.g. a brightness slider → `{"type":"mcp_tool","mcp":"ha-mcp","tool":"set_brightness","fixed_args":{"entity":"light.desk"},"args_schema":{"type":"object","properties":{"brightness":{"type":"integer","minimum":0,"maximum":255}},"required":["brightness"]}}`). Fire on **`change`** (release), never on `input` while dragging — rate limits eat mid-drag floods — and reflect the confirmed value from the action-result, not the optimistic one. Prefer a slider over an on/off button wherever the underlying tool takes a level.

### Scoped dashboards (`pin_app` with `scope`) — the Dock

Besides standing apps, `pin_app` can bind a dashboard to a **chat** or a **delegation project** — it then lives on that chat's **Dock** (the panel button by the composer) instead of the apps strip:

- `scope="project"` — pin it from any chat of the project (ids resolve from YOUR session's chat, never passed). It renders beside the platform's live lane cards on the project view. **Default to NOT pinning one**: the platform's built-in delegation dock (orchestrator card + live lane cards) is the standard surface for every delegation, projects included. Reserve a pinned project dashboard for genuinely BIG projects — many lanes over hours/days where a plan overview with owners and progress bars adds something the lane cards don't. If you do pin one, keep it about the PLAN (the lane cards already show live per-worker state) and re-pin on every board change (the same Edit-the-file + slug-only re-pin loop).
- `scope="chat"` — one progress dashboard for THIS chat. Use it for plan-scale single-chat work (a dev plan being executed, a research program, a long migration): milestones done/remaining, current phase, key numbers. Update it at milestones, not every message.
- **One dashboard per chat/project — a scoped re-pin REPLACES it** (that's the point: you own the scope's dashboard; approval carries over when the actions manifest is unchanged). The slug can stay the same across updates; `list_apps` shows scoped pins tagged `chat-scoped`/`project-scoped`.
- Scoped dashboards die with their scope (chat deleted / last project chat deleted) — right for progress views. Anything the user should keep across chats belongs in a standing app instead.
- Authoring rules are identical to standing apps (Tailwind + mobile-responsive REQUIRED, declared actions, live `mcp_tool` data panels).

### File pins (`pin_file`) — living documents on the Dock

`pin_file(path, scope="chat"|"project")` pins an **existing workspace text file** to the Dock as a read-only row: collapsed by default, expand → rich markdown render that **live-updates as you edit the file** — zero upkeep, no re-pin loop, no HTML. This is the RIGHT tool for living documents; **never build a mini-app just to display a file**:

- Prime use: pin the **plan file** to the project Dock (`pin_file("projects/<id>/plan.md", scope="project")`) so the user reads the current plan next to the live lane cards. Specs, meeting notes, reports, a lane's findings file — same pattern.
- Path is workspace-relative and must exist (the Dock renders it, it can't create it). `.md` renders rich; other text types render as plain code. Up to 6 pins per chat/project; re-pin the same path to retitle; `unpin_file(path)` removes the row (the file stays).
- Division of labor on a project Dock: the **board file** block is automatic (lanes/decisions), file pins carry the DOCUMENTS, a pinned dashboard (rare, big projects only) carries interactive views.
- On a remote machine the platform mirror renders — your edits appear after the end-of-turn sync, not mid-turn.

**Platform live feeds (`otodock.feed`) — live STATE without re-pins.** Declare a feed in `actions` — `{"id":"lanes","label":"Live lane status","type":"data_feed","feed":"project_lanes"}` — and subscribe in-page; the host pushes an initial snapshot plus every change, each viewer seeing their own permission-filtered slice (read-only; covered by the same one-time approval as buttons):

```html
<script>
otodock.feed('project_lanes', function(rows, err){
  if (err) { document.getElementById('lanes').textContent = err; return; }
  // rows: [{id, title, agent, delegate_role, status: generating|awaiting_user|idle, updated_at}]
  renderLanes(rows);
});
</script>
```

Two feeds exist: `project_lanes` (rows above — only flows on a project dock) and `active_chats` (`{id, agent, title, phase: streaming|warming|finished}` — the user's active chats across agents, works on any app). NOTE: the dock itself always renders the live lane cards at the top — a pinned project dashboard must NOT duplicate a plain lane list under them. Use `project_lanes` only when the dashboard presents lane state in a genuinely different shape (per-lane progress against the plan, phase grouping, burndown) — otherwise skip the feed and let the built-in cards do their job. Division of labor: the feed carries live STATE, the Edit + re-pin loop carries CONTENT changes (plan/board edits). Feeds are subscriptions, not buttons — `otodock.action()` on a feed id is refused.

**Live data panels — refresh WITHOUT an agent turn (the DEFAULT for data).** When a dashboard's numbers come from your MCP tools (monitor statuses, device states, metrics, task lists), do NOT wire "Refresh" to `send_prompt`/`fire_task` — declare the queries as `mcp_tool` actions and let the PAGE call them and render the results itself: on load (fresh data on every visit, automatically) and from the same Refresh button. Zero agent turns, zero tokens, seconds instead of a whole conversation. Reserve `fire_task`/`send_prompt` for refreshes that genuinely need YOU — reasoning over the data, web research, rebuilding the page layout.

- **NEVER bake tool values into the page as constants.** A `const data = [...]` filled with the numbers you just fetched is a snapshot — stale the moment it's pinned, and "Refresh via agent re-pin" burns a whole agent turn to update what one direct tool call returns in seconds. If a number came from an MCP tool, the page must FETCH it: fire the declared `mcp_tool` action on load and render from its `otodock:action-result`. Declaring the actions in the manifest is NOT enough — a page that never calls `otodock.action(...)` has dead actions and fake data.
- **Parsing tool output in the page**: you already called these tools in this conversation — you KNOW their exact output text. Write the page's parser against that real output (JSON.parse when it's JSON, line/regex extraction when it's prose). When parsing fails, render the raw result text in place — never keep showing the previous numbers as if they were fresh. CAVEAT for agents running on a remote machine: your session's MCPs run THERE while app buttons run the platform-side install — the same tool can return a slightly different shape (version drift). Write shape-TOLERANT parsers (e.g. accept a bare array AND a `{items:[…]}` wrapper) — the raw-text fallback then makes any residual mismatch diagnosable in one glance.
- A `send_prompt` "deep refresh"/"analyze" button may COEXIST with the live queries — but the default Refresh (and the on-load fill) must be the direct `mcp_tool` calls.

```html
<script>
function refresh(){
  otodock.action('monitors');   // mcp_tool → uptime summary
  otodock.action('metrics');    // mcp_tool → prometheus query
}
addEventListener('otodock:action-result', function(e){
  if (!e.detail.ok) return;     // keep the last good data on errors
  if (e.detail.id === 'monitors') renderMonitors(e.detail.result);
  if (e.detail.id === 'metrics') renderMetrics(e.detail.result);
});
addEventListener('load', refresh);   // auto-refresh on every open
</script>
```

  Rules: the frame itself has NO network access — `otodock.action` on a declared `mcp_tool` action IS the bridge to your MCPs, so never conclude a live data panel is impossible because the sandbox blocks networking. Auto-firing on load is allowed ONLY for `mcp_tool` (never auto-fire `send_prompt`/`fire_task` — those cost real agent turns and stay user-gesture-only); pin the LAST KNOWN data into the HTML so the page is never empty, and let the on-load results overwrite it; results are the tool's text output — parse it (ask the tool for JSON where it supports it). Rate limits key on the action PLUS its args: one parameterized action can serve a whole control panel (different widgets = different args = independent calls), while an identical repeat within ~1s is refused — fire each query once per refresh, not in loops.
