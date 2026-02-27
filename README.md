# k8s-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes Kubernetes operations as tools for AI assistants. It lets AI agents like Claude directly query and manage your Kubernetes cluster through natural language.

## Why use this?

AI assistants without k8s-mcp can only suggest `kubectl` commands for you to copy-paste. With k8s-mcp, they can:

- **Faster and lighter than shell tools** — native MCP tools return structured data directly to the AI, avoiding shell spawning, CLI output parsing, and large text streaming. This means faster responses, lower token usage, and a more hands-off experience compared to agents running `kubectl` via shell
- **Secure by design** — reuses your existing `~/.kube/config`, which is authenticated through your organization's auth flow (e.g., SSO, OIDC, certificate). The server never stores, transmits, or manages credentials itself
- **Diagnose issues in one shot** — `diagnose_pod` combines status, conditions, events, and logs from failing containers into a single report. No more juggling `describe` and `logs` separately
- **Query, manage, and take action** — list pods, read logs, inspect configs, scale deployments, restart workloads, apply manifests, and delete resources — all within the conversation, with your approval for destructive operations
- **Autonomous deploy loops** — apply manifests, poll with `wait_for_ready` until pods are healthy, and iterate on failures — all without manual intervention
- **Generate deployment manifests** — scaffold production-ready Kustomize manifests (ServiceAccount, RBAC, Deployment, Service) and apply them to the cluster in one step
- **Detect config drift** — export live resources as YAML with `get_resource_yaml` and compare against local manifests
- **Work with any MCP client** — supports Claude Code, Codex CLI, Gemini CLI, Opencode, and any client that speaks stdio, HTTP, or SSE

## Prerequisites

- **Python 3.10+**
- **Poetry**, **uv**, or **pip** (for dependency management)
- **kubectl access configured** — the server reads your `~/.kube/config` automatically

### Kubeconfig setup

The server uses your existing kubeconfig (`~/.kube/config`) to connect to the cluster. Make sure you can run `kubectl` commands before using this server:

```bash
# Verify your cluster connection
kubectl auth whoami

# Verify access to your namespace
kubectl get all -n <your-namespace>
```

The server inherits whatever permissions your kubeconfig user has. No additional credentials are needed.

> **Note:** Some operations (e.g., `list_namespaces`, `list_nodes`) require cluster-wide permissions that your user may not have. If a request fails with a `403 Forbidden` error, ask your cluster admin to grant the necessary RBAC roles.

## Installation

```bash
git clone git@github.com:jingyanjiang/k8s-mcp.git
cd k8s-mcp
```

### Option 1: pipx (recommended)

Install globally with an isolated environment — no virtualenv activation needed:

```bash
pipx install .
```

This puts `k8s-mcp` on your PATH and works from any directory.

### Option 2: uv tool

```bash
uv tool install .
```

### Option 3: Poetry (development)

```bash
poetry install
```

Use `poetry run k8s-mcp` to run, or set `cwd` in your MCP client config.

### Option 4: pip

```bash
pip install .
```

## Configuration

### Claude Code / Claude Desktop

