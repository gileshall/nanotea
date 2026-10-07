"""Deploy nanotea in each of several ways and drive each headless (e2e/journey.py). Stdlib only: the host needs
python3, and docker for the Linux scenarios or uv and Ollama for native-macos.

python3 e2e/run.py [--out DIR] [--keep] SCENARIO...

Scenarios:
  native-macos   this Mac: uv tool install, nanotea init (say), whisper, llm with Ollama
  native-linux   the driver container as a Linux machine: the same with espeak-ng; Ollama in a container beside it
  docker         the shipped compose.yaml with Kokoro-FastAPI, behind Caddy on the host, as docs/running.md has it
  docker-kokoro  the image with the in-process Kokoro, behind Traefik and nginx (deploy/traefik)

Each scenario's results.json and artifacts are in DIR/<scenario>/; DIR/summary.json has every check of every
scenario. Exits 1 if any check failed. Docker resources are all named nanotea-ci*, capped, and removed after each
scenario; model caches stay in the nanotea-ci-cache-* volumes. --keep leaves a failed scenario's containers up."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "e2e" / "compose"
PLAYWRIGHT = "playwright==1.63.0"
OLLAMA_MODEL = "qwen2.5:0.5b"
LLM, LLM_OLLAMA = "llm==0.36", "llm-ollama==0.17.1"
PORTS = {"service": 27447, "https": 27443, "http": 27480, "push": 27460}
GATEWAY, NGINX = "172.30.77.1", "172.30.77.10"
DRIVER = "nanotea-ci-driver:latest"
BUILDER = "nanotea-ci"
CACHES = ("nanotea-ci-cache-service", "nanotea-ci-cache-driver", "nanotea-ci-cache-ollama")


def cap(cpus: float) -> str:
    """A CPU cap no bigger than the machine: docker refuses one that is."""
    return str(min(cpus, os.cpu_count()))


def sh(argv, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(map(str, argv)), flush=True)
    return subprocess.run([str(a) for a in argv], check=True, **kw)


def rewrite_argv(model: str) -> list[str]:
    return ["llm", "-m", model, "-s", "{system}", "--schema", "{schema_file}", "--no-stream"]


def write_scenario(work: Path, scenario: dict) -> None:
    (work / "scenario.json").write_text(json.dumps(scenario, indent=2))


# On this Mac


def native_macos(work: Path, args) -> int:
    for tool in ("uv", "ollama", "ffmpeg", "say"):
        if not shutil.which(tool):
            sys.exit(f"native-macos needs {tool} on the PATH")
    models = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True).stdout
    if OLLAMA_MODEL not in models:
        sys.exit(f"native-macos needs the Ollama model {OLLAMA_MODEL}: ollama pull {OLLAMA_MODEL}")
    whisper_py = args.whisper_python
    if not whisper_py:
        sys.exit("native-macos needs --whisper-python, a python3 with openai-whisper installed")
    sh([whisper_py, "-c", "import whisper"])
    certs = work / "certs"
    sh(["uv", "run", "--project", ROOT, "python", "-m", "e2e.certs", certs, "127.0.0.1", "localhost"], cwd=ROOT)
    (work / "home").mkdir()
    tools = work / "tools"
    url = f"http://127.0.0.1:{PORTS['service']}"
    write_scenario(work, {
        "name": "native-macos", "public_url": url, "agent_port": PORTS["service"],
        "config": str(work / "home" / "config.toml"), "bin": str(tools / "bin"),
        # As docs/running.md installs it, in a place of the run's own.
        "native": {"init_url": url,
                   "install": [["uv", "tool", "install", "--quiet", "--force", str(ROOT)],
                               ["uv", "tool", "install", "--quiet", "--force", LLM, "--with", LLM_OLLAMA]],
                   "install_env": {"UV_TOOL_DIR": str(tools / "venvs"), "UV_TOOL_BIN_DIR": str(tools / "bin")},
                   "env": {"REQUESTS_CA_BUNDLE": str(certs / "ca.crt"),
                           "PATH": f"{tools / 'bin'}:{os.environ['PATH']}"},
                   "edits": native_edits(whisper_py, args.whisper_model)},
        "push": {"listen": "127.0.0.1", "port": PORTS["push"], "endpoint_host": "127.0.0.1",
                 "cert": str(certs / "cert.pem"), "key": str(certs / "key.pem")},
        "fake_mic": "say", "stt_python": whisper_py, "stt_model": args.whisper_model,
        "transcribe_sh": str(ROOT / "nanotea" / "transcribe.sh"),
        "expect": {"tts": "say", "stt": f"whisper {args.whisper_model}", "rewrite": f"llm {OLLAMA_MODEL}"},
    })
    env = {k: v for k, v in os.environ.items() if not k.startswith("NANOTEA_")}
    return subprocess.run(["uv", "run", "--project", ROOT, "--with", PLAYWRIGHT, "python", "-m", "e2e.journey",
                           work / "scenario.json"], cwd=ROOT, env=env).returncode


def native_edits(whisper_py: str, model: str) -> list:
    return [[["rewrite"], "backend", "command"],
            [["rewrite", "command"], "argv", rewrite_argv(OLLAMA_MODEL)],
            [["stt", "command"], "argv", ["sh", "{package}/transcribe.sh", whisper_py, model, "{audio_file}"]],
            # The browser is on this machine, so push works over http, with a mailto: contact.
            [["notify"], "now", ["push"]]]


# Docker


class Docker:
    """Images, the builder and the caches: everything named nanotea-ci*, and nothing else touched."""

    def __init__(self, args):
        self.args = args
        self.built: set[str] = set()

    def builder(self) -> None:
        if subprocess.run(["docker", "buildx", "inspect", BUILDER], capture_output=True).returncode != 0:
            sh(["docker", "buildx", "create", "--name", BUILDER, "--driver", "docker-container",
                "--driver-opt", f"cpu-quota={int(float(cap(4)) * 100000)}", "--driver-opt", "cpu-period=100000",
                "--driver-opt", "cpu-shares=256", "--driver-opt", "memory=8g"])
        for v in CACHES:
            sh(["docker", "volume", "create", v], stdout=subprocess.DEVNULL)

    def build(self, tag: str, *build_args: str, dockerfile: str | None = None) -> None:
        if tag in self.built:
            return
        argv = ["docker", "buildx", "build", "--builder", BUILDER, "--load", "--progress", "plain", "-t", tag]
        for a in build_args:
            argv += ["--build-arg", a]
        if dockerfile:
            argv += ["-f", ROOT / dockerfile]
        sh([*argv, ROOT])
        self.built.add(tag)

    def driver(self, work: Path, name: str, network: str, argv: list[str], cpus: str, mem: str) -> int:
        """The driver container, as this host's user, with the run's work directory and e2e/ mounted."""
        cmd = ["docker", "run", "--rm", "--name", f"nanotea-ci-driver-{name}", "--network", network,
               "--cpus", cpus, "--memory", mem, "--cpu-shares", "256", "--shm-size", "1g",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp/home",
               "-v", f"{work}:/work", "-v", f"{ROOT / 'e2e'}:/src/e2e:ro",
               "-v", "nanotea-ci-cache-driver:/cache", DRIVER, *argv]
        print("+", " ".join(map(str, cmd)), flush=True)
        return subprocess.run([str(a) for a in cmd]).returncode

    def prepare(self, work: Path, names: list[str]) -> None:
        """The run's certificates, made in the driver so the host needs nothing but python3 and docker."""
        rc = self.driver(work, "certs", "none", ["python", "-m", "e2e.certs", "/work/certs", *names], "1", "512m")
        if rc != 0:
            sys.exit("making certificates failed")


