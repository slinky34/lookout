"""Portainer API: find out what manages each container, and redeploy/recreate to update.

Endpoints used (checked against Portainer 2.45 source):
  GET  /api/endpoints                                   environments
  GET  /api/stacks                                      stacks (Id, Name, EndpointId, Type, GitConfig, Env, Option)
  GET  /api/endpoints/{env}/docker/containers/json      containers, via Portainer's Docker proxy
  GET  /api/stacks/{id}/file                            stack file of an editor/upload stack
  PUT  /api/stacks/{id}?endpointId=                     redeploy an editor/upload stack
  PUT  /api/stacks/{id}/git/redeploy?endpointId=        redeploy a Git stack
  POST /api/docker/{env}/containers/{id}/recreate       recreate a standalone container
Authentication: an access token in the X-API-Key header.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests
import urllib3

log = logging.getLogger(__name__)

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
STACK_COMPOSE = 2


class PortainerError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Portainer and its agent can't be recreated through Portainer: the request would
# stop the very process carrying it out.
_PORTAINER_SELF = re.compile(r"^(docker\.io/)?portainer/(portainer-ce|portainer-ee|portainer|agent)(:|@|$)")


@dataclass
class Index:
    """What Portainer knows, keyed for matching Lookout's containers."""
    endpoints: dict[int, str] = field(default_factory=dict)                 # id -> name
    containers: dict[str, dict] = field(default_factory=dict)               # full container id -> {endpoint, name, labels}
    by_name: dict[str, list[dict]] = field(default_factory=dict)            # container name -> [entries]
    stacks: dict[tuple[int, str], dict] = field(default_factory=dict)       # (endpoint id, project lower) -> stack
    errors: dict[int, str] = field(default_factory=dict)                    # endpoint id -> why it couldn't be read
    counts: dict[int, int] = field(default_factory=dict)                    # endpoint id -> containers seen
    skipped: dict[str, str] = field(default_factory=dict)                   # name -> why it isn't watched


class PortainerClient:
    def __init__(self, url: str, token: str, verify_tls: bool = False, timeout: float = 20.0):
        self.base = url.rstrip("/")
        if self.base.endswith("/api"):
            self.base = self.base[:-4]
        self.http = requests.Session()
        self.http.headers.update({"X-API-Key": token, "User-Agent": "lookout-update-watcher"})
        self.http.verify = bool(verify_tls)
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.timeout = timeout

    def _call(self, method: str, path: str, timeout: float | None = None, **kw):
        try:
            r = self.http.request(method, f"{self.base}/api{path}", timeout=timeout or self.timeout, **kw)
        except requests.exceptions.SSLError:
            raise PortainerError("Portainer's HTTPS certificate isn't trusted. Turn off certificate checking in Settings, or use its http:// address.")
        except requests.exceptions.ConnectionError:
            raise PortainerError(f"Can't reach Portainer at {self.base}")
        except requests.exceptions.Timeout:
            raise PortainerError("Portainer didn't answer in time")
        if r.status_code == 401:
            raise PortainerError("Portainer rejected the access token")
        if r.status_code == 403:
            raise PortainerError("The access token's user isn't allowed to do that in Portainer")
        if r.status_code >= 400:
            msg = ""
            try:
                body = r.json()
                msg = body.get("details") or body.get("message") or ""
            except ValueError:
                msg = r.text[:200]
            raise PortainerError(f"Portainer answered {r.status_code}{': ' + msg if msg else ''}", r.status_code)
        return r.json() if r.content else None

    # -- reading ---------------------------------------------------------------

    def index(self) -> Index:
        idx = Index()
        for ep in self._call("GET", "/endpoints") or []:
            # Portainer environment types: 1 Docker, 2 Docker agent, 4 Docker edge agent,
            # 3 Azure, 5-7 Kubernetes. Only Docker ones have containers to read here.
            name = ep.get("Name") or str(ep["Id"])
            if ep.get("Type") not in (1, 2, 4):
                idx.skipped[name] = "Not a Docker environment (Kubernetes/Azure aren't supported)"
                continue
            idx.endpoints[ep["Id"]] = name
            if ep.get("Status") == 2:
                idx.errors[ep["Id"]] = "Portainer says this environment is down"
        for ep_id in list(idx.endpoints):
            if ep_id in idx.errors:
                continue
            try:
                containers = self._call("GET", f"/endpoints/{ep_id}/docker/containers/json", params={"all": 1})
            except PortainerError as e:
                log.warning("Portainer environment %s: %s", idx.endpoints[ep_id], e)
                idx.errors[ep_id] = f"Portainer couldn't list this environment's containers: {e}"
                continue
            idx.counts[ep_id] = len(containers or [])
            for c in containers or []:
                name = (c.get("Names") or ["/?"])[0].lstrip("/")
                entry = {"endpoint": ep_id, "id": c["Id"], "name": name, "labels": c.get("Labels") or {}}
                idx.containers[c["Id"]] = entry
                idx.by_name.setdefault(name, []).append(entry)
        for s in self._call("GET", "/stacks") or []:
            idx.stacks[(s.get("EndpointId"), (s.get("Name") or "").lower())] = s
        return idx

    def summary(self) -> dict:
        idx = self.index()
        envs = [{"name": n, "containers": idx.counts.get(i, 0), "error": idx.errors.get(i)}
                for i, n in sorted(idx.endpoints.items())]
        envs += [{"name": n, "containers": 0, "error": why} for n, why in idx.skipped.items()]
        return {"environments": len(idx.endpoints), "containers": len(idx.containers),
                "stacks": len(idx.stacks), "details": envs}

    # -- updating --------------------------------------------------------------

    def redeploy_stack(self, stack: dict) -> None:
        sid, ep = stack["Id"], stack["EndpointId"]
        pull = {"RepullImageAndRedeploy": True, "PullImage": True}
        if stack.get("GitConfig"):
            self._call("PUT", f"/stacks/{sid}/git/redeploy", params={"endpointId": ep}, json=pull, timeout=600)
            return
        content = (self._call("GET", f"/stacks/{sid}/file") or {}).get("StackFileContent")
        if not content:
            raise PortainerError(f"Couldn't read the stack file for {stack.get('Name')}")
        prune = bool((stack.get("Option") or {}).get("Prune"))
        body = {"StackFileContent": content, "Env": stack.get("Env") or [], "Prune": prune, **pull}
        self._call("PUT", f"/stacks/{sid}", params={"endpointId": ep}, json=body, timeout=600)

    def recreate_container(self, endpoint: int, container_id: str) -> None:
        self._call("POST", f"/docker/{endpoint}/containers/{container_id}/recreate",
                   json={"PullImage": True}, timeout=600)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class PortainerDocker:
    """Just enough of docker.DockerClient for DockerWatcher, read through Portainer's
    Docker proxy for one environment, so its containers need no socket proxy."""

    def __init__(self, client: PortainerClient, endpoint: int):
        self.client = client
        self.endpoint = endpoint
        self._images: dict[str, dict] | None = None
        self.containers = _Obj(list=self._list_containers)
        self.images = _Obj(get=self._get_image)

    def _docker(self, path: str, **kw):
        return self.client._call("GET", f"/endpoints/{self.endpoint}/docker{path}", **kw)

    def _list_containers(self, all: bool = False):
        out = []
        for c in self._docker("/containers/json", params={"all": 1 if all else 0}) or []:
            image_ref = c.get("Image") or ""
            if image_ref.startswith("sha256:"):
                # The list shows an ID once the tag has moved on (a newer image was
                # pulled but the container wasn't recreated). Inspect for the real name.
                try:
                    image_ref = (self._docker(f"/containers/{c['Id']}/json").get("Config") or {}).get("Image") or image_ref
                except PortainerError:
                    pass
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            image_id = c.get("ImageID")
            out.append(_Obj(
                id=c["Id"], name=name, labels=c.get("Labels") or {},
                attrs={"Config": {"Image": image_ref}, "Image": image_id},
                image=_Obj(attrs=self._get_image(image_id, quiet=True)),
            ))
        return out

    def _get_image(self, image_id, quiet: bool = False):
        if self._images is None:
            self._images = {i["Id"]: i for i in (self._docker("/images/json") or [])}
        img = self._images.get(image_id)
        if img is None:
            if quiet:
                return {}
            raise KeyError(image_id)
        return _Obj(attrs=img) if not quiet else img


