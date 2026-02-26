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
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config, dynamic
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
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
}

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

_host = os.getenv("K8S_MCP_HOST", "localhost")
_port = int(os.getenv("K8S_MCP_PORT", "8000"))

mcp = FastMCP(
    "k8s-mcp",
    instructions="Kubernetes MCP server — run kubectl-like operations from AI assistants",
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

  # ---- Write access (scale, restart, delete pods) ----
  - apiGroups: [""]
    resources: [pods]
    verbs: [delete]
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
