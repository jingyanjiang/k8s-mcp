"""Kubernetes MCP Server — expose kubectl operations as MCP tools.

Connects to Kubernetes automatically:
  - Local: uses ~/.kube/config
  - In-cluster: uses mounted ServiceAccount token

Environment variables (for HTTP transports):
  K8S_MCP_TRANSPORT  Transport protocol (default: streamable-http)
  K8S_MCP_HOST       Bind address (default: localhost)
  K8S_MCP_PORT       Bind port (default: 8000)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config, dynamic
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream as k8s_stream
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# K8s client management
# ---------------------------------------------------------------------------

_k8s_configured = False


def _ensure_k8s() -> None:
    """Load Kubernetes configuration on first use (lazy init)."""
    global _k8s_configured
    if _k8s_configured:
        return
    try:
        config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Using kubeconfig from ~/.kube/config")
    _k8s_configured = True


def _core() -> client.CoreV1Api:
    _ensure_k8s()
    return client.CoreV1Api()


def _apps() -> client.AppsV1Api:
    _ensure_k8s()
    return client.AppsV1Api()


def _batch() -> client.BatchV1Api:
    _ensure_k8s()
    return client.BatchV1Api()


def _dyn() -> dynamic.DynamicClient:
    _ensure_k8s()
    return dynamic.DynamicClient(ApiClient())


def _networking() -> client.NetworkingV1Api:
    _ensure_k8s()
    return client.NetworkingV1Api()


def _custom() -> client.CustomObjectsApi:
    _ensure_k8s()
    return client.CustomObjectsApi()


def _rbac() -> client.RbacAuthorizationV1Api:
    _ensure_k8s()
    return client.RbacAuthorizationV1Api()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age(ts: datetime | None) -> str:
    """Format a timestamp as a human-readable age (e.g. '3d', '5h', '12m')."""
    if not ts:
        return "<unknown>"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    days, seconds = delta.days, delta.seconds
    if days > 0:
        return f"{days}d"
    hours = seconds // 3600
    if hours > 0:
        return f"{hours}h"
    return f"{seconds // 60}m"


def _clean(obj: Any) -> dict:
    """Convert a K8s API object to dict, stripping managed_fields for readability."""
    d = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
    meta = d.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managed_fields", None)
    return d


# ---------------------------------------------------------------------------
# Resource type mapping (for generic operations)
# ---------------------------------------------------------------------------

_RESOURCE_MAP: dict[str, tuple[str, str]] = {
    # Core v1
    "pod": ("v1", "Pod"), "pods": ("v1", "Pod"), "po": ("v1", "Pod"),
    "service": ("v1", "Service"), "services": ("v1", "Service"), "svc": ("v1", "Service"),
    "configmap": ("v1", "ConfigMap"), "configmaps": ("v1", "ConfigMap"), "cm": ("v1", "ConfigMap"),
    "secret": ("v1", "Secret"), "secrets": ("v1", "Secret"),
    "namespace": ("v1", "Namespace"), "namespaces": ("v1", "Namespace"), "ns": ("v1", "Namespace"),
    "node": ("v1", "Node"), "nodes": ("v1", "Node"), "no": ("v1", "Node"),
    "persistentvolumeclaim": ("v1", "PersistentVolumeClaim"), "pvc": ("v1", "PersistentVolumeClaim"),
    "serviceaccount": ("v1", "ServiceAccount"), "serviceaccounts": ("v1", "ServiceAccount"), "sa": ("v1", "ServiceAccount"),
    # Apps v1
    "deployment": ("apps/v1", "Deployment"), "deployments": ("apps/v1", "Deployment"), "deploy": ("apps/v1", "Deployment"),
    "statefulset": ("apps/v1", "StatefulSet"), "statefulsets": ("apps/v1", "StatefulSet"), "sts": ("apps/v1", "StatefulSet"),
    "daemonset": ("apps/v1", "DaemonSet"), "daemonsets": ("apps/v1", "DaemonSet"), "ds": ("apps/v1", "DaemonSet"),
    "replicaset": ("apps/v1", "ReplicaSet"), "replicasets": ("apps/v1", "ReplicaSet"), "rs": ("apps/v1", "ReplicaSet"),
    # Batch v1
    "job": ("batch/v1", "Job"), "jobs": ("batch/v1", "Job"),
    "cronjob": ("batch/v1", "CronJob"), "cronjobs": ("batch/v1", "CronJob"), "cj": ("batch/v1", "CronJob"),
    # Networking
    "ingress": ("networking.k8s.io/v1", "Ingress"), "ingresses": ("networking.k8s.io/v1", "Ingress"), "ing": ("networking.k8s.io/v1", "Ingress"),
    # RBAC v1
    "role": ("rbac.authorization.k8s.io/v1", "Role"), "roles": ("rbac.authorization.k8s.io/v1", "Role"),
    "rolebinding": ("rbac.authorization.k8s.io/v1", "RoleBinding"), "rolebindings": ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    "clusterrole": ("rbac.authorization.k8s.io/v1", "ClusterRole"), "clusterroles": ("rbac.authorization.k8s.io/v1", "ClusterRole"), "cr": ("rbac.authorization.k8s.io/v1", "ClusterRole"),
    "clusterrolebinding": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"), "clusterrolebindings": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"), "crb": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
}

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

_host = os.getenv("K8S_MCP_HOST", "localhost")
_port = int(os.getenv("K8S_MCP_PORT", "8000"))

mcp = FastMCP(
    "k8s-mcp",
    instructions=(
        "Kubernetes MCP server — run kubectl-like operations from AI assistants.\n\n"
        "Behavioral rules:\n"
        "- Before any operation, confirm the target namespace with the user if they "
        "did not explicitly provide one. Do NOT silently default to 'default'.\n"
        "- For destructive operations (delete, scale to 0, restart), confirm the "
        "resource name, namespace, and cluster with the user before proceeding.\n"
        "- If the user is unsure about a value (namespace, resource name, etc.), "
        "use list/get tools (list_pods, list_deployments, list_namespaces, "
        "get_current_context) to help them discover the correct value rather than "
        "guessing.\n"
        "- Use get_current_context to verify the active cluster before making changes."
    ),
    host=_host,
    port=_port,
)


# ---- Context tools --------------------------------------------------------


@mcp.tool()
def get_contexts() -> str:
    """List available kubeconfig contexts and indicate the active one."""
    try:
        contexts, active = config.list_kube_config_contexts()
    except config.ConfigException:
        return "Running in-cluster mode — kubeconfig contexts are not available."
    lines = [f"  {'NAME':<40} {'CLUSTER':<30} NAMESPACE"]
    for ctx in contexts:
        marker = "*" if ctx["name"] == active["name"] else " "
        cluster = ctx["context"].get("cluster", "")
        ns = ctx["context"].get("namespace", "")
        lines.append(f"{marker} {ctx['name']:<40} {cluster:<30} {ns}")
    return "\n".join(lines)


@mcp.tool()
def get_current_context() -> str:
    """Get the currently active kubeconfig context with cluster and user details."""
    try:
        _, active = config.list_kube_config_contexts()
        return json.dumps(active, indent=2, default=str)
    except config.ConfigException:
        return "Running in-cluster mode — kubeconfig contexts are not available."


# ---- Namespace tools ------------------------------------------------------


@mcp.tool()
def list_namespaces() -> str:
    """List all namespaces in the cluster."""
    try:
        items = _core().list_namespace().items
        lines = [f"{'NAME':<40} {'STATUS':<12} AGE"]
        for ns in items:
            lines.append(
                f"{ns.metadata.name:<40} {ns.status.phase:<12} "
                f"{_age(ns.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing namespaces: {e.reason} (HTTP {e.status})"


# ---- Pod tools ------------------------------------------------------------


@mcp.tool()
def list_pods(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List pods in a namespace or across all namespaces.

    Args:
        namespace: Target namespace (ignored when all_namespaces is True).
        label_selector: Label filter, e.g. 'app=nginx'.
        all_namespaces: List pods in all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        pods = (
            v1.list_pod_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_pod(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<50} {'READY':<8} {'STATUS':<15} {'RESTARTS':<10} AGE"]
        for p in pods:
            total = len(p.spec.containers)
            statuses = p.status.container_statuses or []
            ready = sum(1 for c in statuses if c.ready)
            restarts = sum(c.restart_count for c in statuses)
            prefix = f"{p.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{p.metadata.name:<50} {ready}/{total:<6} "
                f"{p.status.phase:<15} {restarts:<10} "
                f"{_age(p.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing pods: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_pod(name: str, namespace: str = "default") -> str:
    """Get detailed information about a specific pod.

    Args:
        name: Pod name.
        namespace: Pod namespace.
    """
    try:
        pod = _core().read_namespaced_pod(name, namespace)
        return json.dumps(_clean(pod), indent=2, default=str)
    except ApiException as e:
        return f"Error getting pod '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_pod_logs(
    name: str,
    namespace: str = "default",
    container: str | None = None,
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """Get logs from a pod container.

    Args:
        name: Pod name.
        namespace: Pod namespace.
        container: Container name (required for multi-container pods).
        tail_lines: Number of lines from the end of the log.
        previous: Return logs from the previous terminated container instance.
    """
    try:
        kw: dict[str, Any] = {"tail_lines": tail_lines, "previous": previous}
        if container:
            kw["container"] = container
        return _core().read_namespaced_pod_log(name, namespace, **kw)
    except ApiException as e:
        return f"Error getting logs for pod '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def delete_pod(name: str, namespace: str = "default", grace_period_seconds: int = 30) -> str:
    """Delete a pod.

    Args:
        name: Pod name.
        namespace: Pod namespace.
        grace_period_seconds: Grace period for termination.
    """
    try:
        _core().delete_namespaced_pod(
            name, namespace, grace_period_seconds=grace_period_seconds,
        )
        return f"pod/{name} deleted from namespace '{namespace}'"
    except ApiException as e:
        return f"Error deleting pod '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def diagnose_pod(name: str, namespace: str = "default", tail_lines: int = 50) -> str:
    """Diagnose a pod by combining its status, conditions, container states, events, and logs.

    Returns a unified diagnostic report — equivalent to running kubectl describe
    and kubectl logs together. Ideal for quickly understanding why a pod is failing.

    Args:
        name: Pod name.
        namespace: Pod namespace.
        tail_lines: Number of log lines to include from failing containers.
    """
    v1 = _core()
    sections: list[str] = []

    # --- Pod status & conditions ---
    try:
        pod = v1.read_namespaced_pod(name, namespace)
    except ApiException as e:
        return f"Error getting pod '{name}': {e.reason} (HTTP {e.status})"

    sections.append(f"=== Pod: {name} (namespace: {namespace}) ===")
    sections.append(f"Phase: {pod.status.phase}")
    if pod.status.reason:
        sections.append(f"Reason: {pod.status.reason}")
    if pod.status.message:
        sections.append(f"Message: {pod.status.message}")
    sections.append(f"Node: {pod.spec.node_name or '<pending>'}")
    sections.append(f"Age: {_age(pod.metadata.creation_timestamp)}")

    # Conditions
    if pod.status.conditions:
        sections.append("\n--- Conditions ---")
        for c in pod.status.conditions:
            line = f"  {c.type}: {c.status}"
            if c.reason:
                line += f" ({c.reason})"
            if c.message:
                line += f" — {c.message}"
            sections.append(line)

    # Container statuses
    failing_containers: list[str] = []
    for label, statuses in [
        ("Init Containers", pod.status.init_container_statuses),
        ("Containers", pod.status.container_statuses),
    ]:
        if not statuses:
            continue
        sections.append(f"\n--- {label} ---")
        for cs in statuses:
            state_info = "Unknown"
            if cs.state:
                if cs.state.running:
                    state_info = f"Running (since {_age(cs.state.running.started_at)})"
                elif cs.state.waiting:
                    state_info = f"Waiting: {cs.state.waiting.reason or 'unknown'}"
                    if cs.state.waiting.message:
                        state_info += f" — {cs.state.waiting.message}"
                    failing_containers.append(cs.name)
                elif cs.state.terminated:
                    t = cs.state.terminated
                    state_info = f"Terminated: {t.reason or 'unknown'} (exit {t.exit_code})"
                    if t.message:
                        state_info += f" — {t.message}"
                    if t.exit_code != 0:
                        failing_containers.append(cs.name)
            ready = "yes" if cs.ready else "no"
            sections.append(
                f"  {cs.name}: {state_info} | ready={ready} restarts={cs.restart_count}"
            )

    # --- Events ---
    try:
        events = v1.list_namespaced_event(
            namespace, field_selector=f"involvedObject.name={name}",
        ).items
        if events:
            sections.append("\n--- Recent Events ---")
            events.sort(key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc))
            for ev in events[-15:]:
                last = _age(ev.last_timestamp or ev.event_time)
                count = f"(x{ev.count})" if ev.count and ev.count > 1 else ""
                sections.append(f"  {last:<8} {ev.type:<8} {ev.reason:<22} {ev.message} {count}")
    except ApiException:
        sections.append("\n--- Events: <error fetching> ---")

    # --- Logs from failing containers ---
    containers_to_log = failing_containers or [
        cs.name for cs in (pod.status.container_statuses or [])
        if cs.restart_count > 0
    ]
    if not containers_to_log and pod.status.phase in ("Failed", "Unknown"):
        containers_to_log = [c.name for c in pod.spec.containers]

    for cname in containers_to_log:
        sections.append(f"\n--- Logs: {cname} (last {tail_lines} lines) ---")
        try:
            logs = v1.read_namespaced_pod_log(
                name, namespace, container=cname, tail_lines=tail_lines,
            )
            sections.append(logs or "<empty>")
        except ApiException as e:
            sections.append(f"  <error: {e.reason}>")
        # Also try previous container logs if there were restarts
        for cs in (pod.status.container_statuses or []):
            if cs.name == cname and cs.restart_count > 0:
                sections.append(f"\n--- Previous Logs: {cname} ---")
                try:
                    prev = v1.read_namespaced_pod_log(
                        name, namespace, container=cname,
                        tail_lines=tail_lines, previous=True,
                    )
                    sections.append(prev or "<empty>")
                except ApiException:
                    sections.append("  <no previous logs>")

    return "\n".join(sections)


@mcp.tool()
def exec_command(
    name: str,
    command: list[str],
    namespace: str = "default",
    container: str | None = None,
    timeout: int = 30,
) -> str:
    """Execute a command inside a running pod container.

    Useful for debugging connectivity (curl, nslookup), inspecting environment
    variables, checking file contents, or running diagnostic commands without
    leaving the AI assistant.

    Args:
        name: Pod name.
        command: Command and arguments as a list (e.g. ['curl', '-s', 'http://svc:8080/health']).
        namespace: Pod namespace.
        container: Container name (required for multi-container pods).
        timeout: Execution timeout in seconds.
    """
    try:
        kw: dict[str, Any] = {
            "command": command,
            "stderr": True,
            "stdout": True,
            "stdin": False,
            "tty": False,
            "_request_timeout": timeout,
        }
        if container:
            kw["container"] = container
        resp = k8s_stream(
            _core().connect_get_namespaced_pod_exec,
            name,
            namespace,
            **kw,
        )
        return resp if resp else "<no output>"
    except ApiException as e:
        return f"Error executing command in pod '{name}': {e.reason} (HTTP {e.status})"
    except Exception as e:
        return f"Error executing command in pod '{name}': {e}"


# ---- Deployment tools -----------------------------------------------------


@mcp.tool()
def list_deployments(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List deployments in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        apps = _apps()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        deps = (
            apps.list_deployment_for_all_namespaces(**kw).items
            if all_namespaces
            else apps.list_namespaced_deployment(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<40} {'READY':<10} {'UP-TO-DATE':<12} {'AVAILABLE':<12} AGE"]
        for d in deps:
            ready = f"{d.status.ready_replicas or 0}/{d.spec.replicas or 0}"
            prefix = f"{d.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{d.metadata.name:<40} {ready:<10} "
                f"{d.status.updated_replicas or 0:<12} "
                f"{d.status.available_replicas or 0:<12} "
                f"{_age(d.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing deployments: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_deployment(name: str, namespace: str = "default") -> str:
    """Get detailed information about a deployment.

    Args:
        name: Deployment name.
        namespace: Deployment namespace.
    """
    try:
        dep = _apps().read_namespaced_deployment(name, namespace)
        return json.dumps(_clean(dep), indent=2, default=str)
    except ApiException as e:
        return f"Error getting deployment '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def scale_deployment(name: str, replicas: int, namespace: str = "default") -> str:
    """Scale a deployment to the specified replica count.

    Args:
        name: Deployment name.
        replicas: Desired number of replicas.
        namespace: Deployment namespace.
    """
    try:
        _apps().patch_namespaced_deployment_scale(
            name, namespace, body={"spec": {"replicas": replicas}},
        )
        return f"deployment/{name} scaled to {replicas} replicas"
    except ApiException as e:
        return f"Error scaling deployment '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def restart_deployment(name: str, namespace: str = "default") -> str:
    """Restart a deployment via rolling update (equivalent to kubectl rollout restart).

    Args:
        name: Deployment name.
        namespace: Deployment namespace.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        _apps().patch_namespaced_deployment(
            name,
            namespace,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now,
                            }
                        }
                    }
                }
            },
        )
        return f"deployment/{name} restarted"
    except ApiException as e:
        return f"Error restarting deployment '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_rollout_status(name: str, namespace: str = "default") -> str:
    """Check the rollout status of a deployment.

    Shows whether a rollout is complete, progressing, or stuck — equivalent
    to 'kubectl rollout status deployment/<name>'.

    Args:
        name: Deployment name.
        namespace: Deployment namespace.
    """
    try:
        dep = _apps().read_namespaced_deployment(name, namespace)
    except ApiException as e:
        return f"Error getting deployment '{name}': {e.reason} (HTTP {e.status})"

    spec_replicas = dep.spec.replicas or 1
    status = dep.status
    updated = status.updated_replicas or 0
    ready = status.ready_replicas or 0
    available = status.available_replicas or 0
    unavailable = status.unavailable_replicas or 0
    generation = dep.metadata.generation
    observed = status.observed_generation or 0

    lines: list[str] = [f"=== Rollout: {name} (namespace: {namespace}) ==="]
    lines.append(
        f"Replicas: {spec_replicas} desired | {updated} updated | "
        f"{ready} ready | {available} available | {unavailable} unavailable"
    )
    lines.append(f"Generation: {generation} (observed: {observed})")

    # Determine rollout state from conditions
    conditions = {c.type: c for c in (status.conditions or [])}
    progressing = conditions.get("Progressing")

    if progressing and progressing.reason == "NewReplicaSetAvailable" and ready >= spec_replicas:
        verdict = "COMPLETE — all replicas updated and available"
    elif progressing and progressing.reason == "ProgressDeadlineExceeded":
        verdict = f"STUCK — {progressing.message}"
    elif updated < spec_replicas or ready < updated:
        verdict = f"IN PROGRESS — {updated}/{spec_replicas} updated, {ready} ready"
    elif generation > observed:
        verdict = "PENDING — waiting for controller to observe new generation"
    else:
        verdict = "COMPLETE — all replicas updated and available"

    lines.append(f"\nStatus: {verdict}")

    # Show conditions
    if status.conditions:
        lines.append("\n--- Conditions ---")
        for c in status.conditions:
            line = f"  {c.type}: {c.status}"
            if c.reason:
                line += f" ({c.reason})"
            if c.message:
                line += f" — {c.message}"
            lines.append(line)

    return "\n".join(lines)


