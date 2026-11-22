# VALERIA VFlow — Language Guide

**Status:** normative practical guide for the implemented DSL.
**Date:** 21 November 2026.
**Audience:** pipeline authoring administrators, VFlow developers, QA and operations.
**Scope:** the VFlow textual language, its compilation to Graph IR, data contracts and publication rules.

> This guide documents the **actual implementation** under `src/opencode_web/vflow/`, not the conceptual syntax from early mockups. If this guide and the code disagree, the code is the source of truth: `parser.py`, `compiler.py`, `logic.py`, `handlers.py`, `runtime.py`, `policy.py` and `config.py`. `VALERIA_VFlow_mockup_v2.html` is a UI reference and does not define the grammar.

**Related documents**

- [VFlow Technical Specification](VALERIA_VFlow_Technical_Specification_RU.md) — graph model, execution points, Fork/Merge, security. (Russian)
- [ADR-001…004](VALERIA_VFlow_ADR_RU.md) — accepted architecture decisions. (Russian)
- [VFlow Operations Documentation](VALERIA_VFlow_Operations_RU.md) — API, database, Task Gateway, operations. (Russian)
- [VFlow Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md) — code-only way for developers to add system blocks. (Russian)
- [VFlow QA Test Plan](VALERIA_VFlow_QA_Test_Plan_RU.md) — QA scenarios and acceptance checks. (Russian)
- [Block Compatibility](VALERIA_VFlow_Block_Compatibility_RU.md) — handler versions and migrations. (Russian)
- [Russian version of this guide](VALERIA_VFlow_Language_Guide_RU.md).

---

## Contents