class Compose:
    def __init__(self, project: str, files: list[Path], env: dict):
        self.project, self.files, self.env = project, files, {**os.environ, **env}

    def __call__(self, *argv, **kw) -> subprocess.CompletedProcess:
        files = [x for f in self.files for x in ("-f", f)]
        return sh(["docker", "compose", "-p", self.project, *files, *argv], env=self.env, **kw)

    def down(self) -> None:
        self("down", "-v", "--remove-orphans", "--timeout", "20")


def docker_service(work: Path, args, dk: Docker, name: str, flavor: str, edits: list, proxy: str) -> int:
    """The shipped compose.yaml with the run's override, the driver on the host's network as the owner's
    browser and the host's agents."""
    dk.build(DRIVER, dockerfile="e2e/driver.Dockerfile")
    dk.build(f"nanotea-ci-service:{flavor}", "WHISPER=1", f"KOKORO={1 if flavor == 'kokoro' else 0}")
    dk.prepare(work, ["nanotea.test", "push.test", "127.0.0.1", GATEWAY])
    cfg = work / "docker-config"
    cfg.mkdir()
    (cfg / "env").touch()
    public = f"https://nanotea.test:{PORTS['https']}"
    edits = [[[], "port", PORTS["service"]], [[], "public_url", public], [["app"], "owner", "Robin"],
             [["notify", "push"], "subject", "https://nanotea.test"], *edits]
    shutil.copy(ROOT / "docs" / "docker.config.toml", cfg / "config.toml")
    rc = dk.driver(work, "configure", "none", ["python", "-m", "e2e.configure", "/work/docker-config/config.toml",
                                                "/work/docker-config/config.toml", json.dumps(edits)], "1", "512m")
    if rc != 0:
        sys.exit("writing the config failed")
    files = [ROOT / "compose.yaml", COMPOSE / "ci.yaml"]
    profiles = []
    if proxy == "traefik":
        files.append(COMPOSE / "traefik.yaml")
        (work / "nginx.conf").write_text((ROOT / "deploy" / "traefik" / "nginx.conf").read_text()
                                         .replace("HOST_RUNNING_NANOTEA:7447", f"nanotea:{PORTS['service']}"))
        (work / "traefik.yaml").write_text(
            "http:\n  routers:\n    pub:\n      rule: Host(`nanotea.test`)\n      entryPoints: [https]\n"
            "      service: pub\n      tls: {}\n  services:\n    pub:\n      loadBalancer:\n"
            f"        servers:\n          - url: http://{NGINX}:80\n"
            "tls:\n  certificates:\n    - certFile: /certs/cert.pem\n      keyFile: /certs/key.pem\n")
    if flavor == "whisper":
        profiles = ["--profile", "kokoro"]
    project = f"nanotea-ci-{name}"
    compose = Compose(project, files, {
        "NANOTEA_PORT": str(PORTS["service"]), "NANOTEA_CONFIG_DIR": str(cfg), "NANOTEA_CI_FLAVOR": flavor,
        "NANOTEA_CI_WORK": str(work), "NANOTEA_CI_CPUS": cap(3 if flavor == "kokoro" else 2),
        "NANOTEA_CI_KOKORO_CPUS": cap(2),
        "NANOTEA_CI_MEM": "5g" if flavor == "kokoro" else "4g", "COMPOSE_PROFILES": ""})
    ok = False
    try:
        if flavor == "kokoro":
            compose("run", "--rm", "--no-deps", "nanotea", "kokoro", "download")
        compose(*profiles, "up", "-d", "--wait", "--wait-timeout", "600")
        link = compose("exec", "-T", "nanotea", "nanotea", "pair-link", capture_output=True, text=True).stdout
        (work / "pair-link").touch(mode=0o600)
        (work / "pair-link").write_text(link)
        scenario = {
            "name": name, "public_url": public, "agent_port": PORTS["service"],
            "config": "/work/docker-config/config.toml", "pair_link_file": "/work/pair-link", "bin": "/opt/e2e/bin",
            "resolve": {"nanotea.test": "127.0.0.1"}, "trust": [] if proxy == "caddy" else ["/work/certs/ca.crt"],
            "push": {"listen": GATEWAY, "port": PORTS["push"], "endpoint_host": "push.test",
                     "cert": "/work/certs/cert.pem", "key": "/work/certs/key.pem"},
            "fake_mic": "espeak-ng", "stt_python": "/opt/whisper/bin/python", "stt_model": "small",
            "transcribe_sh": "/src/nanotea/transcribe.sh",
            "expect": {"tts": "Kokoro-FastAPI v0.9.0" if flavor == "whisper" else "kokoro in the service",
                       "stt": "whisper small", "rewrite": "identity", "proxy": proxy},
        }
        if proxy == "caddy":
            scenario["caddy"] = {"site": f"nanotea.test:{PORTS['https']}", "bind": "127.0.0.1",
                                 "upstream": f"127.0.0.1:{PORTS['service']}", "http_port": PORTS["http"]}
        write_scenario(work, scenario)
        rc = dk.driver(work, name, "host", ["python", "-m", "e2e.journey", "/work/scenario.json"], cap(3), "6g")
        ok = rc == 0
        return rc
    finally:
        with open(work / "compose.log", "w") as log:
            compose(*profiles, "logs", "--no-color", "--timestamps", stdout=log, stderr=subprocess.STDOUT)
        if ok or not args.keep:
            compose(*profiles, "down", "-v", "--remove-orphans", "--timeout", "20")


