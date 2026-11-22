"""A stand-in for Portainer's API, shaped after Portainer 2.45's handlers, for tests and demos.

Run it alone:  python tests/fake_portainer.py 9555
"""

from __future__ import annotations

import sys
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

TOKEN = "ptr_test"


def default_state(real_digests: dict | None = None):
    d = real_digests or {}
    old = "sha256:" + "0" * 64

    def c(cid, name, image, image_id, project=None, ep=1):
        labels = {"com.docker.compose.project": project, "com.docker.compose.service": name} if project else {}
        return {"Id": cid, "Names": [f"/{name}"], "Image": image, "ImageID": image_id, "Labels": labels, "_ep": ep}

    containers = [
        c("a" * 64, "sonarr", "lscr.io/linuxserver/sonarr:latest", "img-sonarr", "media"),
        c("b" * 64, "radarr", "lscr.io/linuxserver/radarr:latest", "img-radarr", "media"),
        c("c" * 64, "immich_server", "ghcr.io/immich-app/immich-server:release", "img-immich", "immich"),
        c("d" * 64, "nginx", "nginx:1.25.3", "img-nginx"),
        c("e" * 64, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest", "img-qbit", "downloads", ep=2),
        c("f" * 64, "portainer_agent", "portainer/agent:latest", "img-agent", ep=2),
    ]
    images = {
        1: [
            {"Id": "img-sonarr", "RepoDigests": [f"lscr.io/linuxserver/sonarr@{old}"]},
            {"Id": "img-radarr", "RepoDigests": [f"lscr.io/linuxserver/radarr@{d.get('lscr.io/linuxserver/radarr:latest', old)}"]},
            {"Id": "img-immich", "RepoDigests": [f"ghcr.io/immich-app/immich-server@{old}"]},
            {"Id": "img-nginx", "RepoDigests": [f"nginx@{old}"]},
        ],
        2: [
            {"Id": "img-qbit", "RepoDigests": [f"lscr.io/linuxserver/qbittorrent@{old}"]},
            {"Id": "img-agent", "RepoDigests": [f"portainer/agent@{d.get('portainer/agent:latest', old)}"]},
        ],
    }
    return {
        "endpoints": [{"Id": 1, "Name": "local", "Type": 1}, {"Id": 2, "Name": "nas", "Type": 2},
                      {"Id": 3, "Name": "k8s", "Type": 5}],
        "stacks": [
            {"Id": 5, "Name": "media", "EndpointId": 1, "Type": 2, "GitConfig": None,
             "Env": [{"name": "TZ", "value": "UTC"}], "Option": {"Prune": False}},
            {"Id": 9, "Name": "downloads", "EndpointId": 2, "Type": 2, "GitConfig": {"URL": "https://example.invalid/x.git"},
             "Env": [], "Option": None},
        ],
        "files": {5: "services:\n  sonarr:\n    image: lscr.io/linuxserver/sonarr:latest\n"},
        "containers": containers,
        "images": images,
        "calls": [],
    }


def create_app(state: dict) -> Flask:
    app = Flask("fake-portainer")

    @app.before_request
    def auth():
        if request.headers.get("X-API-Key") != TOKEN:
            return jsonify({"message": "Unauthorized", "details": "A valid authorisation token is missing"}), 401

    def record():
        state["calls"].append((request.method, request.path, request.args.to_dict(), request.get_json(silent=True)))

    @app.get("/api/endpoints")
    def endpoints():
        return jsonify(state["endpoints"])

    @app.get("/api/stacks")
    def stacks():
        return jsonify(state["stacks"])

    @app.get("/api/endpoints/<int:ep>/docker/containers/json")
    def containers(ep):
        if ep == 3:
            return jsonify({"message": "Not a Docker environment"}), 400
        show_all = request.args.get("all") in ("1", "true")
        return jsonify([{k: v for k, v in c.items() if not k.startswith("_")}
                        for c in state["containers"] if c["_ep"] == ep and (show_all or True)])

    @app.get("/api/endpoints/<int:ep>/docker/containers/<cid>/json")
    def inspect(ep, cid):
        c = next(c for c in state["containers"] if c["Id"] == cid)
        return jsonify({"Id": cid, "Config": {"Image": c["Image"]}})

    @app.get("/api/endpoints/<int:ep>/docker/images/json")
    def images(ep):
        return jsonify(state["images"].get(ep, []))

    @app.get("/api/stacks/<int:sid>/file")
    def stack_file(sid):
        return jsonify({"StackFileContent": state["files"].get(sid, "")})

    @app.put("/api/stacks/<int:sid>")
    def update_stack(sid):
        record()
        body = request.get_json()
        if not body.get("StackFileContent"):
            return jsonify({"message": "Invalid request payload", "details": "Invalid stack file content"}), 400
        return jsonify({"Id": sid})

    @app.put("/api/stacks/<int:sid>/git/redeploy")
    def redeploy(sid):
        record()
        return jsonify({"Id": sid})

    @app.post("/api/docker/<int:ep>/containers/<cid>/recreate")
    def recreate(ep, cid):
        record()
        return jsonify({"Id": "new" + cid[3:]})

    return app


class FakePortainer:
    def __init__(self, port: int = 0, state: dict | None = None):
        self.state = state or default_state()
        self.server = make_server("127.0.0.1", port, create_app(self.state))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
    from lookout import imageref
    from lookout.registry import RegistryClient
    reg = RegistryClient()
    real = {}
    for ref in ("lscr.io/linuxserver/radarr:latest", "portainer/agent:latest"):
        try:
            real[ref] = reg.manifest_digest(imageref.parse(ref))
        except Exception as e:
            print("couldn't fetch", ref, e)
    fp = FakePortainer(int(sys.argv[1]) if len(sys.argv) > 1 else 9555, default_state(real))
    print("fake Portainer on", fp.url, "token", TOKEN, flush=True)
    fp.server.serve_forever()