# ---- Service tools --------------------------------------------------------


@mcp.tool()
def list_services(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List services in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        svcs = (
            v1.list_service_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_service(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<40} {'TYPE':<15} {'CLUSTER-IP':<20} {'PORTS':<30} AGE"]
        for s in svcs:
            ports = ", ".join(
                f"{p.port}/{p.protocol}" + (f":{p.node_port}" if p.node_port else "")
                for p in (s.spec.ports or [])
            )
            prefix = f"{s.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{s.metadata.name:<40} {s.spec.type:<15} "
                f"{s.spec.cluster_ip or '<none>':<20} {ports:<30} "
                f"{_age(s.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing services: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_service(name: str, namespace: str = "default") -> str:
    """Get detailed information about a service.

    Args:
        name: Service name.
        namespace: Service namespace.
    """
    try:
        svc = _core().read_namespaced_service(name, namespace)
        return json.dumps(_clean(svc), indent=2, default=str)
    except ApiException as e:
        return f"Error getting service '{name}': {e.reason} (HTTP {e.status})"


# ---- ConfigMap tools ------------------------------------------------------


@mcp.tool()
def list_configmaps(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List ConfigMaps in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        cms = (
            v1.list_config_map_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_config_map(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<50} {'DATA':<8} AGE"]
        for cm in cms:
            data_count = len(cm.data or {})
            prefix = f"{cm.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{cm.metadata.name:<50} {data_count:<8} "
                f"{_age(cm.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing configmaps: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_configmap(name: str, namespace: str = "default") -> str:
    """Get a ConfigMap's metadata and data contents.

    Args:
        name: ConfigMap name.
        namespace: ConfigMap namespace.
    """
    try:
        cm = _core().read_namespaced_config_map(name, namespace)
        result: dict[str, Any] = {
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "labels": cm.metadata.labels,
            "annotations": cm.metadata.annotations,
            "age": _age(cm.metadata.creation_timestamp),
            "data": cm.data or {},
        }
        if cm.binary_data:
            result["binary_data_keys"] = list(cm.binary_data.keys())
        return json.dumps(result, indent=2, default=str)
    except ApiException as e:
        return f"Error getting configmap '{name}': {e.reason} (HTTP {e.status})"


# ---- Secret tools ---------------------------------------------------------


@mcp.tool()
def list_secrets(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List Secrets in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        secrets = (
            v1.list_secret_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_secret(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<50} {'TYPE':<35} {'DATA':<8} AGE"]
        for s in secrets:
            data_count = len(s.data or {})
            prefix = f"{s.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{s.metadata.name:<50} {s.type:<35} {data_count:<8} "
                f"{_age(s.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing secrets: {e.reason} (HTTP {e.status})"


def _mask_value(val: str, visible: int = 4) -> str:
    """Decode a base64 secret value and partially mask it for safety."""
    try:
        decoded = base64.b64decode(val).decode("utf-8", errors="replace")
    except Exception:
        return "<binary data>"
    if len(decoded) <= visible:
        return "*" * len(decoded)
    return decoded[:visible] + "*" * (len(decoded) - visible)


@mcp.tool()
def get_secret(name: str, namespace: str = "default", decode: bool = False) -> str:
    """Get a Secret's metadata and key names. Optionally decode values (masked).

    By default only shows key names for safety. Use decode=True to see
    base64-decoded values with partial masking (first 4 chars visible).

    Args:
        name: Secret name.
        namespace: Secret namespace.
        decode: Decode and show masked values (default: False, keys only).
    """
    try:
        secret = _core().read_namespaced_secret(name, namespace)
        result: dict[str, Any] = {
            "name": secret.metadata.name,
            "namespace": secret.metadata.namespace,
            "type": secret.type,
            "labels": secret.metadata.labels,
            "annotations": secret.metadata.annotations,
            "age": _age(secret.metadata.creation_timestamp),
        }
        data = secret.data or {}
        if decode:
            result["data"] = {k: _mask_value(v) for k, v in data.items()}
        else:
            result["data_keys"] = list(data.keys())
            result["data_count"] = len(data)
        return json.dumps(result, indent=2, default=str)
    except ApiException as e:
        return f"Error getting secret '{name}': {e.reason} (HTTP {e.status})"


# ---- ServiceAccount tools -------------------------------------------------


@mcp.tool()
def list_service_accounts(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List ServiceAccounts in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        sas = (
            v1.list_service_account_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_service_account(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<40} {'SECRETS':<10} AGE"]
        for sa in sas:
            secrets_count = len(sa.secrets or [])
            prefix = f"{sa.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{sa.metadata.name:<40} {secrets_count:<10} "
                f"{_age(sa.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing service accounts: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_service_account(name: str, namespace: str = "default") -> str:
    """Get detailed information about a ServiceAccount.

    Args:
        name: ServiceAccount name.
        namespace: ServiceAccount namespace.
    """
    try:
        sa = _core().read_namespaced_service_account(name, namespace)
        result: dict[str, Any] = {
            "name": sa.metadata.name,
            "namespace": sa.metadata.namespace,
            "labels": sa.metadata.labels,
            "annotations": sa.metadata.annotations,
            "age": _age(sa.metadata.creation_timestamp),
            "automount_token": sa.automount_service_account_token,
            "secrets": [s.name for s in (sa.secrets or [])],
        }
        return json.dumps(result, indent=2, default=str)
    except ApiException as e:
        return f"Error getting service account '{name}': {e.reason} (HTTP {e.status})"


# ---- RBAC tools -----------------------------------------------------------


@mcp.tool()
def list_roles(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
    include_cluster_roles: bool = False,
) -> str:
    """List Roles in a namespace or across all namespaces, optionally including ClusterRoles.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
        include_cluster_roles: Also list ClusterRoles (cluster-scoped).
    """
    try:
        rbac = _rbac()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        roles = (
            rbac.list_role_for_all_namespaces(**kw).items
            if all_namespaces
            else rbac.list_namespaced_role(namespace, **kw).items
        )
        show_ns = all_namespaces or include_cluster_roles
        header = f"{'NAMESPACE':<20} " if show_ns else ""
        lines = [f"{header}{'NAME':<50} AGE"]
        for r in roles:
            prefix = f"{r.metadata.namespace:<20} " if show_ns else ""
            lines.append(f"{prefix}{r.metadata.name:<50} {_age(r.metadata.creation_timestamp)}")
        if include_cluster_roles:
            cluster_roles = rbac.list_cluster_role(**kw).items
            for cr in cluster_roles:
                lines.append(f"{'<cluster>':<20} {cr.metadata.name:<50} {_age(cr.metadata.creation_timestamp)}")
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing roles: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_role(name: str, namespace: str = "default", cluster: bool = False) -> str:
    """Get detailed information about a Role or ClusterRole.

    Args:
        name: Role name.
        namespace: Role namespace (ignored when cluster=True).
        cluster: If True, get a ClusterRole instead of a namespaced Role.
    """
    try:
        rbac = _rbac()
        role = (
            rbac.read_cluster_role(name)
            if cluster
            else rbac.read_namespaced_role(name, namespace)
        )
        rules = []
        for rule in role.rules or []:
            rules.append({
                "api_groups": rule.api_groups,
                "resources": rule.resources,
                "verbs": rule.verbs,
                "resource_names": rule.resource_names,
            })
        result: dict[str, Any] = {
            "name": role.metadata.name,
            "kind": "ClusterRole" if cluster else "Role",
            "labels": role.metadata.labels,
            "annotations": role.metadata.annotations,
            "age": _age(role.metadata.creation_timestamp),
            "rules": rules,
        }
        if not cluster:
            result["namespace"] = role.metadata.namespace
        return json.dumps(result, indent=2, default=str)
    except ApiException as e:
        kind = "cluster role" if cluster else "role"
        return f"Error getting {kind} '{name}': {e.reason} (HTTP {e.status})"


@mcp.tool()
def list_role_bindings(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
    include_cluster_role_bindings: bool = False,
) -> str:
    """List RoleBindings in a namespace or across all namespaces, optionally including ClusterRoleBindings.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
        include_cluster_role_bindings: Also list ClusterRoleBindings (cluster-scoped).
    """
    try:
        rbac = _rbac()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        bindings = (
            rbac.list_role_binding_for_all_namespaces(**kw).items
            if all_namespaces
            else rbac.list_namespaced_role_binding(namespace, **kw).items
        )
        show_ns = all_namespaces or include_cluster_role_bindings
        header = f"{'NAMESPACE':<20} " if show_ns else ""
        lines = [f"{header}{'NAME':<40} {'ROLE-REF':<40} AGE"]
        for rb in bindings:
            role_ref = f"{rb.role_ref.kind}/{rb.role_ref.name}"
            prefix = f"{rb.metadata.namespace:<20} " if show_ns else ""
            lines.append(
                f"{prefix}{rb.metadata.name:<40} {role_ref:<40} "
                f"{_age(rb.metadata.creation_timestamp)}"
            )
        if include_cluster_role_bindings:
            crbs = rbac.list_cluster_role_binding(**kw).items
            for crb in crbs:
                role_ref = f"{crb.role_ref.kind}/{crb.role_ref.name}"
                lines.append(
                    f"{'<cluster>':<20} {crb.metadata.name:<40} {role_ref:<40} "
                    f"{_age(crb.metadata.creation_timestamp)}"
                )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing role bindings: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_role_binding(name: str, namespace: str = "default", cluster: bool = False) -> str:
    """Get detailed information about a RoleBinding or ClusterRoleBinding.

    Args:
        name: RoleBinding name.
        namespace: RoleBinding namespace (ignored when cluster=True).
        cluster: If True, get a ClusterRoleBinding instead of a namespaced RoleBinding.
    """
    try:
        rbac = _rbac()
        binding = (
            rbac.read_cluster_role_binding(name)
            if cluster
            else rbac.read_namespaced_role_binding(name, namespace)
        )
        subjects = []
        for s in binding.subjects or []:
            subj: dict[str, str] = {"kind": s.kind, "name": s.name}
            if s.namespace:
                subj["namespace"] = s.namespace
            if s.api_group:
                subj["api_group"] = s.api_group
            subjects.append(subj)
        result: dict[str, Any] = {
            "name": binding.metadata.name,
            "kind": "ClusterRoleBinding" if cluster else "RoleBinding",
            "labels": binding.metadata.labels,
            "annotations": binding.metadata.annotations,
            "age": _age(binding.metadata.creation_timestamp),
            "role_ref": {
                "kind": binding.role_ref.kind,
                "name": binding.role_ref.name,
                "api_group": binding.role_ref.api_group,
            },
            "subjects": subjects,
        }
        if not cluster:
            result["namespace"] = binding.metadata.namespace
        return json.dumps(result, indent=2, default=str)
    except ApiException as e:
        kind = "cluster role binding" if cluster else "role binding"
        return f"Error getting {kind} '{name}': {e.reason} (HTTP {e.status})"


# ---- Node tools -----------------------------------------------------------


@mcp.tool()
def list_nodes(label_selector: str = "") -> str:
    """List all cluster nodes.

    Args:
        label_selector: Label filter.
    """
    try:
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        nodes = _core().list_node(**kw).items
        lines = [f"{'NAME':<40} {'STATUS':<12} {'ROLES':<20} AGE"]
        for n in nodes:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            status = "Ready" if conds.get("Ready") == "True" else "NotReady"
            roles = ", ".join(
                k.removeprefix("node-role.kubernetes.io/")
                for k in (n.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ) or "<none>"
            lines.append(
                f"{n.metadata.name:<40} {status:<12} {roles:<20} "
                f"{_age(n.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing nodes: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_node(name: str) -> str:
    """Get detailed information about a node.

    Args:
        name: Node name.
    """
    try:
        node = _core().read_node(name)
        return json.dumps(_clean(node), indent=2, default=str)
    except ApiException as e:
        return f"Error getting node '{name}': {e.reason} (HTTP {e.status})"


# ---- Event tools ----------------------------------------------------------


@mcp.tool()
def list_events(
    namespace: str = "default",
    involved_object_name: str = "",
    all_namespaces: bool = False,
) -> str:
    """List cluster events, optionally filtered by involved resource.

    Args:
        namespace: Target namespace.
        involved_object_name: Filter events related to a specific resource name.
        all_namespaces: List across all namespaces.
    """
    try:
        v1 = _core()
        kw: dict[str, Any] = {}
        if involved_object_name:
            kw["field_selector"] = f"involvedObject.name={involved_object_name}"
        events = (
            v1.list_event_for_all_namespaces(**kw).items
            if all_namespaces
            else v1.list_namespaced_event(namespace, **kw).items
        )
        lines = [f"{'LAST SEEN':<12} {'TYPE':<10} {'REASON':<22} {'OBJECT':<40} MESSAGE"]
        for ev in events:
            last = _age(ev.last_timestamp or ev.event_time)
            obj = f"{ev.involved_object.kind}/{ev.involved_object.name}"
            lines.append(f"{last:<12} {ev.type:<10} {ev.reason:<22} {obj:<40} {ev.message}")
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing events: {e.reason} (HTTP {e.status})"


# ---- Describe tool --------------------------------------------------------


@mcp.tool()
def describe_resource(
    resource_type: str,
    name: str,
    namespace: str = "default",
) -> str:
    """Describe a Kubernetes resource — combines resource details with related events.

    Equivalent to 'kubectl describe'. Returns the resource spec/status plus
    recent events involving that resource.

    Args:
        resource_type: Resource type or abbreviation (e.g. 'pod', 'deploy', 'svc', 'cm').
        name: Resource name.
        namespace: Resource namespace.
    """
    key = resource_type.lower()
    mapping = _RESOURCE_MAP.get(key)
    if not mapping:
        supported = sorted({v[1] for v in _RESOURCE_MAP.values()})
        return f"Unknown resource type '{resource_type}'. Supported: {', '.join(supported)}"

    api_version, kind = mapping
    sections: list[str] = []

    # --- Fetch resource ---
    try:
        dyn = _dyn()
        resource = dyn.resources.get(api_version=api_version, kind=kind)
        obj = resource.get(name=name, namespace=namespace)
        obj_dict = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        meta = obj_dict.get("metadata", {})
        meta.pop("managedFields", None)
        meta.pop("managed_fields", None)
    except ApiException as e:
        return f"Error getting {kind}/{name}: {e.reason} (HTTP {e.status})"

    sections.append(f"=== {kind}: {name} (namespace: {namespace}) ===")

    # Key metadata
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    sections.append(f"Labels:      {json.dumps(labels, default=str) if labels else '<none>'}")
    sections.append(f"Annotations: {len(annotations)} keys")
    creation = meta.get("creationTimestamp") or meta.get("creation_timestamp")
    if creation:
        sections.append(f"Created:     {creation}")

    # Spec and Status as JSON
    spec = obj_dict.get("spec")
    status = obj_dict.get("status")
    if spec:
        sections.append("\n--- Spec ---")
        sections.append(json.dumps(spec, indent=2, default=str))
    if status:
        sections.append("\n--- Status ---")
        sections.append(json.dumps(status, indent=2, default=str))

    # --- Events ---
    try:
        events = _core().list_namespaced_event(
            namespace, field_selector=f"involvedObject.name={name}",
        ).items
        if events:
            sections.append("\n--- Events ---")
            events.sort(key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc))
            for ev in events[-20:]:
                last = _age(ev.last_timestamp or ev.event_time)
                count = f"(x{ev.count})" if ev.count and ev.count > 1 else ""
                sections.append(
                    f"  {last:<8} {ev.type:<8} {ev.reason:<22} {ev.message} {count}"
                )
        else:
            sections.append("\n--- Events: <none> ---")
    except ApiException:
        sections.append("\n--- Events: <error fetching> ---")

    return "\n".join(sections)


# ---- Job tools ------------------------------------------------------------


@mcp.tool()
def list_jobs(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List jobs in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        b = _batch()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        jobs = (
            b.list_job_for_all_namespaces(**kw).items
            if all_namespaces
            else b.list_namespaced_job(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<40} {'COMPLETIONS':<15} {'DURATION':<12} AGE"]
        for j in jobs:
            succeeded = j.status.succeeded or 0
            completions = j.spec.completions or 1
            duration = ""
            if j.status.completion_time and j.status.start_time:
                secs = int((j.status.completion_time - j.status.start_time).total_seconds())
                duration = f"{secs}s"
            prefix = f"{j.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{j.metadata.name:<40} {succeeded}/{completions:<13} "
                f"{duration:<12} {_age(j.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing jobs: {e.reason} (HTTP {e.status})"


# ---- Ingress tools --------------------------------------------------------


@mcp.tool()
def list_ingresses(
    namespace: str = "default",
    label_selector: str = "",
    all_namespaces: bool = False,
) -> str:
    """List Ingresses in a namespace or across all namespaces.

    Args:
        namespace: Target namespace.
        label_selector: Label filter.
        all_namespaces: List across all namespaces.
    """
    try:
        net = _networking()
        kw: dict[str, Any] = {}
        if label_selector:
            kw["label_selector"] = label_selector
        ingresses = (
            net.list_ingress_for_all_namespaces(**kw).items
            if all_namespaces
            else net.list_namespaced_ingress(namespace, **kw).items
        )
        header = f"{'NAMESPACE':<20} " if all_namespaces else ""
        lines = [f"{header}{'NAME':<40} {'CLASS':<15} {'HOSTS':<40} {'PORTS':<10} AGE"]
        for ing in ingresses:
            cls = ing.spec.ingress_class_name or "<none>"
            hosts = ", ".join(
                r.host or "*" for r in (ing.spec.rules or [])
            ) or "<none>"
            ports = "443" if ing.spec.tls else "80"
            prefix = f"{ing.metadata.namespace:<20} " if all_namespaces else ""
            lines.append(
                f"{prefix}{ing.metadata.name:<40} {cls:<15} {hosts:<40} "
                f"{ports:<10} {_age(ing.metadata.creation_timestamp)}"
            )
        return "\n".join(lines)
    except ApiException as e:
        return f"Error listing ingresses: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_ingress(name: str, namespace: str = "default") -> str:
    """Get detailed information about an Ingress, including rules and TLS config.

    Args:
        name: Ingress name.
        namespace: Ingress namespace.
    """
    try:
        ing = _networking().read_namespaced_ingress(name, namespace)
        return json.dumps(_clean(ing), indent=2, default=str)
    except ApiException as e:
        return f"Error getting ingress '{name}': {e.reason} (HTTP {e.status})"


# ---- Generic resource tools -----------------------------------------------


@mcp.tool()
def apply_manifest(manifest_yaml: str) -> str:
    """Apply a Kubernetes YAML manifest (create or update resources).

    Equivalent to 'kubectl apply'. Supports multi-document YAML separated by '---'.

    Args:
        manifest_yaml: YAML manifest string.
    """
    dyn = _dyn()
    results: list[str] = []
    for doc in yaml.safe_load_all(manifest_yaml):
        if not doc:
            continue
        api_version = doc.get("apiVersion", "")
        kind = doc.get("kind", "")
        name = doc.get("metadata", {}).get("name", "<unnamed>")
        ns = doc.get("metadata", {}).get("namespace")
        try:
            resource = dyn.resources.get(api_version=api_version, kind=kind)
            try:
                if ns:
                    resource.create(body=doc, namespace=ns)
                else:
                    resource.create(body=doc)
                results.append(f"{kind}/{name} created")
            except ApiException as create_err:
                if create_err.status == 409:
                    if ns:
                        resource.patch(
                            body=doc, name=name, namespace=ns,
                            content_type="application/merge-patch+json",
                        )
                    else:
                        resource.patch(
                            body=doc, name=name,
                            content_type="application/merge-patch+json",
                        )
                    results.append(f"{kind}/{name} configured")
                else:
                    results.append(f"{kind}/{name} error: {create_err.reason}")
        except Exception as exc:
            results.append(f"{kind}/{name} error: {exc}")
    return "\n".join(results)


@mcp.tool()
def apply_kustomize(directory: str) -> str:
    """Render and apply a Kustomize directory.

    Equivalent to 'kubectl apply -k <directory>'. Renders the kustomization
    locally using kubectl, then applies each resource via the Kubernetes API.

    Args:
        directory: Path to a directory containing a kustomization.yaml file.
    """
    kust_dir = Path(directory).expanduser().resolve()
    if not kust_dir.is_dir():
        return f"Error: directory '{directory}' does not exist"
    if not (kust_dir / "kustomization.yaml").exists() and not (kust_dir / "kustomization.yml").exists():
        return f"Error: no kustomization.yaml found in '{directory}'"
    try:
        result = subprocess.run(
            ["kubectl", "kustomize", str(kust_dir)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return f"Error rendering kustomize: {result.stderr.strip()}"
        return apply_manifest(result.stdout)
    except FileNotFoundError:
        return "Error: kubectl not found on PATH. kubectl is required for kustomize rendering."
    except subprocess.TimeoutExpired:
        return "Error: kubectl kustomize timed out after 30 seconds"


@mcp.tool()
def delete_resource(resource_type: str, name: str, namespace: str = "") -> str:
    """Delete a Kubernetes resource by type and name.

    Args:
        resource_type: Resource type or abbreviation (e.g. 'pod', 'deploy', 'svc', 'cm').
        name: Resource name.
        namespace: Namespace (omit for cluster-scoped resources like nodes or namespaces).
    """
    key = resource_type.lower()
    mapping = _RESOURCE_MAP.get(key)
    if not mapping:
        supported = sorted({v[1] for v in _RESOURCE_MAP.values()})
        return f"Unknown resource type '{resource_type}'. Supported: {', '.join(supported)}"
    api_version, kind = mapping
    try:
        dyn = _dyn()
        resource = dyn.resources.get(api_version=api_version, kind=kind)
        if namespace:
            resource.delete(name=name, namespace=namespace)
        else:
            resource.delete(name=name)
        return f"{kind}/{name} deleted"
    except ApiException as e:
        return f"Error deleting {kind}/{name}: {e.reason} (HTTP {e.status})"


@mcp.tool()
def get_resource_yaml(
    resource_type: str,
    name: str,
    namespace: str = "default",
) -> str:
    """Export a deployed Kubernetes resource as YAML.

    Returns the live resource manifest — useful for comparing deployed state
    against local manifests or detecting config drift.

    Args:
        resource_type: Resource type or abbreviation (e.g. 'pod', 'deploy', 'svc', 'cm').
        name: Resource name.
        namespace: Resource namespace (omit for cluster-scoped resources).
    """
    key = resource_type.lower()
    mapping = _RESOURCE_MAP.get(key)
    if not mapping:
        supported = sorted({v[1] for v in _RESOURCE_MAP.values()})
        return f"Unknown resource type '{resource_type}'. Supported: {', '.join(supported)}"

    api_version, kind = mapping
    try:
        dyn = _dyn()
        resource = dyn.resources.get(api_version=api_version, kind=kind)
        obj = resource.get(name=name, namespace=namespace)
        obj_dict = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        # Strip noisy fields for readability
        meta = obj_dict.get("metadata", {})
        meta.pop("managedFields", None)
        meta.pop("managed_fields", None)
        meta.pop("resourceVersion", None)
        meta.pop("resource_version", None)
        meta.pop("uid", None)
        meta.pop("selfLink", None)
        meta.pop("self_link", None)
        obj_dict.pop("status", None)
        return yaml.dump(obj_dict, default_flow_style=False, sort_keys=False)
    except ApiException as e:
        return f"Error getting {kind}/{name}: {e.reason} (HTTP {e.status})"


# ---- Resource metrics tools -----------------------------------------------

_METRICS_GROUP = "metrics.k8s.io"
_METRICS_VERSION = "v1beta1"


@mcp.tool()
def top_pods(
    namespace: str = "default",
    all_namespaces: bool = False,
    sort_by: str = "memory",
) -> str:
    """Show CPU and memory usage for pods (requires metrics-server).

    Equivalent to 'kubectl top pods'. Returns current resource consumption
    for each pod/container.

    Args:
        namespace: Target namespace.
        all_namespaces: Show pods across all namespaces.
        sort_by: Sort by 'cpu' or 'memory' (default: memory).
    """
    api = _custom()
    try:
        if all_namespaces:
            result = api.list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "pods")
        else:
            result = api.list_namespaced_custom_object(
                _METRICS_GROUP, _METRICS_VERSION, namespace, "pods",
            )
    except ApiException as e:
        if e.status == 404:
            return "metrics-server is not installed or not available in this cluster."
        return f"Error fetching pod metrics: {e.reason} (HTTP {e.status})"

    items = result.get("items", [])
    if not items:
        return "No pod metrics available."

    # Parse metrics into sortable rows
    rows: list[dict[str, Any]] = []
    for pod in items:
        meta = pod.get("metadata", {})
        pod_name = meta.get("name", "")
        pod_ns = meta.get("namespace", "")
        total_cpu_nano = 0
        total_mem_bytes = 0
        for c in pod.get("containers", []):
            usage = c.get("usage", {})
            total_cpu_nano += _parse_cpu(usage.get("cpu", "0"))
            total_mem_bytes += _parse_memory(usage.get("memory", "0"))
        rows.append({
            "namespace": pod_ns,
            "name": pod_name,
            "cpu_nano": total_cpu_nano,
            "mem_bytes": total_mem_bytes,
        })

    # Sort
    sort_key = "mem_bytes" if sort_by != "cpu" else "cpu_nano"
    rows.sort(key=lambda r: r[sort_key], reverse=True)

    header = f"{'NAMESPACE':<20} " if all_namespaces else ""
    lines = [f"{header}{'NAME':<50} {'CPU':<12} MEMORY"]
    for r in rows:
        prefix = f"{r['namespace']:<20} " if all_namespaces else ""
        lines.append(
            f"{prefix}{r['name']:<50} {_fmt_cpu(r['cpu_nano']):<12} "
            f"{_fmt_memory(r['mem_bytes'])}"
        )
    return "\n".join(lines)


@mcp.tool()
def top_nodes() -> str:
    """Show CPU and memory usage for nodes (requires metrics-server).

    Equivalent to 'kubectl top nodes'. Returns current resource consumption
    alongside allocatable capacity.
    """
    api = _custom()
    try:
        result = api.list_cluster_custom_object(_METRICS_GROUP, _METRICS_VERSION, "nodes")
    except ApiException as e:
        if e.status == 404:
            return "metrics-server is not installed or not available in this cluster."
        return f"Error fetching node metrics: {e.reason} (HTTP {e.status})"

    items = result.get("items", [])
    if not items:
        return "No node metrics available."

    # Get node capacity for percentage calculations
    node_capacity: dict[str, dict[str, int]] = {}
    try:
        for node in _core().list_node().items:
            alloc = node.status.allocatable or {}
            node_capacity[node.metadata.name] = {
                "cpu": _parse_cpu(alloc.get("cpu", "0")),
                "mem": _parse_memory(alloc.get("memory", "0")),
            }
    except ApiException:
        pass

    lines = [f"{'NAME':<40} {'CPU':<12} {'CPU%':<8} {'MEMORY':<12} MEM%"]
    for n in items:
        name = n.get("metadata", {}).get("name", "")
        usage = n.get("usage", {})
        cpu_nano = _parse_cpu(usage.get("cpu", "0"))
        mem_bytes = _parse_memory(usage.get("memory", "0"))

        cap = node_capacity.get(name, {})
        cpu_pct = f"{cpu_nano * 100 // cap['cpu']}%" if cap.get("cpu") else "-"
        mem_pct = f"{mem_bytes * 100 // cap['mem']}%" if cap.get("mem") else "-"

        lines.append(
            f"{name:<40} {_fmt_cpu(cpu_nano):<12} {cpu_pct:<8} "
            f"{_fmt_memory(mem_bytes):<12} {mem_pct}"
        )
    return "\n".join(lines)


def _parse_cpu(val: str) -> int:
    """Parse a K8s CPU value to nanocores."""
    if val.endswith("n"):
        return int(val[:-1])
    if val.endswith("u"):
        return int(val[:-1]) * 1000
    if val.endswith("m"):
        return int(val[:-1]) * 1_000_000
    return int(val) * 1_000_000_000


def _parse_memory(val: str) -> int:
    """Parse a K8s memory value to bytes."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    for suffix, multiplier in units.items():
        if val.endswith(suffix):
            return int(val[: -len(suffix)]) * multiplier
    if val.endswith("k"):
        return int(val[:-1]) * 1000
    if val.endswith("M"):
        return int(val[:-1]) * 1_000_000
    if val.endswith("G"):
        return int(val[:-1]) * 1_000_000_000
    return int(val)


def _fmt_cpu(nanocores: int) -> str:
    """Format nanocores as human-readable (e.g. '250m', '1.5')."""
    if nanocores >= 1_000_000_000:
        cores = nanocores / 1_000_000_000
        return f"{cores:.1f}" if cores != int(cores) else str(int(cores))
    return f"{nanocores // 1_000_000}m"


def _fmt_memory(mem_bytes: int) -> str:
    """Format bytes as human-readable (e.g. '128Mi', '2Gi')."""
    if mem_bytes >= 1024**3:
        return f"{mem_bytes // (1024**3)}Gi"
    if mem_bytes >= 1024**2:
        return f"{mem_bytes // (1024**2)}Mi"
    if mem_bytes >= 1024:
        return f"{mem_bytes // 1024}Ki"
    return f"{mem_bytes}B"


# ---- Wait tool ------------------------------------------------------------


@mcp.tool()
def wait_for_ready(
    resource_type: str,
    name: str,
    namespace: str = "default",
    timeout: int = 120,
    interval: int = 5,
) -> str:
    """Poll a resource until it is ready or a timeout is reached.

    Supports pods (Running + all containers ready) and deployments
    (all replicas updated, ready, and available). Returns the final status.

    Args:
        resource_type: 'pod' or 'deployment' (or abbreviations 'po', 'deploy').
        name: Resource name.
        namespace: Resource namespace.
        timeout: Maximum wait time in seconds.
        interval: Polling interval in seconds.
    """
    key = resource_type.lower()
    if key in ("pod", "pods", "po"):
        return _wait_pod(name, namespace, timeout, interval)
    if key in ("deployment", "deployments", "deploy"):
        return _wait_deployment(name, namespace, timeout, interval)
    return f"wait_for_ready supports 'pod' and 'deployment', got '{resource_type}'"


def _wait_pod(name: str, namespace: str, timeout: int, interval: int) -> str:
    """Poll until a pod is Running with all containers ready."""
    v1 = _core()
    deadline = time.monotonic() + timeout
    last_phase = ""
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(name, namespace)
        except ApiException as e:
            if e.status == 404:
                time.sleep(interval)
                continue
            return f"Error checking pod '{name}': {e.reason} (HTTP {e.status})"
        last_phase = pod.status.phase or "Unknown"
        if last_phase == "Running":
            statuses = pod.status.container_statuses or []
            if statuses and all(cs.ready for cs in statuses):
                return f"pod/{name} is Ready (all {len(statuses)} containers running)"
        elif last_phase in ("Failed", "Succeeded"):
            return f"pod/{name} terminated with phase: {last_phase}"
        time.sleep(interval)
    return f"Timed out after {timeout}s waiting for pod/{name} (last phase: {last_phase})"


def _wait_deployment(name: str, namespace: str, timeout: int, interval: int) -> str:
    """Poll until a deployment rollout is complete."""
    apps = _apps()
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        try:
            dep = apps.read_namespaced_deployment(name, namespace)
        except ApiException as e:
            return f"Error checking deployment '{name}': {e.reason} (HTTP {e.status})"
        desired = dep.spec.replicas or 1
        updated = dep.status.updated_replicas or 0
        ready = dep.status.ready_replicas or 0
        available = dep.status.available_replicas or 0
        last_status = f"{updated} updated, {ready} ready, {available} available of {desired}"
        # Check for stuck rollout
        for c in (dep.status.conditions or []):
            if c.type == "Progressing" and c.reason == "ProgressDeadlineExceeded":
                return f"deployment/{name} rollout STUCK: {c.message}"
        if updated >= desired and ready >= desired and available >= desired:
            return f"deployment/{name} rollout complete ({last_status})"
        time.sleep(interval)
    return f"Timed out after {timeout}s waiting for deployment/{name} ({last_status})"


# ---- Deployment manifest generation tool -----------------------------------

_MANIFEST_SERVICEACCOUNT = """apiVersion: v1
kind: ServiceAccount
metadata:
  name: k8s-mcp
"""

_MANIFEST_RBAC = """apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: k8s-mcp
rules:
  # ---- Read-only access ----
  - apiGroups: [""]
    resources:
      - pods
      - pods/log
      - services
      - events
      - configmaps
      - secrets
      - persistentvolumeclaims
      - serviceaccounts
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list, watch]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list, watch]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, networkpolicies]
    verbs: [get, list, watch]

  # ---- Write access (scale, restart, delete pods, exec) ----
  - apiGroups: [""]
    resources: [pods]
    verbs: [delete]
  - apiGroups: [""]
    resources: [pods/exec]
    verbs: [create]
  - apiGroups: [apps]
    resources: [deployments, deployments/scale, statefulsets, statefulsets/scale]
    verbs: [patch, update]

  # ---- Apply / delete manifests ----
  - apiGroups: [""]
    resources: [pods, services, configmaps, secrets, persistentvolumeclaims, serviceaccounts]
    verbs: [create, update, patch, delete]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets]
    verbs: [create, delete]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses]
    verbs: [create, update, patch, delete]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: k8s-mcp
subjects:
  - kind: ServiceAccount
    name: k8s-mcp
    namespace: {namespace}
roleRef:
  kind: Role
  name: k8s-mcp
  apiGroup: rbac.authorization.k8s.io
"""

_MANIFEST_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-mcp
  labels:
    app: k8s-mcp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: k8s-mcp
  template:
    metadata:
      labels:
        app: k8s-mcp
    spec:
      serviceAccountName: k8s-mcp{image_pull_secrets}
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: k8s-mcp
          image: k8s-mcp
          imagePullPolicy: Always
          ports:
            - containerPort: 8000
          env:
            - name: K8S_MCP_HOST
              value: "0.0.0.0"
            - name: K8S_MCP_PORT
              value: "8000"
            - name: K8S_MCP_TRANSPORT
              value: "streamable-http"
          resources:
            requests: {{ memory: "128Mi", cpu: "100m" }}
            limits:   {{ memory: "512Mi", cpu: "500m" }}
          readinessProbe:
            tcpSocket:
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            tcpSocket:
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 30
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
"""

_MANIFEST_SERVICE = """apiVersion: v1
kind: Service
metadata:
  name: k8s-mcp
spec:
  selector:
    app: k8s-mcp
  ports:
    - port: 8000
      targetPort: 8000
  type: ClusterIP
"""

_MANIFEST_KUSTOMIZATION = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: {namespace}
resources:
  - serviceaccount.yaml
  - rbac.yaml
  - deployment.yaml
  - service.yaml
images:
  - name: k8s-mcp
    newName: {registry}
    newTag: "{tag}"
"""


@mcp.tool()
def generate_deploy_manifests(
    namespace: str = "default",
    registry: str = "",
    tag: str = "latest",
    image_pull_secret: str = "",
    output_dir: str = "k8s",
) -> str:
    """Generate Kubernetes deployment manifests for deploying k8s-mcp to a cluster.

    Creates kustomize-based manifests (ServiceAccount, RBAC, Deployment, Service)
    in the specified output directory.

    Before calling this tool, the AI assistant should ask the user for:
      1. cluster — which cluster to deploy to (verify with get_current_context)
      2. namespace — target Kubernetes namespace
      3. registry — container image registry URL
      4. tag — image tag
      5. output_dir — where to write the manifests (default: k8s/)
      6. image_pull_secret — if their registry requires authentication

    After generation, the assistant should apply the manifests using the
    apply_manifest tool or kubectl, rather than asking the user to run commands.

    Args:
        namespace: Target Kubernetes namespace.
        registry: Container image registry URL (e.g. 'docker.io/user/k8s-mcp').
        tag: Image tag.
        image_pull_secret: Name of the image pull secret (leave empty if none).
        output_dir: Directory to write manifests to (default: k8s/).
    """
    registry = registry or "<YOUR_REGISTRY>/k8s-mcp"

    if image_pull_secret:
        ips_block = f"\n      imagePullSecrets:\n        - name: {image_pull_secret}"
    else:
        ips_block = ""

    manifests = {
        "serviceaccount.yaml": _MANIFEST_SERVICEACCOUNT,
        "rbac.yaml": _MANIFEST_RBAC.format(namespace=namespace),
        "deployment.yaml": _MANIFEST_DEPLOYMENT.format(image_pull_secrets=ips_block),
        "service.yaml": _MANIFEST_SERVICE,
        "kustomization.yaml": _MANIFEST_KUSTOMIZATION.format(
            namespace=namespace, registry=registry, tag=tag,
        ),
    }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    for filename, content in manifests.items():
        (out / filename).write_text(content)

    lines = [f"Manifests generated in {out}/"]
    lines.append(f"  Files: {', '.join(manifests.keys())}")
    lines.append("")

    if "<YOUR_REGISTRY>" not in registry:
        lines.append("Build and deploy:")
        lines.append(f"  docker build --platform linux/amd64 -t {registry}:{tag} .")
        lines.append(f"  docker push {registry}:{tag}")
        lines.append(f"  kubectl apply -k {output_dir}/")
    else:
        lines.append("Next steps:")
        lines.append(f"  1. Update the registry in {output_dir}/kustomization.yaml")
        lines.append(f"  2. Build and push the image:")
        lines.append(f"     docker build --platform linux/amd64 -t <YOUR_REGISTRY>/k8s-mcp:{tag} .")
        lines.append(f"     docker push <YOUR_REGISTRY>/k8s-mcp:{tag}")
        lines.append(f"  3. Apply the manifests:")
        lines.append(f"     kubectl apply -k {output_dir}/")

    lines.append("")
    lines.append(f"The server will be accessible at:")
    lines.append(f"  http://k8s-mcp.{namespace}.svc.cluster.local:8000")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the k8s-mcp server."""
    parser = argparse.ArgumentParser(description="Kubernetes MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("K8S_MCP_TRANSPORT", "streamable-http"),
        help="MCP transport (default: streamable-http, env: K8S_MCP_TRANSPORT)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.transport == "stdio":
        logger.info("Starting k8s-mcp (transport=stdio)")
    else:
        logger.info("Starting k8s-mcp on %s:%d (transport=%s)", _host, _port, args.transport)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
