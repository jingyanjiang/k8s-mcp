---
name: k8s-ops
description: Opinionated multi-step Kubernetes workflows on top of the k8s-mcp server. Encodes tool-sequencing logic and failure-mode decision trees for deploying from a repo, debugging pods/rollouts, performing safe restarts, and auditing cluster posture. Use when the user asks to deploy, diagnose, restart, roll out, or audit Kubernetes workloads.
user-invocable: true
argument-hint: [MODE=deploy|debug|rollout|audit] [NAMESPACE=<ns>]
---

You are operating against a Kubernetes cluster through the **k8s-mcp** server. The server already enforces these rules — do not restate them, just follow them:

- Confirm namespace before any operation if not explicitly provided
- Confirm name + namespace + cluster before any destructive op (delete, scale-to-0, restart)
- Use `get_current_context` to verify the active cluster before changes
- Use list/get tools for discovery rather than guessing

This skill adds **workflow sequencing** on top of those rules. Pick the mode that matches the user's request.

---

## Mode dispatch

| User intent | Mode |
|---|---|
| "deploy this app/repo to my cluster" | `deploy` |
| "my pod is crashing / failing / not ready" | `debug` |
| "restart / roll out / update this deployment" | `rollout` |
| "review RBAC / secrets / who can access what" | `audit` |

If the request blends modes (e.g., "deploy and debug if it fails"), run them in sequence — `deploy` first, then `debug` if `wait_for_ready` reports failure.

---

## Mode: deploy

For "deploy this repo / app to a cluster" requests.

### Preconditions (confirm with user before touching the cluster)
1. Active cluster — call `get_current_context`, show the cluster name, ask if it's the right target.
2. Target namespace — if unsure, `list_namespaces` and ask. Do not default to `default`.
3. Container image — registry, repo, tag. If the repo has a Dockerfile but no built image, flag that the user must build/push first; do not attempt the build inside this skill.
4. ImagePullSecret — ask only if the registry is private.

### Sequence
1. **Analyze repo** — read `Dockerfile`, `pyproject.toml`/`package.json`, any existing `k8s/` or `manifests/` directory. If manifests already exist, prefer `apply_kustomize` or `apply_manifest` on what's there over generating new ones.
2. **Generate manifests if needed** — `generate_deploy_manifests` with the confirmed values. Write to a `k8s/` directory in the repo, not a temp dir.
3. **Show the plan** — list the resources to be created, the namespace, and the cluster. Wait for user approval.
4. **Apply** — `apply_kustomize` is **required** for manifests produced by `generate_deploy_manifests`: the registry/tag substitution lives in `kustomization.yaml`'s `images:` field. Applying the individual `deployment.yaml` with `apply_manifest` will leave the placeholder `image: k8s-mcp` and the pod will fail to pull. Use `apply_manifest` only for hand-written single-document manifests with fully qualified image references.
5. **Wait** — `wait_for_ready` with `resource_type=deployment`, a sensible timeout (120s default; 300s for first-time pulls of large images).
6. **Verify** — on success, `get_deployment` + `list_pods` (label-selector scoped). Report endpoint(s) from `get_service` if a Service was created.
7. **On failure of `wait_for_ready`** — switch to `debug` mode automatically on the failing pod. Do not retry the apply blindly.

### Output convention
After success, report in this order: cluster → namespace → workload → replicas ready → service endpoint (if any). Keep it under 6 lines.

---

## Mode: debug

For "this pod / deployment is broken" requests. The decision tree is the value here — `diagnose_pod` is your default entry, but it doesn't cover every failure shape.

### Step 0 — is anything actually broken?
Don't assume failure just because the user said "debug". Before running `diagnose_pod`:
- If the user named a pod: check `get_pod` for `status.phase == "Running"` and all `containerStatuses[*].ready == true` and `restartCount == 0`. If healthy, report "no failure detected — pod is Ready, 0 restarts" and ask what specifically looks wrong before going deeper.
- If the user named a deployment with `spec.replicas == 0`: this is **scaled to zero / paused**, not broken. Report that explicitly. Don't run `diagnose_pod` on nothing.
- If the user named only a namespace and no pod has `status != Running` or `restartCount > 0`: report "no unhealthy pods in namespace X" and stop. Don't pick a healthy pod and run diagnostics on it for show.