Add to `.mcp.json` (project-level) or `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "k8s": {
      "type": "stdio",
      "command": "k8s-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

> **Note:** If your MCP client can't find `k8s-mcp` on PATH, use the absolute path instead for the `command` value (run `which k8s-mcp` to find it).

### OpenAI Codex CLI

Add to `~/.codex/config.toml` (user-level) or `.codex/config.toml` (project-level):

```toml
[mcp_servers.k8s]
command = "k8s-mcp"
args = ["--transport", "stdio"]
```

### Gemini CLI

Add to `~/.gemini/settings.json` (user-level) or `.gemini/settings.json` (project-level):

```json
{
  "mcpServers": {
    "k8s": {
      "command": "k8s-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

### Opencode

Add to `opencode.json` in your project root:

```json
{
  "mcp": {
    "k8s": {
      "type": "local",
      "command": ["k8s-mcp", "--transport", "stdio"]
    }
  }
}
```

For all stdio configurations above, the server starts automatically when your MCP client connects — no manual commands needed.

<details>
<summary><strong>Using Poetry instead of a global install?</strong></summary>

Replace `"command": "k8s-mcp"` with `"command": "poetry"` and set args to `["run", "k8s-mcp", "--transport", "stdio"]`. You must also add `"cwd": "/absolute/path/to/k8s-mcp"` so Poetry can find the project.

</details>

### Other MCP clients

The server supports three transport modes:

```bash
# stdio (for local MCP clients like Claude Code)
k8s-mcp --transport stdio

# Streamable HTTP (for remote/networked clients)
k8s-mcp --transport streamable-http

# SSE (Server-Sent Events)
k8s-mcp --transport sse
```

For HTTP transports, configure bind address and port via environment variables:

```bash
export K8S_MCP_HOST=0.0.0.0   # default: localhost
export K8S_MCP_PORT=8000       # default: 8000
```

## Tools

All operations are exposed as MCP tools — you interact with them conversationally through your AI assistant.

### Cluster Context
| Tool | Description |
|------|-------------|
| `get_contexts` | List available kubeconfig contexts |
| `get_current_context` | Show the active context, cluster, and user |

### Namespaces
| Tool | Description |
|------|-------------|
| `list_namespaces` | List all namespaces in the cluster |

### Pods
| Tool | Description |
|------|-------------|
| `list_pods` | List pods (by namespace, label, or all namespaces) |
| `get_pod` | Get detailed pod information |
| `get_pod_logs` | Fetch container logs (with tail, previous container support) |
| `delete_pod` | Delete a pod (with configurable grace period) |
| `diagnose_pod` | One-shot diagnostics — combines status, conditions, events, and logs from failing containers |
| `exec_command` | Execute a command inside a running container (e.g., `curl`, `env`, `nslookup`) |

### Deployments
| Tool | Description |
|------|-------------|
| `list_deployments` | List deployments (by namespace, label, or all namespaces) |
| `get_deployment` | Get detailed deployment information |
| `scale_deployment` | Scale a deployment to N replicas |
| `restart_deployment` | Rolling restart (equivalent to `kubectl rollout restart`) |
| `get_rollout_status` | Check if a rollout is complete, in progress, or stuck |

### Services
| Tool | Description |
|------|-------------|
| `list_services` | List services (by namespace, label, or all namespaces) |
| `get_service` | Get detailed service information |

### ConfigMaps
| Tool | Description |
|------|-------------|
| `list_configmaps` | List ConfigMaps (by namespace, label, or all namespaces) |
| `get_configmap` | Get a ConfigMap's metadata and data contents |

### Secrets
| Tool | Description |
|------|-------------|
| `list_secrets` | List Secrets with type and key counts |
| `get_secret` | Get Secret metadata and key names; optionally decode values with masking |

### ServiceAccounts
| Tool | Description |
|------|-------------|
| `list_service_accounts` | List ServiceAccounts (by namespace, label, or all namespaces) |
| `get_service_account` | Get ServiceAccount details including secrets and automount config |

### RBAC (Roles & Bindings)
| Tool | Description |
|------|-------------|
| `list_roles` | List Roles (by namespace or all); optionally include ClusterRoles |
| `get_role` | Get Role or ClusterRole details including permission rules |
| `list_role_bindings` | List RoleBindings (by namespace or all); optionally include ClusterRoleBindings |
| `get_role_binding` | Get RoleBinding or ClusterRoleBinding details including subjects and role reference |

### Nodes
| Tool | Description |
|------|-------------|
| `list_nodes` | List cluster nodes with status and roles |
| `get_node` | Get detailed node information |

### Events
| Tool | Description |
|------|-------------|
| `list_events` | List events, optionally filtered by resource name |

### Jobs
| Tool | Description |
|------|-------------|
| `list_jobs` | List jobs with completion status and duration |

### Ingresses
| Tool | Description |
|------|-------------|
| `list_ingresses` | List Ingresses with hosts, class, and TLS info |
| `get_ingress` | Get detailed Ingress information including routing rules |

### Generic Operations
| Tool | Description |
|------|-------------|
| `apply_manifest` | Apply YAML manifests (create or update, supports multi-document) |
| `apply_kustomize` | Render and apply a Kustomize directory (equivalent to `kubectl apply -k`) |
| `delete_resource` | Delete any resource by type and name (supports abbreviations like `po`, `svc`, `deploy`) |
| `describe_resource` | Describe any resource — combines spec/status with related events (like `kubectl describe`) |
| `get_resource_yaml` | Export a live resource as clean YAML (for config drift detection) |

### Resource Metrics
| Tool | Description |
|------|-------------|
| `top_pods` | Show CPU/memory usage per pod (requires metrics-server) |
| `top_nodes` | Show CPU/memory usage per node with capacity percentages |

### Readiness
| Tool | Description |
|------|-------------|
| `wait_for_ready` | Poll a pod or deployment until ready or timeout (enables autonomous deploy loops) |

### Deployment Generation
| Tool | Description |
|------|-------------|
| `generate_deploy_manifests` | Generate Kubernetes manifests for deploying k8s-mcp itself to a cluster |

## Sample Use Cases

### Check cluster status
Open a new conversation as follows:
> *"Please check the status of my namespace: xxxxx"*

The assistant will list pods, deployments, services, and events in the namespace, surfacing any issues it finds.

![Sample usage of k8s-mcp](images/sample_usage_k8s_mcp.jpg)

### Deploy an application
Open a new conversation as follows:
> *"Please deploy the app/server in this repo to a k8s cluster for me. Make a plan first, then implement it."*

The assistant will plan the deployment end-to-end — confirming the target cluster, namespace, image registry, tag, and image pull secret — then generate the manifests and apply them to the cluster for you.

## License

MIT
