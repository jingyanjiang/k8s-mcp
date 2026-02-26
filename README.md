# k8s-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes Kubernetes operations as tools for AI assistants. It lets AI agents like Claude directly query and manage your Kubernetes cluster through natural language.

## Why use this?

AI assistants without k8s-mcp can only suggest `kubectl` commands for you to copy-paste. With k8s-mcp, they can:

- **Directly interact with your cluster** — list pods, read logs, check deployment status, and diagnose issues in real time
- **React to live state** — see actual pod statuses, event logs, and resource details instead of guessing
- **Take actions on your behalf** — scale deployments, restart workloads, apply manifests, and delete resources (with your approval)
- **Chain operations intelligently** — e.g., notice a pod is crash-looping, pull its logs, check events, and suggest a fix — all in one conversation

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

# Create a virtual environment (or activate your own)
conda create -n k8s-mcp python=3.10 -y && conda activate k8s-mcp
```

Install with your preferred package manager:

```bash
# Option 1: Poetry (recommended)
poetry install

# Option 2: uv
uv pip install -e .

# Option 3: pip
pip install -e .
```

## Configuration

### Claude Code / Claude Desktop

Copy the example config and update the path:

```bash
cp .mcp.json.example .mcp.json
```

Edit `.mcp.json` to set the correct `cwd` path:

```json
{
  "mcpServers": {
    "k8s": {
      "type": "stdio",
      "command": "poetry",
      "args": ["run", "k8s-mcp", "--transport", "stdio"],
      "cwd": "/absolute/path/to/k8s-mcp"
    }
  }
}
```

### OpenAI Codex CLI

Add to `~/.codex/config.toml` (user-level) or `.codex/config.toml` (project-level):

```toml
[mcp_servers.k8s]
command = "poetry"
args = ["run", "k8s-mcp", "--transport", "stdio"]
cwd = "/absolute/path/to/k8s-mcp"
```

### Gemini CLI

Add to `~/.gemini/settings.json` (user-level) or `.gemini/settings.json` (project-level):

```json
{
  "mcpServers": {
    "k8s": {
      "command": "poetry",
      "args": ["run", "k8s-mcp", "--transport", "stdio"],
      "cwd": "/absolute/path/to/k8s-mcp"
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
      "command": ["poetry", "run", "k8s-mcp", "--transport", "stdio"]
    }
  }
}
```

> **Note:** Opencode combines the executable and arguments into a single `command` array and does not support a `cwd` field. Run `opencode` from the k8s-mcp directory, or use an absolute path to the poetry binary.

> **Tip:** If you installed with `uv` or `pip` instead of Poetry, replace `poetry run k8s-mcp` with just `k8s-mcp` in all configurations above (the command is installed into your environment's PATH).

For all stdio configurations above, the server starts automatically when your MCP client connects — no manual commands needed.

### Other MCP clients

The server supports three transport modes:

```bash
# stdio (for local MCP clients like Claude Code)
poetry run k8s-mcp --transport stdio

# Streamable HTTP (for remote/networked clients)
poetry run k8s-mcp --transport streamable-http

# SSE (Server-Sent Events)
poetry run k8s-mcp --transport sse
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

### Deployments
| Tool | Description |
|------|-------------|
| `list_deployments` | List deployments (by namespace, label, or all namespaces) |
| `get_deployment` | Get detailed deployment information |
| `scale_deployment` | Scale a deployment to N replicas |
| `restart_deployment` | Rolling restart (equivalent to `kubectl rollout restart`) |

### Services
| Tool | Description |
|------|-------------|
| `list_services` | List services (by namespace, label, or all namespaces) |
| `get_service` | Get detailed service information |

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

### Generic Operations
| Tool | Description |
|------|-------------|
| `apply_manifest` | Apply YAML manifests (create or update, supports multi-document) |
| `delete_resource` | Delete any resource by type and name (supports abbreviations like `po`, `svc`, `deploy`) |

### Deployment Generation
| Tool | Description |
|------|-------------|
| `generate_deploy_manifests` | Generate Kubernetes manifests for deploying k8s-mcp itself to a cluster |

## Sample Use Cases

### Checking cluster status

> *"Please check the status of my namespace: askalcf"*

The assistant will list pods, deployments, services, and events in the namespace, surfacing any issues it finds.

![Sample usage of k8s-mcp](images/sample_usage_k8s_mcp.png)

### Deploying an application

> *"Please deploy the app/server in this repo to a k8s cluster for me. Make a plan first, then implement it."*

The assistant will plan the deployment end-to-end — confirming the target cluster, namespace, image registry, tag, and image pull secret — then generate the manifests and apply them to the cluster for you.

## License

MIT