### Step 1 — orient
- If the user named a pod: `diagnose_pod` directly.
- If the user named a deployment: `get_rollout_status` first; if stuck, `list_pods` with the deployment's label selector and pick the failing one for `diagnose_pod`.
- If the user named only a namespace: `list_pods` and look for pods with `status != Running` or `restartCount > 0`.

### Step 2 — interpret `diagnose_pod` output and branch

| Symptom in diagnose_pod output | Next tool | Why |
|---|---|---|
| `ImagePullBackOff` / `ErrImagePull` | `get_pod` (check imagePullSecrets), `list_secrets` in ns | Verify pull secret exists and is referenced |
| `CrashLoopBackOff` with logs | Read logs in `diagnose_pod` output; if truncated, `get_pod_logs` with larger `tail_lines` and `previous=true` | Previous container instance has the actual crash |
| `CrashLoopBackOff` with no logs | `describe_resource pod` then `list_events` filtered to the pod | Init container or scheduling issue |
| `Pending` + scheduling events | `list_nodes`, `top_nodes` | Resource pressure or taints |
| `Pending` + PVC events | `describe_resource pvc <name>` | Storage class / volume issue |
| `OOMKilled` in lastState | `top_pods`, then `get_deployment` for current limits | Confirm limit is the bottleneck before suggesting bump |
| `Running` but not `Ready` | `exec_command` to hit the readiness endpoint from inside the pod | Distinguish app-not-ready from probe-misconfigured |
| Connection / DNS issues reported by app | `exec_command` with `nslookup` / `curl` to dependent services; `get_service` on the dep | Verify cluster DNS and Service selector match |

### Step 3 — report
Always structure the report as: **Symptom** (one line) → **Root cause** (one line, evidence-backed) → **Suggested fix** (concrete: manifest change, env var, resource bump, etc.). Do NOT apply the fix without explicit user approval.

### Tool quirks
- `diagnose_pod` only includes a `--- Logs ---` section for *failing* containers. A healthy pod's output has no logs section even with `tail_lines` set — that's expected, not a tool bug.

### Anti-patterns
- Don't dump raw `diagnose_pod` JSON at the user — extract the signal.
- Don't loop `diagnose_pod` more than twice on the same pod expecting different output. If the first call is inconclusive, escalate to `describe_resource` + `list_events`.
- Don't suggest "increase replicas" as a fix for a single failing pod — that masks the bug.
- Don't run diagnostics on a healthy pod just because the user said "debug" — see Step 0.

---

## Mode: rollout

For restart / rolling update / "push the new image" requests. The risk here is a botched rollout taking down a healthy service.

### Pre-flight (before any change)
1. `get_current_context` — confirm cluster.
2. `get_deployment` — record current image, replicas, strategy.
3. `list_pods` with the deployment's label selector — confirm current replicas are healthy. If they aren't, surface that to the user and ask whether to proceed (rolling out on top of a broken state usually makes things worse).
4. `list_events` in the namespace — surface any recent warnings.
5. **Confirm with user**: cluster, namespace, deployment, action (restart vs new image), and rollback plan.

### Action
- Restart only: `restart_deployment`.
- New image / config: user provides the manifest change → `apply_manifest` or `apply_kustomize`.
- Scale change: `scale_deployment`.

### Watch
- `get_rollout_status` immediately after — capture initial state.
- `wait_for_ready resource_type=deployment` with timeout sized to: `replicas * (terminationGracePeriodSeconds + readinessProbe.initialDelaySeconds + 30s buffer)`. Default to 300s if unsure.
- If `wait_for_ready` fails or `get_rollout_status` shows stuck progression: pick a failing pod from `list_pods` and run `diagnose_pod`. Do not keep polling.