def docker(work: Path, args, dk: Docker) -> int:
    edits = [[["stt", "command"], "timeout_s", 900]]
    return docker_service(work, args, dk, "docker", "whisper", edits, "caddy")


def docker_kokoro(work: Path, args, dk: Docker) -> int:
    edits = [[[], "trusted_proxies", [GATEWAY, NGINX]], [["tts"], "backend", "kokoro"]]
    return docker_service(work, args, dk, "docker-kokoro", "kokoro", edits, "traefik")


def native_linux(work: Path, args, dk: Docker) -> int:
    dk.build(DRIVER, dockerfile="e2e/driver.Dockerfile")
    dk.prepare(work, ["127.0.0.1", "localhost"])
    compose = Compose("nanotea-ci-native", [COMPOSE / "native.yaml"], {"NANOTEA_CI_OLLAMA_CPUS": cap(3)})
    ok = False
    try:
        compose("up", "-d", "--wait", "--wait-timeout", "120")
        compose("exec", "-T", "ollama", "ollama", "pull", OLLAMA_MODEL)
        url = f"http://127.0.0.1:{PORTS['service']}"
        (work / "home").mkdir()
        write_scenario(work, {
            "name": "native-linux", "public_url": url, "agent_port": PORTS["service"],
            "config": "/work/home/config.toml", "bin": "/work/tools/bin",
            "native": {"init_url": url,
                       "install": [["uv", "tool", "install", "--quiet", "--force", "/src"]],
                       "install_env": {"UV_TOOL_DIR": "/work/tools/venvs", "UV_TOOL_BIN_DIR": "/work/tools/bin",
                                       "UV_CACHE_DIR": "/cache/uv"},
                       "env": {"REQUESTS_CA_BUNDLE": "/work/certs/ca.crt", "OLLAMA_HOST": "http://ollama:11434"},
                       "edits": native_edits("/opt/whisper/bin/python", "base.en")},
            "push": {"listen": "127.0.0.1", "port": PORTS["push"], "endpoint_host": "127.0.0.1",
                     "cert": "/work/certs/cert.pem", "key": "/work/certs/key.pem"},
            "fake_mic": "espeak-ng", "stt_python": "/opt/whisper/bin/python", "stt_model": "base.en",
            "transcribe_sh": "/src/nanotea/transcribe.sh",
            "expect": {"tts": "espeak-ng", "stt": "whisper base.en", "rewrite": f"llm {OLLAMA_MODEL}"},
        })
        rc = dk.driver(work, "native-linux", "nanotea-ci-native_default",
                       ["python", "-m", "e2e.journey", "/work/scenario.json"], cap(5), "8g")
        ok = rc == 0
        return rc
    finally:
        with open(work / "compose.log", "w") as f:
            compose("logs", "--no-color", "--timestamps", stdout=f, stderr=subprocess.STDOUT)
        if ok or not args.keep:
            compose.down()