def classify(member: dict, idx: Index | None, endpoint_hint: int | None = None) -> dict:
    """How a container is managed, and whether Lookout can update it through Portainer.

    member: {"id", "name", "project"} from Lookout's own scan.
    Returns {"type": "stack"|"compose"|"container"|"swarm", "control": "total"|"limited"|None,
             "can_update": bool, "stack": name, "stack_id", "endpoint", "endpoint_name", "git": bool, "why": str}
    """
    project = member.get("project")
    out = {"type": "compose" if project else "container", "control": None, "can_update": False,
           "stack": project, "stack_id": None, "endpoint": None, "endpoint_name": None, "git": False, "why": None}
    if idx is None:
        out["why"] = "Connect Portainer in Settings to update from here"
        return out

    entry = idx.containers.get(member.get("id") or "")
    if entry is None:
        # Recreated containers get a new ID; find them by name, in the environment
        # they were last seen in when there's more than one with that name.
        candidates = idx.by_name.get(member.get("name") or "", [])
        if endpoint_hint is not None and len(candidates) > 1:
            candidates = [c for c in candidates if c["endpoint"] == endpoint_hint]
        entry = candidates[0] if len(candidates) == 1 else None
    if entry is None:
        if endpoint_hint is not None and endpoint_hint in idx.errors:
            out["why"] = f"Portainer can't reach {idx.endpoints.get(endpoint_hint, 'its environment')} right now: {idx.errors[endpoint_hint]}"
            out["stale"] = True
        elif endpoint_hint is not None:
            out["why"] = "This container has changed since the last check (it may have been recreated or removed). Lookout is checking again; try once that's done."
            out["stale"] = True
        else:
            out["why"] = "Portainer doesn't manage this container's environment"
        return out

    ep = entry["endpoint"]
    out.update(endpoint=ep, endpoint_name=idx.endpoints.get(ep))
    project = (entry["labels"].get(PROJECT_LABEL) or project or "").lower() or None
    if project:
        stack = idx.stacks.get((ep, project))
        if stack is None:
            out.update(type="compose", control="limited", stack=project,
                       why="Started outside Portainer (limited control), so Portainer can't redeploy it")
        elif stack.get("Type") != STACK_COMPOSE:
            out.update(type="swarm", control="total", stack=stack["Name"], stack_id=stack["Id"],
                       why="Swarm stacks aren't supported yet")
        else:
            out.update(type="stack", control="total", stack=stack["Name"], stack_id=stack["Id"],
                       git=bool(stack.get("GitConfig")), can_update=True)
    else:
        out.update(type="container", control="total", can_update=True)
    out["container_id"] = entry["id"]
    if _PORTAINER_SELF.match(member.get("image") or ""):
        out.update(can_update=False,
                   why="Portainer can't update itself or its agent. Update it from the command line on that machine.")
    return out