### Tool quirks
- For deployments scaled to zero (`spec.replicas == 0`), restarting bumps the rollout annotation but no pods spin up — restart is effectively a no-op. The patched `get_rollout_status` and `wait_for_ready` correctly report "complete" in this case; older versions of k8s-mcp (< the fix for `or 1`) report a phantom "1 desired" and time out. If you see that, upgrade or call `get_deployment` directly to read the real `spec.replicas`.

### Rollback
If the rollout is failing and the user wants to revert, do not invent commands. State plainly: "k8s-mcp does not expose `kubectl rollout undo`. To roll back, re-apply the previous manifest, or run `kubectl rollout undo deployment/<name> -n <ns>` outside this session." Then offer to re-apply the prior image if the user can supply it.

### Output convention
Report: previous image/replicas → new image/replicas → rollout duration → final ready count. One block, no narration.

---

## Mode: audit

For "review RBAC", "what secrets exist", "who can access X", or general posture-check requests. This is **read-only** — never modify anything in this mode.

### Scoping
- Cluster-wide audit: `all_namespaces=true` on listings. Requires cluster-read RBAC; if it 403s, fall back to per-namespace and tell the user which namespaces you couldn't read.
- Single-namespace audit: confirm the namespace first.
- **Namespace-scoped kubeconfig**: most users don't have cluster-read. The patched `list_role_bindings include_cluster_role_bindings=true` and `list_roles include_cluster_roles=true` now return the namespace-scoped portion plus a `# ClusterRoleBindings not listed: Forbidden` footer instead of failing wholesale — surface that footer in your report so the user knows CRB scope wasn't covered.

### Default sweep
Run these in parallel (independent reads):
1. `list_service_accounts` — inventory of identities.
2. `list_role_bindings include_cluster_role_bindings=true` — what those identities are bound to.
3. `list_roles include_cluster_roles=true` — what permissions those bindings grant.
4. `list_secrets` — inventory only. Do NOT call `get_secret decode=true` unless the user explicitly asks to inspect a specific secret's value.
5. `list_pods` + `list_deployments` — workload inventory for cross-reference.

### Cross-reference (the actual analysis)
- For each ClusterRoleBinding to `cluster-admin`: list the subjects. Flag any ServiceAccount in a workload namespace as high-risk.
- **`default` SA bound to `admin`/`edit`/`cluster-admin`** (very common antipattern in shared-namespace clusters): always flag `[HIGH]`. Every workload that doesn't explicitly set `spec.serviceAccountName` runs with those permissions; combined with the default `automountServiceAccountToken: true`, any container compromise = full SA scope.
- For ServiceAccounts referenced by pods: parse `spec.serviceAccountName` from one representative pod **per Deployment** (use the deployment's label selector to pick one), not every pod — `get_pod` returns hundreds of lines and N pods × that scales badly. Then trace SA → RoleBinding → Role and report effective permissions.
- For Secrets of type `kubernetes.io/service-account-token` that aren't auto-mounted to any pod: flag as candidates for cleanup.
- For Secrets > 90 days old (creation timestamp from list output): flag for rotation review.

### Report
Structure:
1. **Inventory summary** — counts per resource type.
2. **Findings** — bulleted, severity-tagged: `[HIGH]` / `[MED]` / `[LOW]`. Each finding cites the specific resource (`namespace/name`) and the specific rule violated.
3. **No-action-taken note** — explicit: "audit mode is read-only; no changes were made."

---

## Cross-mode rules

- **Token economy**: prefer scoped lists over `all_namespaces=true` unless the user explicitly asked cluster-wide. The latter can return hundreds of resources.
- **Parallel reads**: independent list/get calls in the same step should be issued in parallel, not sequentially.
- **Confirmations apply per scope**: a user saying "go ahead" to a `restart_deployment` does NOT authorize a subsequent `delete_resource`. Re-confirm for each destructive call.
- **Secrets**: never decode secrets unless the user explicitly asks for a specific one by name. Never echo decoded values into a summary or log.
- **Stop conditions**: if any tool returns a 403, do not retry — surface the missing RBAC to the user and ask how to proceed.