SCENARIOS = {"native-macos": native_macos, "native-linux": native_linux, "docker": docker,
             "docker-kokoro": docker_kokoro}
LINUX = ("native-linux", "docker", "docker-kokoro")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("scenarios", nargs="+", choices=sorted(SCENARIOS) + ["linux"])
    p.add_argument("--out", type=Path, help="where results go; by default /tmp/nanotea-ci-<time>")
    p.add_argument("--keep", action="store_true", help="leave a failed scenario's containers up to look at")
    p.add_argument("--whisper-python", help="native-macos: a python3 with openai-whisper")
    p.add_argument("--whisper-model", default="small.en", help="native-macos: the whisper model (default small.en)")
    args = p.parse_args()
    names = [n for s in args.scenarios for n in (LINUX if s == "linux" else (s,))]
    out = (args.out or Path(f"/tmp/nanotea-ci-{time.strftime('%Y%m%d-%H%M%S')}")).resolve()
    out.mkdir(parents=True, exist_ok=False)
    dk = None
    summary = {}
    for name in names:
        work = out / name
        work.mkdir()
        os.chmod(work, 0o755)
        if name == "native-macos":
            rc = native_macos(work, args)
        else:
            if dk is None:
                dk = Docker(args)
                dk.builder()
            rc = SCENARIOS[name](work, args, dk)
        results = work / "results.json"
        checks = json.loads(results.read_text())["checks"] if results.exists() else []
        summary[name] = {"exit": rc, "checks": [{k: c[k] for k in ("check", "ok", "seconds", "metrics", "error")
                                                 if k in c} for c in checks]}
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nresults: {out}")
    for name, s in summary.items():
        failed = [c["check"] for c in s["checks"] if not c["ok"]]
        print(f"  {name}: {'ok' if s['exit'] == 0 else 'FAILED ' + (', '.join(failed) or f'exit {s['exit']}')}")
    sys.exit(0 if all(s["exit"] == 0 for s in summary.values()) else 1)


if __name__ == "__main__":
    main()