1. [Purpose and concepts](#1-purpose-and-concepts)
2. [Lexical syntax](#2-lexical-syntax)
3. [Workflow header](#3-workflow-header)
4. [Steps and calls](#4-steps-and-calls)
5. [Edge chains and ports](#5-edge-chains-and-ports)
6. [Built-in block reference](#6-built-in-block-reference)
7. [Data contracts and `input_path` semantics](#7-data-contracts-and-input_path-semantics)
8. [Complete examples](#8-complete-examples)
9. [Validation rules](#9-validation-rules)
10. [Limits and configuration](#10-limits-and-configuration)
11. [Errors and observability](#11-errors-and-observability)
12. [Authoring workflow in the UI and API](#12-authoring-workflow-in-the-ui-and-api)
13. [Security and best practices](#13-security-and-best-practices)
14. [Quick reference](#14-quick-reference)

---

## 1. Purpose and concepts

### 1.1. What VFlow is

VFlow is a persistent graph-pipeline module built into VALERIA. A pipeline describes how JSON data flows through system operators and existing VALERIA tasks. VFlow does not start OpenCode directly: a `Task` node enqueues an ordinary VALERIA task in the shared queue and waits for it to finish.

The textual VFlow language is a **declarative representation of a graph**, not a procedural program. It has no variables, `while`/`for` loops, user functions, classes, imports, computed expressions or arbitrary code. Repeating a path is possible only as a graph cycle bounded by runtime budgets.

### 1.2. workflow, step, edge, port

| Concept | Meaning |
|---|---|
| **workflow** | A named graph definition. One VFlow source contains one definition. |
| **step** | A node declaration with a unique ID. A node is a call to a built-in block (`task.run`, `transform`, `if`, `fork`, `merge`). |
| **edge** | A directed transition from an output port of one node to the input of another node or to the `end` terminal. |
| **port** | A named node output: `out` for ordinary nodes, `true`/`false` for `if`, `branch[i]` or `all` for `fork`. |
| **start / end** | Service terminals, not Registry nodes. `start` has exactly one outgoing edge; `end` is a terminal with no outgoing edges. |

The order in which `step` declarations appear does not define execution order. Execution is determined only by edges, ports and execution-point state.

### 1.3. Graph versus run

- **Pipeline Definition** — the logical entity: slug, title, description, audience, settings.
- **Pipeline Version** — an immutable publication of Graph IR, DSL source, input schema, public output schema and required handler versions.
- **Pipeline Run** — one execution of a concrete published version by a concrete user with an input JSON value.
- **Execution Point** — a persistent unit of execution: current `node_id`, JSON data, status, `visit_seq`, `attempt`, Fork/Merge context and lease.

A published version is immutable. A new Run receives a pinned graph version and a Task-configuration snapshot. Editing or republishing does not change already started Runs.

### 1.4. Multiple execution points

A Run has **no single global `current_node`**. At one moment a Run may have several independent points: one waiting for a Task, another waiting at a Merge, a third ready to advance. This is the key difference between VFlow and a sequential executor.

```text
Pipeline Run #9001 — RUNNING — Version #12
    Point #51: node=review_task   state=WAITING_TASK   task_run=job-710
    Point #52: node=security      state=READY          data={...}
    Point #53: node=combined      state=WAITING_MERGE  fork=fork-411
```

`Fork` creates N child points; `Merge` consumes them and creates one continuation point. `If` does not create a new point; it moves the existing point through exactly one selected port. `Task` does not block other points and does not occupy an OpenCode slot while waiting.

### 1.5. What the language does not do

- It does not execute `eval`, `exec`, Python, JavaScript, SQL, shell or import-by-name.
- It does not let a user or administrator upload a custom handler, plugin or script.
- It does not expose prompts, reasoning, tool messages or other users' Runs.
- It does not statically guarantee that an arbitrary cycle terminates: runtime limits transitions and Run wall time.
- It is not a replacement for the VALERIA task queue: `Task` always goes through the existing Task Gateway.

---

## 2. Lexical syntax

### 2.1. Encoding, whitespace and comments

- Source is **UTF-8**.
- Spaces, tabs and newlines are separators. Indentation is insignificant.
- Comments start with `//` or `#` and continue to the end of the line.
- Comments and whitespace inside JSON strings are not comments.
- Blank lines are allowed.

```vflow
// Slash comment
# Hash comment
workflow "comments" {
    version: 1;   // semicolon is optional
    input: json
    step mark = transform(operations: [
        {op: "set", path: "$.ok", value: true}  # comment after a value
    ])
    start -> mark -> end
}
```

### 2.2. Identifiers

Step IDs, argument names and call names use this ASCII pattern in the current compiler:

```text
[A-Za-z_][A-Za-z0-9_]*
```

UTF-8 is fully supported in string literals and comments. For JSON keys containing non-ASCII characters, use bracket JSONPath notation: `$['ключ']`. Identifiers containing Unicode letters are not accepted by the current lexer; this is a grammar limitation, not an encoding issue.

Reserved and service words: `workflow`, `step`, `version`, `input`, `start`, `end`, `true`, `false`, `null`. A step ID must not be `start` or `end`.

### 2.3. Literals

| Type | Examples | Notes |
|---|---|---|
| String | `"text"`, `"путь"`, `"a\nb"` | JSON string with JSON escaping. |
| Integer | `0`, `1`, `42`, `-3` | JSON format; a branch index must be non-negative. |
| Number | `1.5`, `2e3`, `-0.25` | JSON format, including exponent notation. |
| Boolean | `true`, `false` | Literals. |
| Null | `null` | Literal. |
| Array | `[1, "a", true]` | Comma-separated elements. |
| Object | `{op: "set", path: "$.x"}` | Key is an identifier or a string. |

`NaN`, `Infinity`, dates, binary data and Python objects are not literals. All values must be valid JSON.

### 2.4. Objects and arrays

Object: `{ key: value, "quoted key": value }`. A key may be an identifier or a string. Array: `[ value, value ]`. Nesting is arbitrary within the JSON depth limit. Trailing commas are not supported: do not put a comma after the last element.

### 2.5. Optional semicolons

A `;` may follow `version`, `input`, a `step` declaration or an edge chain. It may also appear as a standalone separator between statements. Semicolons are optional; the examples in this guide use them for readability. Newlines do not replace commas inside arguments, objects or arrays.

### 2.6. Bare identifiers as string references

Where a string reference is expected, an unquoted identifier is allowed:

```vflow
step split = fork(count: 2, join: combined)
```

Here `combined` compiles to the string `"combined"`. For arbitrary values (task IDs, JSONPaths, modes), use quotes. Do not treat a bare identifier as a variable: the language has no variables.

---

## 3. Workflow header

Minimal structure:

```vflow
workflow "name" {
    version: 1
    input: json

    step mark = transform(operations: [{op: "set", path: "$.ok", value: true}])
    start -> mark -> end
}
```

| Field | Required | Meaning |
|---|---:|---|
| `workflow "name"` | Yes | Definition name as a JSON string. Use a stable, descriptive name. |
| `version` | Yes | Positive integer (`>= 1`). This is the **language/definition** version, not the database publication number. |
| `input` | Yes | The current contract is `json`. The parser accepts an identifier or a string, but `json` is normative. |

`version` and `input` may appear before or after steps in any order; repeating them is not normal style. Step declarations and edge chains follow the header. A workflow with no executable steps is rejected (`COMPILE_EMPTY_WORKFLOW`).

---

## 4. Steps and calls

### 4.1. Declaration form

```text
step <id> = <call_name>(<arg>: <value>, ...)
```

- `<id>` is a unique step identifier within the workflow.
- `<call_name>` is a Registry call name. Built-ins: `task.run`, `transform`, `if`, `fork`, `merge`.
- Arguments are comma-separated; each argument has the form `name: value`.
- Argument order is irrelevant.
- A repeated argument is a parser error (`PARSE_DUPLICATE_ARGUMENT`).
- An unknown argument is a compiler error (`COMPILE_UNKNOWN_ARGUMENT`).
- Values are JSON-like literals, objects, arrays or bare identifiers used as string references.

```vflow
workflow "shape" {
    version: 1;
    input: json;

    step init = transform(operations: [
        {op: "set", path: "$.ready", value: false}
    ]);
    step decide = if(path: "$.ready", operator: "eq", value: true);

    start -> init -> decide;
    decide.true -> end;
    decide.false -> end;
}
```

### 4.2. Available calls and handler versions

| DSL call | IR type | Version | Purpose |
|---|---|---|---|
| `task.run(...)` | `valeria.task` | `1.0` | Enqueue an existing VALERIA task and wait for its result. |
| `transform(...)` | `core.transform` | `1.0` | Deterministically transform JSON. |
| `if(...)` | `core.if` | `1.0` | Select one of two ports without changing JSON. |
| `fork(...)` | `core.fork` | `1.0` | Atomically create N independent branches. |
| `merge(...)` | `core.merge` | `1.0` | Wait for all branches and create one continuation point. |

The Registry is code-only. A new system type can appear only with a VALERIA code release and a registered `NodeSpec`/handler. An administrator cannot add block types through the UI. See the [Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md).

### 4.3. Declaration order versus execution order

The compiler preserves node declaration order in canonical IR, but the runtime selects a point by state, not by array order. Therefore:

```vflow
step a = transform(...)
step b = transform(...)
start -> b -> a -> end
```

executes `b`, then `a`, regardless of declaration order.

---

## 5. Edge chains and ports

### 5.1. The `->` operator

`a -> b -> c` is equivalent to two edges: `a -> b` and `b -> c`. A chain must contain at least two endpoints. The same edge cannot be declared twice (`COMPILE_DUPLICATE_EDGE`).

```vflow
start -> load -> normalize -> decide;
decide.true -> publish -> end;
decide.false -> reject -> end;
```

### 5.2. start and end

- `start` is the only entry marker; it has exactly one outgoing edge. A second edge from `start` is rejected (`COMPILE_MULTIPLE_START_EDGES`).
- `end` is a terminal. It may have multiple incoming edges but no outgoing edge (`COMPILE_EDGE_FROM_END`).
- An edge into `start` is forbidden (`COMPILE_EDGE_TO_START`).
- `start` and `end` have no ports: `start.out` and `end.true` are invalid.
- Every reachable terminal route must reach `end`; an `end` with no incoming edges is also an error.

### 5.3. Source ports

A port is written **only on the source side** of an edge:

```text
<source>[.<port> | [<index>]] -> <target>
```

| Node | Output ports | Syntax |
|---|---|---|
| `task.run` | `out` | `task_step -> next` |
| `transform` | `out` | `transform_step -> next` |
| `merge` | `out` | `merge_step -> next` |
| `if` | `true`, `false` | `decision.true -> ...`, `decision.false -> ...` |
| `fork` | `branch[0]` … `branch[N-1]` or `all` | `split[0] -> ...`, `split -> next` |

An edge target cannot have a port: `a -> b.true` is forbidden (`COMPILE_TARGET_HAS_PORT`). If a port is needed, it belongs to the source.

### 5.4. The `all` shorthand

```vflow
step split = fork(count: 3, join: combined)
start -> split
split -> worker -> combined
```

`split -> worker` is the `all` shorthand: all N branches start at the same `worker` node. This means **N separate visits** and, if `worker` is a `Task`, **N separate Task Runs**. Do not mix `all` with explicit `branch[i]` edges on the same Fork.

### 5.5. Fan-out is forbidden

`Task`, `Transform` and `Merge` have exactly one outgoing `out` edge. Unconditional branching is rejected (`COMPILE_FAN_OUT`). Use `Fork` for parallelism. For `If`, each of `true` and `false` must have exactly one edge.

---

## 6. Built-in block reference

### 6.1. Task — run an existing VALERIA task

```vflow
step review = task.run(
    task_id: "code.review",
    input_path: "$.request",
    result_mode: "strict_json",
    json_schema: {
        type: "object",
        required: ["ok"],
        properties: {ok: {type: "boolean"}},
        additionalProperties: false
    },
    max_json_repairs: 1,
    queue_timeout_seconds: 300,
    execution_timeout_seconds: 1800,
    retries: 1
)
```

| Argument | Required | Meaning |
|---|---:|---|
| `task_id` | Yes | ID of an existing VALERIA task. Checked at validate/publish and again when a Run is created. |
| `input_path` | No | Limited JSONPath to a fragment of the input JSON. Defaults to `$`. |
| `input` | No | Historical alias for `input_path`. Prefer `input_path`; if both are given, `input_path` wins. |
| `result_mode` | No | **Legacy** mode: `auto` (default), `strict_json`, `none`. Ignored by `output_mode=json`. |
| `json_schema` | No | **Legacy** optional JSON Schema subset for `strict_json`. Supported: `type`, `required`, `properties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `minItems`, `maxItems`, `additionalProperties: false`. |
| `max_json_repairs` | No | **Legacy** integer `0..3`, default `1`: how many correction turns may be sent into the same Task chat when `strict_json` output is invalid. Cannot be combined with `on_json_error: "repair"`. |
| `output_mode` | No | `text` (default) or `json`. `json` enables the JSON Processor output contract and validation. |
| `output_schema` | No | Optional JSON Schema for `output_mode=json`. Preferred over legacy `json_schema`; only the safe processor subset is accepted. |
| `on_json_error` | No | `fail` (default) or `repair`. `repair` sends the raw completed Task text to the JSON Processor instead of failing immediately. Requires `output_mode: "json"`. |
| `repair_trigger` | No | `invalid_json` (default) or `invalid_json_or_schema`. Valid only together with `on_json_error: "repair"`. |
| `queue_timeout_seconds` | No | Non-negative integer: how long to wait for the task to start. Omit for no separate limit. |
| `execution_timeout_seconds` | No | Non-negative integer: how long the task may run after starting. |
| `retries` | No | Non-negative integer: number of **additional** attempts. Default `0`. The UI schema recommends at most 10. |

Unknown arguments are rejected by the compiler. `task_id` must be a non-empty string. Malformed `json_schema`/`output_schema` and `max_json_repairs` outside `0..3` are rejected with `COMPILE_INVALID_JSON_SCHEMA` / `COMPILE_INVALID_MAX_JSON_REPAIRS`. `repair_trigger` without `on_json_error: "repair"` is rejected, and `max_json_repairs > 0` with `on_json_error: "repair"` is rejected with `COMPILE_REPAIR_ENGINE_CONFLICT` so two repair engines never run for one answer.

**What happens:**

1. Runtime selects the value at `input_path` from the point's data.
2. If the path is missing, the deterministic error `TASK_INPUT_MISSING` is raised and no task is enqueued.
3. The Gateway enqueues **one** ordinary Task Run in the existing queue with a deterministic `enqueue_key` and a Task snapshot.
4. The point becomes `waiting_task`; waiting holds neither the HTTP request nor an OpenCode slot.
5. After confirmed completion, runtime reads only the final answer text and builds an envelope.
6. `strict_json` requires the entire final text to be valid JSON. If `json_schema` is configured, the parsed value must also satisfy it. A syntax failure uses `TASK_OUTPUT_NOT_JSON`; a schema mismatch uses `TASK_SCHEMA_INVALID` with a safe path such as `$.items[0].id`.
7. When the output is invalid and `max_json_repairs` allows another attempt, VFlow sends a bounded correction into the **same Task chat/session**. The correction contains the safe validation error, the schema/example and the instruction to return only valid JSON. The Task point waits for that follow-up answer; its `repair_attempt` count is separate from Task `attempt`/`retries`.
8. Repair applies only to JSON syntax/schema failures of a completed Task. Timeouts, cancellation, `TASK_CHANGED`, `TASK_UNAVAILABLE` and execution failures are never repaired. After the repair budget is exhausted the Run fails with `TASK_OUTPUT_NOT_JSON` or `TASK_SCHEMA_INVALID`.
9. A repair follow-up is a normal chat continuation (`task_id = NULL`) in the same chat, not a new Task or a legacy pipeline child. It is idempotent by a deterministic message id and preserves the VFlow run/point correlation.

**JSON Processor mode (`output_mode: "json"`):**

1. A strictly valid JSON answer that satisfies `output_schema` (when configured) advances immediately; no processor job is created.
2. A syntactically invalid answer, or a schema mismatch when `repair_trigger: "invalid_json_or_schema"`, creates one JSON Processor repair job correlated by `vflow_run_id`/`point_id`/`task_run_id`.
3. The point waits in `status = "processing_output"`; `next_check_at` and the processor job id are persisted. The VFlow scheduler never calls an LLM inside its transaction; the separate JSON Processor worker does the repair.
4. On processor success, the repaired decoded value becomes `output.json`. On failure the Run fails with the safe `OUTPUT_INVALID` code and no downstream node runs.
5. A disabled or unconfigured processor is an explicit `JSON_PROCESSOR_NOT_CONFIGURED` error, never a silent bypass.
6. `on_json_error: "fail"` and legacy `result_mode` behaviour remain available for existing pipelines. The legacy same-chat repair (`strict_json` + `max_json_repairs`) is never combined with the processor engine.

**Successful Task envelope (`result_mode=auto` or `strict_json`):**

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-701",
    "status": "completed",
    "output": {
      "text": "{\"ok\":true,\"issues\":[]}",
      "json": {"ok": true, "issues": []}
    }
  }
}
```

- `input` is the **full** point JSON before the Task, not just the selected `input_path`.
- `task.output.text` is the final answer text (empty string if there is no text).
- `task.output.json` is the parsed JSON if the entire text parsed; with `auto` and a parse failure it is `null`; with `none` it is `null`.
- `task.run_id` is the existing VALERIA Task Run ID in the queue.
- Prompts, reasoning and tool messages are never included in the envelope.

**Successful JSON Processor Task envelope (`output_mode: "json"`):**

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "output": {
    "text": "{\"ok\":true,\"issues\":[]}",
    "json": {"ok": true, "issues": []}
  },
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-701",
    "status": "completed",
    "output": {
      "text": "{\"ok\":true,\"issues\":[]}",
      "json": {"ok": true, "issues": []}
    }
  }
}
```

- `output` is the new preferred path (`$.output.json...`); `task.output` is retained for backward compatibility (`$.task.output.json...`).
- `output.json` always contains a decoded, schema-valid JSON value. A failed repair never writes `null`, `{}` or an error envelope into it.
- `input` is the full point JSON before the Task.

The Task snapshot is captured when the Run is created. If the task changes after the Run starts, enqueue fails with `TASK_CHANGED`; VFlow does not silently substitute the newer task version. A new Run sees the new task version.

### 6.2. Transform — safe JSON transformation

```vflow
step normalize = transform(
    operations: [
        {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"},
        {op: "set", path: "$.context.ready", value: true},
        {op: "remove", path: "$.task.output.text"}
    ],
    create_missing: false
)
```

| Argument | Required | Meaning |
|---|---:|---|
| `operations` | Yes | Non-empty array of operation objects. |
| `create_missing` | No | Boolean, default `false`. Allows creation of missing containers. |

**v1 operations:**

| Operation | Arguments | Action |
|---|---|---|
| `set` | `path`, `value` | Set a value at a path. Replacing the root `$` is allowed. |
| `copy` | `from`, `to` | Copy an existing value. |
| `move` | `from`, `to` | Copy and delete the source. |
| `remove` | `path` | Remove a value; removing the root is forbidden. |
| `select` | `path` | Replace the current root with the selected fragment. |
| `merge_object` | `path`, `object` | Shallow-merge an object into the target object. |
| `append` | `path`, `value` | Append a value to an array. |
| `rename` | `from`, `to` | Alias of `move` for renaming a key. |

In `set` and `append`, `value` may be a reference operand:

```json
{"op": "set", "path": "$.copy", "value": {"from_path": "$.source"}}
```

`from_path` reads from the **current intermediate** JSON and copies by value. It is not eval and not an expression: the path must exist. For `copy`/`move`/`rename`, the source is supplied through the separate `from` argument.

**Rules:**

- Operations run strictly in order on the intermediate JSON; each operation receives the previous result.
- The input value is not mutated: Transform creates a new copy.
- Only limited JSONPath is supported: `$`, `.field`, `['field']`, `[index]`. Wildcards, filters, functions and computed expressions are forbidden.
- `create_missing=false`: a missing key, index or container is an error. `true` allows creating a missing object key, appending at the end of an array and creating intermediate containers; type checks still apply.
- A missing path, incompatible type, attempt to remove the root or JSON limit violation produces a deterministic node error. The Run fails under fail-fast.

### 6.3. If — select one port without changing data

```vflow
step decision = if(
    path: "$.quality.score",
    operator: "ge",
    value: 80,
    missing: "error"
)
```

| Argument | Required | Meaning |
|---|---:|---|
| `path` | Yes | Non-empty limited JSONPath. |
| `operator` | Yes | One of `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, `in`, `exists`, `is_null`. |
| `value` | Depends | Required for every operator except `exists` and `is_null`. |
| `missing` | No | `error` (default), `false`, `true` — behavior when the path is missing. |

**Operator semantics:**

| Operator | Condition |
|---|---|
| `eq` / `ne` | Typed equality/inequality. `"12"` is not equal to `12`. |
| `gt` / `ge` / `lt` / `le` | Only two numbers or two strings. Different types yield `false`; unsupported types raise an error. |
| `contains` | For a string — substring; for an array — typed membership of an item. |
| `in` | Typed membership of the value in the `value` array. |
| `exists` | `true` if the path exists (even if the value is `null`). A missing path is always `false`; `missing` is not applied. |
| `is_null` | `true` only if the path exists and its value is `null`; a missing path is `false`. |

For the remaining operators a missing path is handled by `missing`: `error` fails the Run; `false`/`true` selects the corresponding branch.

**Behavior:**

1. If reads a field from **the JSON it received**.
2. It computes a boolean.
3. It selects exactly one port, `true` or `false`.
4. It moves **the same point** through the selected edge.
5. JSON is unchanged and is not wrapped in a new structure; no second point is created.

At publication, If must have exactly one edge on `true` and exactly one on `false`. If both branches later converge on one ordinary node, this is a valid control-flow join without Merge, because only one branch is active. If If is inside a Fork, **both** outcomes must have a route to the paired Merge.

### 6.4. Fork — N independent branches

```vflow
step split = fork(count: 3, join: combined)
```

| Argument | Required | Meaning |
|---|---:|---|
| `count` | Yes | Integer from 2 to 32 (hard compiler bound). Runtime additionally applies `VFLOW_MAX_FORK_N` and the per-pipeline policy. |
| `join` | Yes | ID of the paired `merge` node in the same workflow. |
| `mode` | No | Not supported on Fork; do not confuse it with `merge(mode: "all")`. |

**Ports:** exactly `branch[0]` … `branch[count-1]` **or** exactly one `all`. Mixing modes is forbidden; the `out` port is forbidden on Fork.

**Atomic semantics:**

1. Runtime creates a `fork_instance` in one transaction with `expected_count=N`, a snapshot of the input JSON and the parent `fork_stack`.
2. Sequentially, for `i=0..N-1`, it creates N child points with `branch_index=i` and an independent copy of the input JSON.
3. The source point is marked `forked` and is no longer scheduled.
4. Only after commit do child points become available to the scheduler and run concurrently.

Creating points sequentially does not mean executing them sequentially. Fork does not guarantee physical simultaneity: the shared VALERIA queue and OpenCode limits may start branches at different times.

**`all` shorthand:**

```vflow
split -> worker -> combined
```

All N points start at `worker`; if it is a Task, N separate Task Runs are created. Branch input values are equal as JSON but are independent copies.

**Metadata:** `branch_index`, `fork_instance_id`, `merge_node_id` and `expected_count` are stored in the point's `fork_stack`. They are not automatically added to user JSON. To pass a branch index to a task, use distinct `branch[i]` ports and distinct Task nodes; or a future trusted Transform operation.

### 6.5. Merge — strict wait-all barrier

```vflow
step combined = merge()
```

| Argument | Required | Meaning |
|---|---:|---|
| `mode` | No | Only `all` (the default). Any other value is rejected. |

**Semantics:**

- Merge must be paired with exactly one Fork; one Merge cannot be reused for two Forks.
- Each branch of a concrete Fork Instance records an arrival with a unique `(fork_instance_id, branch_index)`.
- Merge waits for **all** indexes `0..N-1`, not for N arbitrary events.
- After the last arrival, one CAS transition `open -> merged` creates **exactly one** continuation point. The N child points become `consumed`.
- The continuation inherits the `fork_stack` without the closed frame.
- Result order follows `branch_index`, not completion time.
- A duplicate arrival for the same branch is consumed; a stale arrival for a closed Fork Instance cannot create a second point.

**Merge result:**

```json
{
  "source": {"repository": "example/repo", "pr": 24},
  "branches": [
    {"index": 0, "data": {"task": {"output": {"json": {"ok": true}}}}},
    {"index": 1, "data": {"task": {"output": {"json": {"tests": 112}}}}},
    {"index": 2, "data": {"task": {"output": {"json": {"warnings": 0}}}}}
  ]
}
```

- `source` is the data immediately before the corresponding Fork (captured in the Fork Instance).
- `branches[*].data` is the full JSON actually produced by the branch.
- There is no automatic "smart" field merge; use `Transform` after Merge.
- If a required branch fails, the Run fails under fail-fast. Merge **does not** release a partial result and has no `first success` mode.

**Nesting:** an inner Merge closes only its own Fork and must run before the outer Merge. Entering an outer Merge while an inner Fork is still open is impossible: both the validator and runtime inspect the top `fork_stack` frame.

---

## 7. Data contracts and `input_path` semantics

### 7.1. Point JSON value

A point's data is a valid JSON value: object, array, string, number, boolean or `null`. Limits:

- size — `VFLOW_MAX_JSON_BYTES` (default 1 MiB);
- depth — `VFLOW_MAX_JSON_DEPTH` (default 64);
- non-serializable types, unconverted dates, binary objects and `NaN`/`Infinity` are forbidden.

Checks run on Run input, after Transform, after Task, after Merge and at completion. A violation produces `LIMIT_JSON_BYTES`, `LIMIT_JSON_DEPTH` or `LIMIT_JSON_INVALID`.

### 7.2. Run input

The first node (`entry`) receives the Run `input` without a wrapper. If the user submitted:

```json
{"repository": "example/repo", "pr": 24, "request": {"severity": "high"}}
```

then `start -> prepare` passes this exact object to `prepare`.

### 7.3. Task and `input_path`

`input_path` selects the **fragment** serialized into the task text for the agent. The Task envelope still preserves the **full** point input in `input`.

| `input_path` | What the task receives | What remains in `task.input` |
|---|---|---|
| `$` (default) | the whole current JSON | the whole current JSON |
| `$.repository` | the string `"example/repo"` | the whole object |
| `$.request` | `{"severity": "high"}` | the whole object |
| `$.missing` | error `TASK_INPUT_MISSING`, no task is enqueued | — |

`input_path` is not a template or expression. Only `$`, `.field`, `['field']`, `[index]` are allowed; wildcards and filters are forbidden. The path is read from the data that reached the Task. If a previous Transform replaced the root, the path applies to the new root.

Example:

```vflow
step load = task.run(task_id: "repo.load", input_path: "$");
step review = task.run(task_id: "code.review", input_path: "$.request");
step combine = transform(operations: [
    {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"}
]);
```

### 7.4. Transform and If

- `Transform` replaces the data with the result of its last operation. If the previous context is needed, copy it into the result before `select`/`remove`.
- `If` does not change data. It passes the same JSON through the selected port.
- After `Task`, data is the Task envelope; after `Merge`, it is the Merge envelope. Account for this in paths.

### 7.5. Fork and Merge

`Fork` gives every branch an independent logical copy of the input JSON. Changes in one branch are invisible to the others. `Merge` collects the full data of each branch plus the pre-Fork `source` snapshot; it does not merge fields automatically.

### 7.6. Practical path rules

1. After a Task, use `$.task.output.json...` for the structured answer.
2. After Merge, use `$.branches[i].data...` and `$.source...`.
3. Do not rely on implicit type coercion: `"12"` and `12` are different values.
4. Verify paths with `POST /api/admin/vflow/preview-if` and `preview-transform` before publication.
5. Do not pass the entire accumulated envelope to a Task if the agent needs only a fragment: set `input_path`.

---

## 8. Complete examples

> Task IDs in the examples are fictional (`code.review`, `quality.score`, etc.). Before publication, create the corresponding tasks in the normal VALERIA task section. The examples contain no secrets and are ready for compiler verification.

### 8.1. Sequential pipeline with If

```vflow
workflow "sequential_if" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.review", value: {score: 0, status: "new"}}
    ]);

    step score_task = task.run(
        task_id: "quality.score",
        input_path: "$.review",
        result_mode: "strict_json"
    );

    step extract = transform(operations: [
        {op: "copy", from: "$.task.output.json.score", to: "$.review.score"},
        {op: "set", path: "$.review.status", value: "scored"}
    ]);

    step decide = if(
        path: "$.review.score",
        operator: "ge",
        value: 80,
        missing: "error"
    );

    step accept = task.run(task_id: "quality.accept");
    step reject = task.run(task_id: "quality.reject");

    start -> prepare -> score_task -> extract -> decide;
    decide.true -> accept -> end;
    decide.false -> reject -> end;
}
```

Walkthrough: `prepare` creates `review`; the Task receives only `review`; `extract` copies the score out of the Task envelope; If selects exactly one branch; both branches terminate at `end`.

### 8.2. Fork of three tasks and Merge

```vflow
workflow "review_three_ways" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.review.started", value: true}
    ]);

    step split = fork(count: 3, join: combined);

    step code = task.run(task_id: "review.code", result_mode: "strict_json");
    step tests = task.run(task_id: "review.tests", result_mode: "strict_json");
    step security = task.run(task_id: "review.security", result_mode: "strict_json");
    step combined = merge();

    step summarize = transform(operations: [
        {op: "set", path: "$.summary.branch_count", value: 3}
    ]);

    start -> prepare -> split;
    split[0] -> code -> combined;
    split[1] -> tests -> combined;
    split[2] -> security -> combined;
    combined -> summarize -> end;
}
```

All three branches receive independent copies of the data after `prepare`. `combined` waits for indexes 0, 1 and 2; the `branches` result is ordered by index, not by completion speed. If one task fails, the Run becomes `failed` and Merge does not release a partial result.

### 8.3. Nested Fork/Merge

```vflow
workflow "nested_fork" {
    version: 1;
    input: json;

    step outer = fork(count: 2, join: outer_join);
    step inner = fork(count: 2, join: inner_join);

    step work_a = task.run(task_id: "work.a");
    step work_b = task.run(task_id: "work.b");
    step work_c = task.run(task_id: "work.c");

    step inner_join = merge();
    step outer_join = merge();

    step finish = transform(operations: [
        {op: "set", path: "$.nested", value: true}
    ]);

    start -> outer;
    outer[0] -> inner;
    outer[1] -> work_c -> outer_join;
    inner[0] -> work_a -> inner_join;
    inner[1] -> work_b -> inner_join;
    inner_join -> outer_join;
    outer_join -> finish -> end;
}
```

The inner `inner_join` closes Fork `inner` and returns the point to the outer scope; then `outer_join` closes `outer`. Reaching `outer_join` with `inner` still open would be rejected.

### 8.4. Cycle with an exit

```vflow
workflow "retry_until_ready" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.ready", value: false}
    ]);

    step work = task.run(task_id: "job.prepare", result_mode: "strict_json");

    step read = transform(operations: [
        {op: "copy", from: "$.task.output.json.ready", to: "$.ready"}
    ]);

    step check = if(
        path: "$.ready",
        operator: "eq",
        value: true,
        missing: "false"
    );

    step finish = transform(operations: [
        {op: "set", path: "$.status", value: "done"}
    ]);

    start -> prepare -> work -> read -> check;
    check.true -> finish -> end;
    check.false -> work;
}
```

The cycle `work -> read -> check -> work` is allowed outside open Fork/Merge scopes. Every re-entry into the Task creates a **new** Task Run with a new `visit_seq`/`attempt`; runtime bounds the number of transitions with `VFLOW_MAX_TRANSITIONS` and wall time with `VFLOW_MAX_RUN_SECONDS`. Publication does not guarantee that the business condition ever becomes true, so a cycle must have a reachable exit and a meaningful limit.

### 8.5. Task with strict JSON and timeouts

```vflow
workflow "strict_report" {
    version: 1;
    input: json;

    step run = task.run(
        task_id: "report.generate",
        input_path: "$.request",
        result_mode: "strict_json",
        queue_timeout_seconds: 300,
        execution_timeout_seconds: 1800,
        retries: 1
    );

    step shape = transform(operations: [
        {op: "select", path: "$.task.output.json"},
        {op: "set", path: "$.published", value: true}
    ]);

    start -> run -> shape -> end;
}
```

`queue_timeout_seconds` applies only before the task starts; `execution_timeout_seconds` applies after start. `retries: 1` gives one additional attempt on a retryable error or queue timeout, but not on an execution timeout. `strict_json` turns an invalid answer into `TASK_OUTPUT_NOT_JSON`. `select` replaces the root with the clean JSON answer, removing the Task envelope.

---

## 9. Validation rules

Checks happen at several levels: parser (syntax), compiler (structure and types), validate/publish service (task existence) and runtime (state, limits, idempotency).

### 9.1. Syntax

- An unterminated string/workflow block, unexpected character, invalid JSON literal, missing `version` or `input`, or duplicate argument produces an error with `line`/`column`.
- A `step` declaration without `=` or a call is invalid.
- A chain must contain at least two endpoints.
- A branch index must be a non-negative integer.

### 9.2. Graph structure

1. **One `start` and one reachable `end`.** Exactly one edge from `start`; at least one edge to `end`; `end` reachable from `start`.
2. **Unique IDs.** A repeated `step` is rejected (`COMPILE_DUPLICATE_STEP`). `start`/`end` are reserved.
3. **Known handler type and version.** An unknown call name or version is an error (`COMPILE_UNKNOWN_NODE_TYPE`, `COMPILE_UNKNOWN_HANDLER_VERSION`).
4. **Every node is reachable from `start`.** Orphan nodes are an error (`COMPILE_UNREACHABLE_NODES`), not a warning.
5. **No duplicate edges.** `(source, source_port, target)` is unique.
6. **No port on an edge target.** A port is valid only on the source.

### 9.3. Ports and fan-out

1. `Task`, `Transform`, `Merge` have exactly one outgoing `out` edge; fan-out is rejected (`COMPILE_FAN_OUT`).
2. `If` has exactly one `true` edge and exactly one `false` edge (`COMPILE_IF_PORT_COUNT`); other ports are rejected.
3. `Fork` has either the full `branch[0..N-1]` set or exactly one `all` (`COMPILE_FORK_PORTS`); `out` is forbidden.
4. `Fork.join` must reference an existing `merge` node (`COMPILE_FORK_JOIN_NOT_MERGE`).
5. `Merge` must be the join of exactly one Fork (`COMPILE_MERGE_UNPAIRED`, `COMPILE_MERGE_REUSED`).

### 9.4. Fork/Merge nesting

The validator walks every reachable path with an explicit stack of open Fork scopes:

- `end` is allowed only with an empty stack; otherwise `COMPILE_FORK_BYPASS_MERGE`.
- Merge must close the **top** stack frame; otherwise `COMPILE_MERGE_SCOPE_MISMATCH`.
- Re-entering the same Fork before its Merge is forbidden (`COMPILE_FORK_REENTRY`).
- Every branch, including both If outcomes, must have a route to its Merge (`COMPILE_FORK_BRANCH_UNJOINED`).
- Merge without an open Fork (`COMPILE_MERGE_WITHOUT_FORK`) and entry into the middle of a foreign scope are rejected.
- Nested pairs do not overlap; the inner Merge closes before the outer one.

### 9.5. Cycles

- Cycles **outside** open Fork/Merge scopes are allowed if they have a reachable exit.
- Cycles inside a Fork branch are allowed only if every branch path still has a route to the paired Merge; otherwise publication is rejected.
- Re-entering a Fork after its Merge closes creates a **new** Fork Instance; old arrivals do not mix with new ones.
- Static termination of an arbitrary cycle cannot be proven. Runtime applies `VFLOW_MAX_TRANSITIONS` and `VFLOW_MAX_RUN_SECONDS`; exceeding them yields `LIMIT_TRANSITIONS` / `LIMIT_RUN_SECONDS`.

### 9.6. Tasks and handlers

- During `validate`/`publish`, the service verifies that each `task_id` exists (`TASK_NOT_FOUND`).
- Tasks are checked again when a Run is created; a missing task yields `TASK_UNAVAILABLE`.
- Required handler versions must be registered (`required_handlers`); an unknown version makes the version unpublishable or marks health `degraded`.
- A graph that runtime cannot execute must not be published.

### 9.7. Common diagnostic codes

| Code | Cause |
|---|---|
| `PARSE_*` | Syntax error, unterminated block/string, invalid literal. |
| `COMPILE_DUPLICATE_STEP` | Repeated step ID. |
| `COMPILE_UNKNOWN_ARGUMENT` | Unexpected call argument. |
| `COMPILE_INVALID_ARGUMENT` | Wrong argument type/value. |
| `COMPILE_FAN_OUT` | Multiple outgoing edges from an ordinary node. |
| `COMPILE_IF_PORT_COUNT` | Not exactly one `true`/`false` edge. |
| `COMPILE_FORK_PORTS` | Incomplete branch ports or mixing with `all`. |
| `COMPILE_MERGE_UNPAIRED` | Merge without a Fork. |
| `COMPILE_FORK_BRANCH_UNJOINED` | Branch does not reach its Merge. |
| `COMPILE_FORK_BYPASS_MERGE` | Path to `end` while a Fork is open. |
| `TASK_NOT_FOUND` | Task does not exist at validate/publish. |

---

## 10. Limits and configuration

### 10.1. Environment variables

Values are read at application startup. Changing `.env` requires a restart. An invalid value prevents startup.

| Variable | Default | Purpose |
|---|---:|---|
| `VFLOW_ENABLED` | `true` | Main feature flag. When `false`, VFlow routes are not registered and background loops do not start. |
| `VFLOW_MAX_FORK_N` | `32` | Maximum Fork `count`, enforced by runtime. |
| `VFLOW_MAX_ACTIVE_POINTS` | `128` | Maximum active points in a Run when creating a Fork. |
| `VFLOW_MAX_TOTAL_POINTS` | `1000` | Maximum points created in a Run. |
| `VFLOW_MAX_TRANSITIONS` | `10000` | Maximum Run transitions. |
| `VFLOW_MAX_JSON_BYTES` | `1048576` (1 MiB) | Maximum JSON value size. |
| `VFLOW_MAX_JSON_DEPTH` | `64` | Maximum JSON depth. |
| `VFLOW_MAX_RUN_SECONDS` | `86400` | Maximum Run duration; checked while processing points. |
| `VFLOW_RUNS_PER_MINUTE` | `30` | Per-user run limit in the window. |
| `VFLOW_RUN_WINDOW_SECONDS` | `60` | Rate-limit window. |
| `VFLOW_RETENTION_DAYS` | `30` | Retention for finished Runs. |

Example:

```dotenv
VFLOW_ENABLED=true
VFLOW_MAX_FORK_N=32
VFLOW_MAX_ACTIVE_POINTS=128
VFLOW_MAX_TOTAL_POINTS=1000
VFLOW_MAX_TRANSITIONS=10000
VFLOW_MAX_JSON_BYTES=1048576
VFLOW_MAX_JSON_DEPTH=64
VFLOW_MAX_RUN_SECONDS=86400
VFLOW_RUNS_PER_MINUTE=30
VFLOW_RUN_WINDOW_SECONDS=60
VFLOW_RETENTION_DAYS=30
```

### 10.2. Where each limit is enforced

| Limit | Check point | Error code |
|---|---|---|
| `VFLOW_MAX_FORK_N` | `SpawnFork` | `LIMIT_FORK_COUNT` |
| `VFLOW_MAX_ACTIVE_POINTS` | `SpawnFork` | `LIMIT_ACTIVE_POINTS` |
| `VFLOW_MAX_TOTAL_POINTS` | `SpawnFork` | `LIMIT_TOTAL_POINTS` |
| `VFLOW_MAX_TRANSITIONS` | Every transition | `LIMIT_TRANSITIONS` |
| `VFLOW_MAX_JSON_BYTES` | Run input, node output, Merge | `LIMIT_JSON_BYTES` |
| `VFLOW_MAX_JSON_DEPTH` | Run input, node output, Merge | `LIMIT_JSON_DEPTH` |
| `VFLOW_MAX_RUN_SECONDS` | Point processing | `LIMIT_RUN_SECONDS` |
| `queue_timeout_seconds` | Task in queue | `TASK_QUEUE_TIMEOUT` |
| `execution_timeout_seconds` | Task after start | `TASK_EXECUTION_TIMEOUT` |

The compiler accepts `fork count` up to 32 regardless of `VFLOW_MAX_FORK_N`. If the env limit is lower, a graph may be saved and published but its Run fails with `LIMIT_FORK_COUNT`. Keep the env value and per-pipeline policy aligned with the publication policy.

### 10.3. Per-pipeline policy: tightening only

Definition settings live in `settings_json`. A policy may **reduce** global limits but must never increase them.

```http
PUT /api/admin/pipelines/{definition_id}/settings
Content-Type: application/json
x-csrf-token: <csrf-token>

{
  "settings": {
    "limits": {
      "max_fork_n": 8,
      "max_transitions": 2000,
      "max_json_bytes": 65536
    },
    "cancel_own": false
  }
}
```

Rules:

- Allowed fields are `limits` and `cancel_own`; unknown fields are rejected.
- `limits` accepts only the lower-case `VFLOW_MAX_*` names (`max_fork_n`, `max_active_points`, `max_total_points`, `max_transitions`, `max_json_bytes`, `max_json_depth`, `max_run_seconds`).
- Values are positive integers; a value above the global cap is rejected.
- Supplied fields override globals; omitted fields inherit them.
- Effective limits are pinned into the Run at creation (`limits_json`) and do not change retroactively for an already started Run.
- `cancel_own` (default `true`) allows the initiator to cancel their own Run.

### 10.4. Rate limit and retention

- The rate limit applies to `POST /api/pipelines/{identifier}/runs`, is counted per user over a sliding window and returns HTTP `429 VFLOW_RATE_LIMITED` with `Retry-After`.
- The rate limiter is process-local; with multiple processes/replicas it is not shared.
- Retention deletes only finished Runs (`succeeded`, `failed`, `cancelled`) older than `VFLOW_RETENTION_DAYS`. Definitions, versions, active Runs and audit records are not deleted.
- The retention loop runs at startup and then every 24 hours. Before the first run on a large database, take a backup and do a dry-run.

---

## 11. Errors and observability

### 11.1. Compilation diagnostics

Parser and compiler errors contain `code`, `message`, `line`, `column`, `node_id` and `edge_id` (when applicable). The API returns them in this envelope:

```json
{
  "error": {
    "code": "VFLOW_VALIDATION_ERROR",
    "message": "Workflow validation failed",
    "details": {
      "diagnostics": [
        {
          "code": "COMPILE_FORK_BRANCH_UNJOINED",
          "message": "Branch 0 of Fork 'split' has no route to Merge 'combined'",
          "line": 12,
          "column": 5,
          "node_id": "split",
          "edge_id": null
        }
      ]
    },
    "request_id": "req-or-uuid"
  }
}
```

`POST /api/admin/pipelines/{id}/validate` returns `200` with `{"valid": false, "diagnostics": [...]}`; `compile` and `publish` return `422 VFLOW_VALIDATION_ERROR` on validation failure.

### 11.2. Runtime errors

On an unrecoverable error, the Run becomes `failed`. Active points and open Fork Instances become `cancelled`, unfinished Task links become `failed`, and a `RUN_FAILED` event is written.

The public Run status exposes only safe fields:

```json
{
  "status": "failed",
  "error": {
    "code": "TASK_OUTPUT_NOT_JSON",
    "message": "Task output is not valid JSON"
  }
}
```

Internal tracebacks, prompts and intermediate JSON are not exposed to users. Administrators can inspect points and events through `GET /api/admin/vflow/runs/{run_id}`.

### 11.3. Task result modes

| `result_mode` | Behavior | `task.output.json` |
|---|---|---|
| `auto` | Tries to parse the entire text as JSON; a parse error does not fail the Run. | Parsed value or `null`. |
| `strict_json` | Requires valid JSON; otherwise `TASK_OUTPUT_NOT_JSON`. | Parsed value. |
| `none` | Does not parse JSON; the text is retained. | `null`. |

`strict_json` does not extract JSON from a Markdown block or arbitrary prose: it parses the entire final answer. `none` is useful when a downstream node needs only text.

`output_mode: "json"` takes precedence over these legacy modes. It produces both `$.output.json` and the backward-compatible `$.task.output.json`. With `on_json_error: "repair"`, invalid JSON (or a schema mismatch for `invalid_json_or_schema`) goes through the JSON Processor worker; on failure the Run fails with `OUTPUT_INVALID`. Use `output_mode: "json"` plus `output_schema` for new pipelines; keep `result_mode`/`max_json_repairs` only for existing ones.

### 11.4. Task retries and timeouts

- `retries` is the number of **additional** attempts. Each attempt is a new Task Run, a new `attempt` and a new `vflow_task_links` row.
- `queue_timeout_seconds` applies while the task is `queued` and has not started. On expiry the task is cancelled; if `attempt <= retries`, a new attempt is created; otherwise the Run fails with `TASK_QUEUE_TIMEOUT`.
- `execution_timeout_seconds` applies after start. The task is cancelled and the Run fails with `TASK_EXECUTION_TIMEOUT`; there is no automatic retry, to avoid repeating side effects.
- `TASK_CHANGED`, `TASK_UNAVAILABLE` and `TASK_CANCELLED` are not retried.
- A network error while polling a task is not a task failure: waiting continues with backoff and no new task is created.

### 11.5. Cancellation

- The initiator may cancel their own Run unless `cancel_own` forbids it; an administrator may cancel any Run.
- Cancellation sets the Run to `cancelled`, active points and open Fork Instances to `cancelled`; no new Tasks are enqueued.
- The current implementation **does not call** `gateway.cancel` for already enqueued Tasks. An external task may continue and produce side effects; use the normal VALERIA queue/admin controls to stop the agent.
- Cancellation is idempotent: repeating the request for a terminal Run creates no new transitions.

### 11.6. Events, health and metrics

Run events: `RUN_STARTED`, `POINT_ADVANCED`, `TASK_ENQUEUED`, `TASK_RETRY`, `TASK_QUEUE_TIMEOUT`, `TASK_EXECUTION_TIMEOUT`, `FORK_SPAWNED`, `MERGE_ARRIVED`, `MERGE_COMPLETED`, `MERGE_DUPLICATE_ARRIVAL`, `MERGE_STALE_ARRIVAL`, `MERGE_RECOVERED`, `RUN_SUCCEEDED`, `RUN_FAILED`, `RUN_CANCELLED`.

Events contain structural IDs, port, branch index and count, but not Run input or prompts. Payloads are redacted. Admin point data (`data`) and events are available only through the admin API.

| Endpoint | What it shows |
|---|---|
| `GET /api/admin/vflow/health` | Database, handler versions, active Runs, leases and outbox state. |
| `GET /api/admin/vflow/metrics` | Run/point/outbox aggregates and top errors by code. |
| `GET /api/admin/vflow/retention` | Retention settings and deletion candidates. |
| `GET /api/admin/vflow/runs/{id}` | Points, `fork_stack`, `data`, events and Task snapshots. |

`health = degraded` usually means an active Run references a missing handler version or the database schema is incomplete. The startup check logs a warning but does not block application startup.

---

## 12. Authoring workflow in the UI and API

### 12.1. Create tasks

VFlow does not create Task definitions. First create and configure the task in the **normal VALERIA task section** (prompt, model, queue, workspace policy). Then use its `task_id` in `task.run`. The VFlow palette may show unpublished tasks: publishing a pipeline delegates permission to run them in that context. A user does not get direct access to the task.

### 12.2. Create a definition

In the admin “Pipeline Editor” (`#/admin-pipelines`, `#/admin-pipeline-editor`), an administrator creates a definition: slug, title, description. API:

```http
POST /api/admin/pipelines
Content-Type: application/json
x-csrf-token: <csrf-token>

{"slug": "review-changes", "title": "Review changes", "description": "Three-way review"}
```

### 12.3. Graph or code

- The “Graph” tab edits canonical Graph IR: nodes, edges, ports, positions.
- The “VFlow code” tab edits DSL.
- Switching graph → code generates canonical DSL; code → graph is possible only after successful compilation.
- If both `source` and `graph` are sent on save, their canonical graphs must match; otherwise `400`.
- Comments, whitespace and formatting may be normalized on round-trip; semantics must be preserved.
- The source is saved in the draft. On a parse error, the previous valid IR is not overwritten.

### 12.4. Validate and publish

1. **Validate** — `POST /api/admin/pipelines/{id}/validate`: syntax, structure, Fork/Merge, handler versions, task existence.
2. **Publish** — `POST /api/admin/pipelines/{id}/publish`: only for a valid draft; creates an immutable version and makes it published.
3. **Rollback** — `POST /api/admin/pipelines/{id}/rollback` with `version_id`: reactivates a previous published version.
4. **Archive** — stops new Runs but does not cancel active ones.

The draft is updated through `PUT /api/admin/pipelines/{id}/draft`. If a draft already exists, `If-Match: "<etag>"` is required; otherwise `412 VFLOW_PRECONDITION_FAILED`. This prevents overwriting concurrent edits.

### 12.5. Audience, schemas and policy

- **Audience:** `{"mode": "all"}` or `{"mode": "selected", "user_ids": ["..."]}`. A hidden pipeline looks like `404` to a user; the server checks audience for the catalog and Run creation.
- **`input_schema`:** a safe JSON Schema subset for the form and server-side input validation. Supported: `type` (`object`, `array`, `string`, `boolean`, `integer`, `number`, `null`), `enum`, `required`, `properties`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern` (Python `re.search`), `minimum`, `maximum`. `additionalProperties`, `format`, `oneOf`, `anyOf`, `$ref` are ignored.
- **`public_output_schema`:** an allowlist projection of the public result. An empty schema or `null` means status-only. `GET /api/pipeline-runs/{id}/result` returns only a successful Run and only allowed fields. This is a safe projection, not full schema validation.
- **`settings`:** per-pipeline policy, tightening limits only, plus `cancel_own` (see section 10.3).

### 12.6. Run

A user opens `#/pipelines`, selects an available published pipeline, fills the form generated from `input_schema`, and starts it:

```http
POST /api/pipelines/review-changes/runs
Content-Type: application/json
x-csrf-token: <csrf-token>
Idempotency-Key: <uuid>

{"input": {"repository": "example/repo", "pr": 24}}
```

The `202` response contains `id`, `status`, `pipeline` and `progress`. Repeating the same `Idempotency-Key` with the same input returns the existing Run; the same key with different input returns `409 IDEMPOTENCY_CONFLICT`. `workspace_id` comes from the user profile, not from input JSON.

### 12.7. Main API routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/pipelines` | Catalog of available published pipelines. |
| `GET` | `/api/pipelines/{identifier}` | Public metadata + `input_schema`. |
| `POST` | `/api/pipelines/{identifier}/runs` | Create a Run (`202`). |
| `GET` | `/api/pipeline-runs` | Own run history. |
| `GET` | `/api/pipeline-runs/{run_id}` | Own Run: status, progress, safe error. |
| `GET` | `/api/pipeline-runs/{run_id}/result` | Public result through the allowlist schema. |
| `POST` | `/api/pipeline-runs/{run_id}/cancel` | Cancel own Run. |
| `GET` | `/api/admin/pipelines` | All definitions. |
| `POST` | `/api/admin/pipelines` | Create a definition. |
| `PUT` | `/api/admin/pipelines/{id}/draft` | Save draft (`If-Match`). |
| `POST` | `/api/admin/pipelines/{id}/validate` | Validate. |
| `POST` | `/api/admin/pipelines/{id}/publish` | Publish a version. |
| `POST` | `/api/admin/pipelines/{id}/rollback` | Roll back publication. |
| `PUT` | `/api/admin/pipelines/{id}/audience` | Set audience. |
| `PUT` | `/api/admin/pipelines/{id}/settings` | Set policy. |
| `GET` | `/api/admin/vflow/block-registry` | Block types and schemas. |
| `GET` | `/api/admin/vflow/tasks-catalog` | VALERIA tasks for the palette. |
| `POST` | `/api/admin/vflow/compile` | DSL → IR + diagnostics. |
| `POST` | `/api/admin/vflow/format` | IR → canonical DSL. |
| `POST` | `/api/admin/vflow/preview-transform` | Preview Transform without a Task Run. |
| `POST` | `/api/admin/vflow/preview-if` | Preview If (`result`, `port`). |

---

## 13. Security and best practices

### 13.1. Role separation

- **Administrator** creates tasks, definitions, drafts, validates, publishes, configures audience/schemas/policy and sees diagnostics.
- **User** (`user`, `tasks_only` when access is enabled) sees only published pipelines available to them, runs them, and views their own Runs and safe progress.
- Users do not see Graph IR, DSL, `node_id`, `task_id`, `task_run_id`, `fork_stack`, intermediate JSON or prompts.
- All checks are server-side; hiding a UI button is not security.

### 13.2. Language restrictions

- No `eval`, `exec`, `import`, shell, SQL, dynamic calls or user-defined functions.
- JSONPath is a strictly limited subset over JSON data; keys are not Python/JS attributes.
- `Transform` works only with JSON and has no network/file side effects.
- New system blocks are added **only by a VALERIA developer** through code, Registry, tests and deployment. An administrator cannot upload plugins or execute code through a web form.

### 13.3. Tasks and workspaces

- A `Task` is enqueued through the existing queue as the Run initiator and in their permitted workspace.
- Publishing a version delegates permission to run specific Tasks in the pipeline context; the user gets no direct access to a hidden task.
- The Task snapshot pins the task version at Run start; a change after start yields `TASK_CHANGED`.
- A workspace is not a full sandbox. Multiple Fork branches may modify the same files concurrently; warn authors about conflicts and use VALERIA isolation/queue controls.

### 13.4. Data, results and secrets

- By default, the public result is status-only. Enable `public_output_schema` deliberately and only as an allowlist.
- Do not expose intermediate JSON, prompts, reasoning, tool messages or task snapshots to users.
- Do not put tokens, passwords, keys or personal data into `input`, `config`, `task_id`, `public_output_schema`, events, audit records or documentation.
- Task snapshots contain metadata only (`task_id`, title, updated_at, agent/provider/model, queue_id), without prompt/content.
- Admin point diagnostics may contain sensitive JSON; restrict admin API access and database backups.
- Use fictional IDs, `example/repo` and placeholder tokens in documentation and tests.

### 13.5. Practical authoring advice

1. Create and test the Task in the normal task section first.
2. Start with a sequential graph; add Fork only for genuinely independent work.
3. Every Fork branch must have an explicit route to its paired Merge, including both If outcomes.
4. Do not fan out an ordinary node; use Fork.
5. Test `input_path` and Transform paths against real input with preview endpoints.
6. Use `strict_json` for structured answers and `none` for free text.
7. Set queue/execution timeouts and retries deliberately; execution timeout is not retried.
8. Keep a cycle with a reachable exit and account for the transition limit.
9. Publish a new version instead of editing an active one; an active Run is pinned to the old version.
10. Test audience and the public schema with a non-admin user first.

---

## 14. Quick reference

### 14.1. Node calls and ports

| DSL call | IR type | Input | Outputs | Required arguments | Main optional arguments |
|---|---|---|---|---|---|
| `task.run(...)` | `valeria.task@1.0` | `in` | `out` | `task_id` | `input_path`, `input`, `result_mode`, `output_mode`, `output_schema`, `on_json_error`, `repair_trigger`, `queue_timeout_seconds`, `execution_timeout_seconds`, `retries` |
| `transform(...)` | `core.transform@1.0` | `in` | `out` | `operations` | `create_missing` |
| `if(...)` | `core.if@1.0` | `in` | `true`, `false` | `path`, `operator` | `value`, `missing` |
| `fork(...)` | `core.fork@1.0` | `in` | `branch[0..N-1]` or `all` | `count`, `join` | — |
| `merge(...)` | `core.merge@1.0` | `in` (N edges) | `out` | — | `mode` (only `all`) |

### 14.2. Edge syntax

| Form | Meaning |
|---|---|
| `a -> b -> c` | `a -> b`, `b -> c`. |
| `start -> a` | The only Run entry is node `a`. |
| `a -> end` | Terminate the point at the terminal. |
| `decision.true -> a` | Transition through the If true port. |
| `decision.false -> b` | Transition through the If false port. |
| `split[0] -> a` | Explicit Fork branch with index 0. |
| `split -> a` | `all` shorthand: every branch goes to `a`. |
| `a.true -> b` | Error if `a` is not an If. |
| `a -> b.true` | Error: a port on the target is forbidden. |

### 14.3. Fast contracts

| After node | Data shape |
|---|---|
| `Task` | `{"input": <full input>, "output": {"text", "json"}, "task": {"task_id", "run_id", "status", "output": {"text", "json"}}}` (`output` exists for `output_mode=json`; `task.output` remains for all modes) |
| `Transform` | Result of the last operation (a new copy). |
| `If` | The same JSON, unchanged. |
| `Fork` | N independent copies of the input JSON. |
| `Merge` | `{"source": <Fork input>, "branches": [{"index": i, "data": <branch data>}]}` |

### 14.4. Minimal working skeleton

```vflow
workflow "hello" {
    version: 1;
    input: json;

    step echo = transform(operations: [
        {op: "set", path: "$.ok", value: true}
    ]);

    start -> echo -> end;
}
```

---

## Links

- [VFlow Technical Specification](VALERIA_VFlow_Technical_Specification_RU.md)
- [ADR-001…004](VALERIA_VFlow_ADR_RU.md)
- [VFlow Operations Documentation](VALERIA_VFlow_Operations_RU.md)
- [VFlow Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md)
- [VFlow QA Test Plan](VALERIA_VFlow_QA_Test_Plan_RU.md)
- [Block Compatibility](VALERIA_VFlow_Block_Compatibility_RU.md)
- [Implementation Backlog](VALERIA_VFlow_Implementation_Backlog_RU.md)
- [Execution Plan](VALERIA_VFlow_Execution_Plan_RU.md)
- [`.env.example`](../.env.example)
- [Russian version of this guide](VALERIA_VFlow_Language_Guide_RU.md)

Sources that define normative behavior:

- `src/opencode_web/vflow/parser.py` — lexer and parser.
- `src/opencode_web/vflow/compiler.py` — compilation and structural validation.
- `src/opencode_web/vflow/logic.py` — Transform and If.
- `src/opencode_web/vflow/handlers.py` — Task/Transform/If/Fork/Merge action planning.
- `src/opencode_web/vflow/runtime.py` — points, transitions, Fork/Merge, Task, limits.
- `src/opencode_web/vflow/policy.py`, `config.py`, `limits.py` — policy and limits.
- `src/opencode_web/vflow/service.py`, `api.py` — validate/publish, audience, schemas, HTTP API.
